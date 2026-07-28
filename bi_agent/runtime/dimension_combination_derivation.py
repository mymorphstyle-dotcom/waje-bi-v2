from __future__ import annotations

from itertools import combinations
from typing import Any, Mapping, Sequence


class DimensionCombinationDerivationError(ValueError):
    pass


_POLICY_FIELDS = {
    "schema_version",
    "source_dependency",
    "candidate_field",
    "candidate_pool_limit",
    "depth_budgets",
    "maximum_estimated_cells_per_query",
    "maximum_estimated_cells_total",
    "ancestor_pair_policy",
}


def validate_dimension_combination_policy(
    value: Any,
    *,
    expected_source_dependency: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _POLICY_FIELDS:
        raise DimensionCombinationDerivationError(
            "dimension_combination_policy_invalid"
        )
    source_dependency = value.get("source_dependency")
    candidate_field = value.get("candidate_field")
    candidate_pool_limit = value.get("candidate_pool_limit")
    per_query_limit = value.get("maximum_estimated_cells_per_query")
    total_limit = value.get("maximum_estimated_cells_total")
    if (
        value.get("schema_version")
        != "dimension-combination-derivation-policy.v1"
        or type(source_dependency) is not str
        or not source_dependency
        or (
            expected_source_dependency is not None
            and source_dependency != expected_source_dependency
        )
        or type(candidate_field) is not str
        or not candidate_field
        or candidate_field != candidate_field.strip()
        or type(candidate_pool_limit) is not int
        or not 2 <= candidate_pool_limit <= 12
        or type(per_query_limit) is not int
        or per_query_limit <= 0
        or type(total_limit) is not int
        or total_limit < per_query_limit
        or value.get("ancestor_pair_policy") != "exclude"
    ):
        raise DimensionCombinationDerivationError(
            "dimension_combination_policy_invalid"
        )
    raw_budgets = value.get("depth_budgets")
    if (
        not isinstance(raw_budgets, list)
        or not raw_budgets
        or any(
            not isinstance(item, Mapping)
            or set(item) != {"depth", "maximum_combinations"}
            or type(item.get("depth")) is not int
            or not 2 <= item["depth"] <= candidate_pool_limit
            or type(item.get("maximum_combinations")) is not int
            or item["maximum_combinations"] <= 0
            for item in raw_budgets
        )
    ):
        raise DimensionCombinationDerivationError(
            "dimension_combination_policy_invalid"
        )
    depths = [int(item["depth"]) for item in raw_budgets]
    if depths != sorted(set(depths)):
        raise DimensionCombinationDerivationError(
            "dimension_combination_policy_invalid"
        )
    return {
        "schema_version": str(value["schema_version"]),
        "source_dependency": source_dependency,
        "candidate_field": candidate_field,
        "candidate_pool_limit": candidate_pool_limit,
        "depth_budgets": [
            {
                "depth": int(item["depth"]),
                "maximum_combinations": int(item["maximum_combinations"]),
            }
            for item in raw_budgets
        ],
        "maximum_estimated_cells_per_query": per_query_limit,
        "maximum_estimated_cells_total": total_limit,
        "ancestor_pair_policy": "exclude",
    }


def derive_dimension_combinations(
    candidate_payload: Mapping[str, Any],
    *,
    dimension_metadata: Mapping[str, Mapping[str, Any]],
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    normalized_policy = validate_dimension_combination_policy(policy)
    if not isinstance(candidate_payload, Mapping) or not isinstance(
        dimension_metadata, Mapping
    ):
        raise DimensionCombinationDerivationError(
            "dimension_combination_evidence_invalid"
        )
    raw_candidates = candidate_payload.get(normalized_policy["candidate_field"])
    raw_profiles = candidate_payload.get("dimension_profiles")
    if (
        isinstance(raw_candidates, (str, bytes))
        or not isinstance(raw_candidates, Sequence)
        or isinstance(raw_profiles, (str, bytes))
        or not isinstance(raw_profiles, Sequence)
    ):
        raise DimensionCombinationDerivationError(
            "dimension_combination_evidence_invalid"
        )
    ranked: list[tuple[int, str]] = []
    seen_dimensions: set[str] = set()
    for item in raw_candidates:
        if not isinstance(item, Mapping):
            raise DimensionCombinationDerivationError(
                "dimension_combination_evidence_invalid"
            )
        dimension_id = item.get("dimension")
        rank = item.get("priority_rank")
        if (
            type(dimension_id) is not str
            or not dimension_id
            or type(rank) is not int
            or rank <= 0
            or dimension_id in seen_dimensions
            or dimension_id not in dimension_metadata
        ):
            raise DimensionCombinationDerivationError(
                "dimension_combination_evidence_invalid"
            )
        seen_dimensions.add(dimension_id)
        ranked.append((rank, dimension_id))
    ranked.sort(key=lambda item: (item[0], item[1]))
    pool = tuple(
        dimension_id
        for _, dimension_id in ranked[: normalized_policy["candidate_pool_limit"]]
    )
    profile_by_dimension: dict[str, Mapping[str, Any]] = {}
    for item in raw_profiles:
        if not isinstance(item, Mapping):
            raise DimensionCombinationDerivationError(
                "dimension_combination_evidence_invalid"
            )
        dimension_id = item.get("dimension")
        segment_count = item.get("segment_count")
        if (
            type(dimension_id) is not str
            or not dimension_id
            or type(segment_count) is not int
            or segment_count < 0
        ):
            raise DimensionCombinationDerivationError(
                "dimension_combination_evidence_invalid"
            )
        profile_by_dimension[dimension_id] = item
    if any(item not in profile_by_dimension for item in pool):
        raise DimensionCombinationDerivationError(
            "dimension_combination_evidence_invalid"
        )

    selected: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    estimated_cells_total = 0
    rank_by_dimension = {
        dimension_id: rank for rank, dimension_id in ranked
    }
    for budget in normalized_policy["depth_budgets"]:
        admitted_for_depth = 0
        depth = int(budget["depth"])
        ordered_combinations = sorted(
            combinations(pool, depth),
            key=lambda group: (
                max(rank_by_dimension[item] for item in group),
                sum(rank_by_dimension[item] for item in group),
                group,
            ),
        )
        for group in ordered_combinations:
            if admitted_for_depth >= int(budget["maximum_combinations"]):
                break
            if _contains_ancestor_pair(group, dimension_metadata):
                excluded.append(
                    {
                        "dimension_ids": group,
                        "reason": "ancestor_pair_excluded",
                    }
                )
                continue
            estimated_cells = 1
            for dimension_id in group:
                estimated_cells *= max(
                    1,
                    int(profile_by_dimension[dimension_id]["segment_count"]),
                )
            if (
                estimated_cells
                > normalized_policy["maximum_estimated_cells_per_query"]
            ):
                excluded.append(
                    {
                        "dimension_ids": group,
                        "reason": "per_query_cell_budget_exceeded",
                        "estimated_cells": estimated_cells,
                    }
                )
                continue
            if (
                estimated_cells_total + estimated_cells
                > normalized_policy["maximum_estimated_cells_total"]
            ):
                excluded.append(
                    {
                        "dimension_ids": group,
                        "reason": "total_cell_budget_exceeded",
                        "estimated_cells": estimated_cells,
                    }
                )
                continue
            selected.append(
                {
                    "dimension_ids": group,
                    "depth": depth,
                    "estimated_cells": estimated_cells,
                    "source_ranks": tuple(
                        rank_by_dimension[item] for item in group
                    ),
                }
            )
            estimated_cells_total += estimated_cells
            admitted_for_depth += 1
    return {
        "schema_version": "derived-dimension-combinations.v1",
        "source_dependency": normalized_policy["source_dependency"],
        "candidate_pool": pool,
        "selected_combinations": tuple(selected),
        "excluded_combinations": tuple(excluded),
        "estimated_cells_total": estimated_cells_total,
        "policy": normalized_policy,
    }


def _contains_ancestor_pair(
    group: Sequence[str],
    dimension_metadata: Mapping[str, Mapping[str, Any]],
) -> bool:
    selected = set(group)
    for dimension_id in group:
        current = str(
            dimension_metadata.get(dimension_id, {}).get("parent_dimension") or ""
        )
        visited: set[str] = set()
        while current:
            if current in visited:
                raise DimensionCombinationDerivationError(
                    "dimension_hierarchy_cycle"
                )
            if current in selected:
                return True
            visited.add(current)
            current = str(
                dimension_metadata.get(current, {}).get("parent_dimension") or ""
            )
    return False
