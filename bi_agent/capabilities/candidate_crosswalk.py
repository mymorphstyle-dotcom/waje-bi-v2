from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from math import fsum, isfinite
from typing import Any


_SUPPORTED_RULES = frozenset(
    {
        "unicode_casefold",
        "remove_non_alphanumeric",
        "strip_paid_source_prefix_pa",
    }
)
_SUPPORTED_METRIC_STRATEGIES = frozenset({"sum", "ratio"})


def candidate_crosswalk(
    left_rows: Iterable[Mapping[str, Any]],
    right_rows: Iterable[Mapping[str, Any]],
    *,
    time_key: str,
    group_key: str,
    metric_strategies: Mapping[str, Mapping[str, str]],
    candidate_rules: Iterable[str] = (),
    mapped_group_key: str = "mapped_group",
) -> dict[str, Any]:
    """Build a mechanical, candidate-only cross-source group alignment.

    ``candidate_rules`` defaults to exact string matching. Rules must be named
    explicitly; this function never performs fuzzy matching. A normalized key
    is accepted only when it identifies exactly one source value on each side.

    ``metric_strategies`` has the shape
    ``{"left": {"metric": "sum"}, "right": {"metric": "ratio"}}``.
    ``sum`` metrics may be aggregated across duplicate source rows. ``ratio``
    metrics are assumed to have been aggregated upstream and therefore reject
    duplicate rows for the same time x source-group cell.
    """

    _require_name(time_key, "time_key")
    _require_name(group_key, "group_key")
    _require_name(mapped_group_key, "mapped_group_key")
    rules = _validate_rules(candidate_rules)
    strategies = _validate_metric_strategies(metric_strategies)
    left = tuple(left_rows)
    right = tuple(right_rows)
    _validate_rows(left, side="left", time_key=time_key, group_key=group_key)
    _validate_rows(right, side="right", time_key=time_key, group_key=group_key)

    left_index, left_empty_rows = _normalized_group_index(
        left,
        side="left",
        group_key=group_key,
        rules=rules,
    )
    right_index, right_empty_rows = _normalized_group_index(
        right,
        side="right",
        group_key=group_key,
        rules=rules,
    )
    accepted_keys = tuple(
        sorted(
            key
            for key in left_index.keys() & right_index.keys()
            if len(left_index[key]) == 1 and len(right_index[key]) == 1
        )
    )
    accepted = {
        key: (next(iter(left_index[key])), next(iter(right_index[key])))
        for key in accepted_keys
    }
    left_to_mapped = {
        left_value: key for key, (left_value, _right_value) in accepted.items()
    }
    right_to_mapped = {
        right_value: key for key, (_left_value, right_value) in accepted.items()
    }

    left_cells = _aggregate_source_cells(
        left,
        side="left",
        time_key=time_key,
        group_key=group_key,
        strategies=strategies["left"],
        raw_to_mapped=left_to_mapped,
    )
    right_cells = _aggregate_source_cells(
        right,
        side="right",
        time_key=time_key,
        group_key=group_key,
        strategies=strategies["right"],
        raw_to_mapped=right_to_mapped,
    )
    aligned_rows = _aligned_rows(
        left_cells,
        right_cells,
        accepted=accepted,
        time_key=time_key,
        mapped_group_key=mapped_group_key,
        left_metrics=tuple(strategies["left"]),
        right_metrics=tuple(strategies["right"]),
    )

    left_ambiguous_keys = {
        key for key, values in left_index.items() if len(values) != 1
    }
    right_ambiguous_keys = {
        key for key, values in right_index.items() if len(values) != 1
    }
    ambiguous_keys = left_ambiguous_keys | right_ambiguous_keys
    mapped_left_values = set(left_to_mapped)
    mapped_right_values = set(right_to_mapped)
    left_values = {value for values in left_index.values() for value in values}
    right_values = {value for values in right_index.values() for value in values}
    left_unmatched = left_values - mapped_left_values
    right_unmatched = right_values - mapped_right_values
    left_coverage, left_coverage_detail = _metric_coverage(
        left,
        group_key=group_key,
        strategies=strategies["left"],
        raw_to_mapped=left_to_mapped,
    )
    right_coverage, right_coverage_detail = _metric_coverage(
        right,
        group_key=group_key,
        strategies=strategies["right"],
        raw_to_mapped=right_to_mapped,
    )

    summary = {
        "pair_count": len(accepted),
        "left_distinct_group_count": len(left_values),
        "right_distinct_group_count": len(right_values),
        "left_unmatched_count": len(left_unmatched),
        "right_unmatched_count": len(right_unmatched),
        "unmatched_count": len(left_unmatched) + len(right_unmatched),
        "ambiguous_count": len(ambiguous_keys),
        "left_ambiguous_group_count": sum(
            len(left_index[key]) for key in left_ambiguous_keys
        ),
        "right_ambiguous_group_count": sum(
            len(right_index[key]) for key in right_ambiguous_keys
        ),
        "left_empty_group_row_count": left_empty_rows,
        "right_empty_group_row_count": right_empty_rows,
        "left_metric_coverage": left_coverage,
        "right_metric_coverage": right_coverage,
        "left_metric_coverage_detail": left_coverage_detail,
        "right_metric_coverage_detail": right_coverage_detail,
        "aligned_cell_count": len(aligned_rows),
        "complete_aligned_cell_count": sum(
            bool(row["left_present"] and row["right_present"])
            for row in aligned_rows
        ),
    }
    return {
        "candidate_rules": rules,
        "mapping_status": "candidate_unreviewed",
        "mapping_pairs": tuple(
            {
                mapped_group_key: key,
                "left_group": left_value,
                "right_group": right_value,
            }
            for key, (left_value, right_value) in sorted(accepted.items())
        ),
        "mapping_summary": summary,
        "aligned_rows": aligned_rows,
    }


def _validate_rules(candidate_rules: Iterable[str]) -> tuple[str, ...]:
    rules = tuple(str(rule).strip() for rule in candidate_rules)
    if any(not rule for rule in rules):
        raise ValueError("candidate_rules cannot contain empty values")
    unsupported = set(rules) - _SUPPORTED_RULES
    if unsupported:
        raise ValueError(
            "unsupported candidate_rules: " + ", ".join(sorted(unsupported))
        )
    return rules


def _validate_metric_strategies(
    metric_strategies: Mapping[str, Mapping[str, str]],
) -> dict[str, dict[str, str]]:
    if set(metric_strategies) != {"left", "right"}:
        raise ValueError("metric_strategies must declare exactly left and right")
    normalized: dict[str, dict[str, str]] = {}
    for side in ("left", "right"):
        side_value = metric_strategies.get(side)
        if not isinstance(side_value, Mapping) or not side_value:
            raise ValueError(f"metric_strategies.{side} must be a non-empty mapping")
        side_strategies: dict[str, str] = {}
        for metric, strategy in side_value.items():
            metric_name = str(metric).strip()
            _require_name(metric_name, f"metric_strategies.{side} metric")
            normalized_strategy = str(strategy).strip().casefold()
            if normalized_strategy not in _SUPPORTED_METRIC_STRATEGIES:
                raise ValueError(
                    f"unsupported metric strategy for {side}.{metric_name}: "
                    f"{normalized_strategy}"
                )
            side_strategies[metric_name] = normalized_strategy
        normalized[side] = side_strategies
    return normalized


def _validate_rows(
    rows: tuple[Mapping[str, Any], ...],
    *,
    side: str,
    time_key: str,
    group_key: str,
) -> None:
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise TypeError(f"{side}_rows[{index}] must be a mapping")
        if time_key not in row or _is_blank(row.get(time_key)):
            raise ValueError(f"{side}_rows[{index}] has no usable {time_key}")
        try:
            hash(row[time_key])
        except TypeError as error:
            raise ValueError(
                f"{side}_rows[{index}].{time_key} must be hashable"
            ) from error
        if group_key not in row:
            raise ValueError(f"{side}_rows[{index}] is missing {group_key}")


def _normalized_group_index(
    rows: tuple[Mapping[str, Any], ...],
    *,
    side: str,
    group_key: str,
    rules: tuple[str, ...],
) -> tuple[dict[str, set[str]], int]:
    index: dict[str, set[str]] = defaultdict(set)
    empty_rows = 0
    for row in rows:
        raw = _group_value(row.get(group_key))
        normalized = _normalize_group(raw, side=side, rules=rules)
        if not normalized:
            empty_rows += 1
            continue
        index[normalized].add(raw)
    return dict(index), empty_rows


def _normalize_group(
    value: str,
    *,
    side: str,
    rules: tuple[str, ...],
) -> str:
    normalized = value
    for rule in rules:
        if rule == "unicode_casefold":
            normalized = normalized.casefold()
        elif rule == "remove_non_alphanumeric":
            normalized = "".join(char for char in normalized if char.isalnum())
        elif rule == "strip_paid_source_prefix_pa" and side == "left":
            if normalized.casefold().startswith("pa"):
                normalized = normalized[2:]
    return normalized


def _aggregate_source_cells(
    rows: tuple[Mapping[str, Any], ...],
    *,
    side: str,
    time_key: str,
    group_key: str,
    strategies: Mapping[str, str],
    raw_to_mapped: Mapping[str, str],
) -> dict[tuple[Any, str], dict[str, Any]]:
    rows_by_source_cell: dict[tuple[Any, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        raw = _group_value(row.get(group_key))
        rows_by_source_cell[(row[time_key], raw)].append(row)

    for (_time_value, _raw_group), source_rows in rows_by_source_cell.items():
        if len(source_rows) <= 1:
            continue
        ratio_metrics = tuple(
            metric for metric, strategy in strategies.items() if strategy == "ratio"
        )
        if ratio_metrics:
            raise ValueError(
                f"{side} ratio metrics require one upstream-aggregated row per "
                "time x group cell: "
                + ", ".join(ratio_metrics)
            )

    aggregated: dict[tuple[Any, str], dict[str, Any]] = {}
    for (time_value, raw_group), source_rows in rows_by_source_cell.items():
        mapped_group = raw_to_mapped.get(raw_group)
        if mapped_group is None:
            continue
        values: dict[str, Any] = {}
        for metric, strategy in strategies.items():
            numeric_values = tuple(
                value
                for row in source_rows
                if (value := _number(row.get(metric))) is not None
            )
            if strategy == "sum":
                values[metric] = fsum(numeric_values) if numeric_values else None
            else:
                values[metric] = numeric_values[0] if numeric_values else None
        aggregated[(time_value, mapped_group)] = values
    return aggregated


def _aligned_rows(
    left_cells: Mapping[tuple[Any, str], Mapping[str, Any]],
    right_cells: Mapping[tuple[Any, str], Mapping[str, Any]],
    *,
    accepted: Mapping[str, tuple[str, str]],
    time_key: str,
    mapped_group_key: str,
    left_metrics: tuple[str, ...],
    right_metrics: tuple[str, ...],
) -> tuple[dict[str, Any], ...]:
    cell_keys = left_cells.keys() | right_cells.keys()
    rows: list[dict[str, Any]] = []
    for time_value, mapped_group in sorted(
        cell_keys,
        key=lambda item: (_sort_token(item[0]), item[1]),
    ):
        left_values = left_cells.get((time_value, mapped_group))
        right_values = right_cells.get((time_value, mapped_group))
        left_group, right_group = accepted[mapped_group]
        row = {
            time_key: time_value,
            mapped_group_key: mapped_group,
            "left_group": left_group,
            "right_group": right_group,
            "left_present": left_values is not None,
            "right_present": right_values is not None,
        }
        row.update(
            {
                f"left_{metric}": (
                    left_values.get(metric) if left_values is not None else None
                )
                for metric in left_metrics
            }
        )
        row.update(
            {
                f"right_{metric}": (
                    right_values.get(metric) if right_values is not None else None
                )
                for metric in right_metrics
            }
        )
        rows.append(row)
    return tuple(rows)


def _metric_coverage(
    rows: tuple[Mapping[str, Any], ...],
    *,
    group_key: str,
    strategies: Mapping[str, str],
    raw_to_mapped: Mapping[str, str],
) -> tuple[dict[str, float | None], dict[str, dict[str, Any]]]:
    coverage: dict[str, float | None] = {}
    detail: dict[str, dict[str, Any]] = {}
    for metric, strategy in strategies.items():
        observations = tuple(
            (_group_value(row.get(group_key)), value)
            for row in rows
            if (value := _number(row.get(metric))) is not None
        )
        matched = tuple(
            value for raw_group, value in observations if raw_group in raw_to_mapped
        )
        if strategy == "sum":
            denominator = fsum(abs(value) for _group, value in observations)
            numerator = fsum(abs(value) for value in matched)
            value = numerator / denominator if denominator else None
            basis = "absolute_metric_mass"
        else:
            denominator = len(observations)
            numerator = len(matched)
            value = numerator / denominator if denominator else None
            basis = "observed_metric_cells"
        coverage[metric] = value
        detail[metric] = {
            "coverage": value,
            "basis": basis,
            "matched": numerator,
            "total": denominator,
        }
    return coverage, detail


def _group_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return numeric if isfinite(numeric) else None


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _sort_token(value: Any) -> tuple[str, str]:
    return type(value).__name__, repr(value)


def _require_name(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
