from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from bi_agent.runtime.contracts import load_contract


_STATUS_PRIORITY = {
    "blocked": 0,
    "degraded": 1,
    "executable": 2,
}


def build_formula_candidate_framework(
    *,
    metric_contract_path: str | Path,
    available_runtime_metrics: Iterable[str] = (),
    available_dimensions: Iterable[str] = (),
    requested_components: Iterable[str] = (),
) -> dict[str, Any]:
    """Build formula candidates from a metric contract without promoting a claim.

    The metric contract owns the candidate set and the runtime projection. User
    wording can rank declared candidates, while it cannot add or remove paths.
    """

    contract_path = Path(metric_contract_path)
    contract = load_contract(contract_path)
    raw_paths = contract.get("decomposition_paths") or ()
    if not isinstance(raw_paths, list):
        raise ValueError("decomposition_paths must be a list")

    available_metrics = _ordered_unique(available_runtime_metrics)
    available_metric_set = set(available_metrics)
    available_dimension_values = _ordered_unique(available_dimensions)
    available_dimension_set = set(available_dimension_values)
    requested = _ordered_unique(requested_components)

    candidates: list[dict[str, Any]] = []
    for contract_order, raw_path in enumerate(raw_paths):
        if not isinstance(raw_path, Mapping):
            raise ValueError("each decomposition path must be a mapping")
        path_id = _required_string(raw_path, "path_id")
        projection = raw_path.get("runtime_projection") or {}
        if not isinstance(projection, Mapping):
            raise ValueError(
                f"decomposition path {path_id!r} runtime_projection must be a mapping"
            )

        declared_components = _string_list(
            raw_path.get("requires_components") or ()
        )
        projected_components = _string_list(
            projection.get("requires_components") or declared_components
        )
        component_bindings = _component_bindings(
            projection.get("component_bindings") or {}
        )
        runtime_components = _ordered_unique(
            component_bindings.get(component_id, component_id)
            for component_id in projected_components
        )

        declared_dimensions = _string_list(
            raw_path.get("requires_dimensions") or ()
        )
        projected_dimensions = _string_list(
            projection.get("requires_dimensions") or declared_dimensions
        )
        dimension_bindings = _component_bindings(
            projection.get("dimension_bindings") or {}
        )
        runtime_dimensions = _ordered_unique(
            dimension_bindings.get(dimension_id, dimension_id)
            for dimension_id in projected_dimensions
        )

        missing_runtime_components = [
            component_id
            for component_id in runtime_components
            if component_id not in available_metric_set
        ]
        missing_dimensions = [
            dimension_id
            for dimension_id in runtime_dimensions
            if dimension_id not in available_dimension_set
        ]
        available_component_count = sum(
            component_id in available_metric_set
            for component_id in runtime_components
        )
        candidate_status = _candidate_status(
            runtime_components=runtime_components,
            missing_runtime_components=missing_runtime_components,
            runtime_dimensions=runtime_dimensions,
            missing_dimensions=missing_dimensions,
            available_component_count=available_component_count,
        )

        related_runtime_metrics = set(runtime_components)
        target_metric = projection.get("target_metric")
        if isinstance(target_metric, str) and target_metric.strip():
            related_runtime_metrics.add(target_metric.strip())
        matched_requested_components = [
            component_id
            for component_id in requested
            if component_id in related_runtime_metrics
        ]

        candidate = {
            "path_id": path_id,
            "ssot_node_id": raw_path.get("ssot_node_id"),
            "expression": raw_path.get("expression"),
            "evidence_type": raw_path.get("evidence_type"),
            "path_role": str(raw_path.get("path_role") or "root"),
            "target_component": raw_path.get("target_component"),
            "contract_ref": str(contract_path),
            "declared_components": declared_components,
            "runtime_components": runtime_components,
            "component_bindings": component_bindings,
            "required_dimensions": runtime_dimensions,
            "missing_runtime_components": missing_runtime_components,
            "missing_dimensions": missing_dimensions,
            "matched_requested_components": matched_requested_components,
            "launch_status": raw_path.get("launch_status"),
            "reconciliation": dict(raw_path.get("reconciliation") or {}),
            "runtime_projection": dict(projection),
            "candidate_status": candidate_status,
            "candidate_role": "auxiliary_candidate",
            "_contract_order": contract_order,
        }
        candidates.append(candidate)

    ranked = sorted(candidates, key=_candidate_rank_key, reverse=True)
    for candidate_rank, candidate in enumerate(ranked, start=1):
        candidate["candidate_rank"] = candidate_rank
    primary_candidate = next(
        (
            candidate
            for candidate in ranked
            if str(candidate.get("path_role") or "") == "root"
        ),
        None,
    )
    if primary_candidate is not None:
        primary_candidate["candidate_role"] = "primary_candidate"

    for candidate in candidates:
        candidate.pop("_contract_order", None)

    return {
        "metric_id": contract.get("metric_id"),
        "metric_contract_ref": str(contract_path),
        "selection_state": "candidate_only",
        "primary_formula": None,
        "primary_candidate_path_id": (
            primary_candidate["path_id"]
            if primary_candidate is not None
            else None
        ),
        "available_runtime_metrics": available_metrics,
        "available_dimensions": available_dimension_values,
        "requested_components": requested,
        "candidates": candidates,
    }


def _candidate_status(
    *,
    runtime_components: list[str],
    missing_runtime_components: list[str],
    runtime_dimensions: list[str],
    missing_dimensions: list[str],
    available_component_count: int,
) -> str:
    if missing_dimensions:
        return "blocked"
    if not missing_runtime_components:
        return "executable"
    if available_component_count:
        return "degraded"
    if not runtime_components and runtime_dimensions:
        return "executable"
    return "blocked"


def _candidate_rank_key(candidate: Mapping[str, Any]) -> tuple[int, int, int, int, int]:
    status_priority = _STATUS_PRIORITY.get(
        str(candidate.get("candidate_status")),
        -1,
    )
    root_priority = int(str(candidate.get("path_role")) == "root")
    requested_match_count = len(
        candidate.get("matched_requested_components") or ()
    )
    launch_priority = _launch_priority(candidate.get("launch_status"))
    contract_order = int(candidate.get("_contract_order") or 0)
    return (
        status_priority,
        root_priority,
        requested_match_count,
        launch_priority,
        -contract_order,
    )


def _launch_priority(value: Any) -> int:
    normalized = str(value or "").strip().lower()
    if normalized.startswith("available"):
        return 2
    if normalized.startswith("evidence_linked"):
        return 1
    return 0


def _component_bindings(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("runtime projection bindings must be a mapping")
    output: dict[str, str] = {}
    for component_id, raw_binding in value.items():
        source_id = str(component_id).strip()
        if isinstance(raw_binding, Mapping):
            raw_binding = raw_binding.get("runtime_metric_id")
        runtime_id = str(raw_binding or "").strip()
        if source_id and runtime_id:
            output[source_id] = runtime_id
    return output


def _required_string(value: Mapping[str, Any], key: str) -> str:
    output = str(value.get(key) or "").strip()
    if not output:
        raise ValueError(f"decomposition path must declare {key}")
    return output


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, Iterable):
        raise ValueError("formula candidate list field must be iterable")
    return _ordered_unique(value)


def _ordered_unique(values: Iterable[Any]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        output.append(normalized)
    return output
