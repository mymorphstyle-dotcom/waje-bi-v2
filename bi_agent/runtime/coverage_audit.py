from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from bi_agent.runtime.current_data_coverage import current_data_coverage_cases
from bi_agent.runtime.dataset_catalog import (
    DatasetSnapshot,
    dataset_release_authority_integrity_errors,
    snapshot_matches_release_authority,
)
from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry


COVERAGE_STATES = (
    "executable",
    "degraded",
    "source_unbound",
    "contract_partial",
    "snapshot_unavailable_as_of",
)


def audit_existing_data_coverage(
    registry: RuntimeContractRegistry,
    snapshot_records: Sequence[Any],
    release_resolver: Any,
    as_of: datetime,
) -> dict[str, Any]:
    if type(registry) is not RuntimeContractRegistry:
        raise ValueError("coverage_registry_invalid")
    if as_of.tzinfo is None:
        raise ValueError("coverage_context_invalid")
    snapshots = tuple(_snapshot(item) for item in snapshot_records)
    _validate_release_authority(snapshots, release_resolver)
    cases = current_data_coverage_cases(registry)
    families = _capability_families(registry)
    cells: dict[str, dict[str, Any]] = {}
    for capability_id in sorted(registry.capability_ids):
        contract = registry.capability_inputs(capability_id)
        datasets = _datasets(registry, contract)
        for dataset_id in datasets:
            state, selected = _state(
                registry, cases, contract, dataset_id, snapshots, as_of
            )
            owner = _owner(state, dataset_id)
            cells[f"{capability_id}:{dataset_id}"] = {
                "question_families": list(families.get(capability_id, ())),
                "capability": capability_id,
                "datasets": [dataset_id],
                "metrics": list(_metrics(registry, contract, dataset_id)),
                "dimensions": list(_dimensions(registry, contract, dataset_id)),
                "windows": sorted(str(item) for item in contract.get("required_windows", ())),
                "evidence_types": sorted(str(item) for item in contract.get("supported_evidence_types", ())),
                "claim_ceiling": str(contract.get("maximum_claim_strength") or "insufficient"),
                "current_release_refs": sorted({item.release_ref for item in selected if item.release_ref}),
                "current_releases": [
                    {
                        "dataset_id": item.dataset_id,
                        "snapshot_ref": item.snapshot_ref,
                        "release_ref": item.release_ref,
                        "load_revision": item.load_revision,
                        "schema_fingerprint": item.schema_fingerprint,
                        "evidence_state": item.evidence_state,
                    }
                    for item in sorted(selected, key=lambda value: value.snapshot_ref)
                ],
                "state": state,
                "owner": owner,
                "impact": _impact(state, capability_id, dataset_id),
                "next_action": _next_action(state, dataset_id),
            }
    ordered = {key: cells[key] for key in sorted(cells)}
    summary = {state: sum(cell["state"] == state for cell in ordered.values()) for state in COVERAGE_STATES}
    return {
        "schema_version": "existing-data-coverage-v2",
        "as_of": as_of.isoformat(),
        "registry_contract_version": registry.contract_version,
        "states": list(COVERAGE_STATES),
        "summary": summary,
        "cells": ordered,
    }


def _snapshot(value: Any) -> DatasetSnapshot:
    value = getattr(value, "snapshot", value)
    if isinstance(value, DatasetSnapshot):
        return value
    if isinstance(value, Mapping):
        fields = DatasetSnapshot.__dataclass_fields__
        try:
            return DatasetSnapshot(**{key: value[key] for key in fields if key in value})
        except TypeError as exc:
            raise ValueError("coverage_snapshot_invalid") from exc
    raise ValueError("coverage_snapshot_invalid")


def _validate_release_authority(snapshots: tuple[DatasetSnapshot, ...], resolver: Any) -> None:
    for snapshot in snapshots:
        if snapshot.status != "active":
            continue
        try:
            authority = resolver.resolve_dataset_release(snapshot.release_ref)
        except Exception as exc:
            raise ValueError(f"coverage_release_resolver:{snapshot.dataset_id}") from exc
        if dataset_release_authority_integrity_errors(authority):
            raise ValueError(f"coverage_release_integrity:{snapshot.dataset_id}")
        if not snapshot_matches_release_authority(snapshot, authority):
            raise ValueError(f"coverage_release_membership:{snapshot.dataset_id}")


def _state(registry, cases, contract, dataset_id, snapshots, as_of):
    candidates = tuple(item for item in snapshots if item.dataset_id == dataset_id and item.status == "active")
    if not candidates:
        return "source_unbound", ()
    available = tuple(item for item in candidates if _loaded_at(item) <= as_of.astimezone(timezone.utc))
    if not available:
        return "snapshot_unavailable_as_of", candidates
    selected = (max(available, key=lambda item: (_loaded_at(item), item.snapshot_ref)),)
    if selected[0].evidence_state != "claim_ready":
        return "degraded", selected
    query_families = set(str(item) for item in contract.get("query_families", ()))
    supported = any(
        dataset_id in case.dataset_ids
        and case.expected_state == "supported"
        and case.query_family in query_families
        for case in cases
    )
    required_metrics = set(str(item) for item in contract.get("required_metrics", ()))
    bound_metrics = set(_metrics(registry, contract, dataset_id))
    if query_families and (not supported or not required_metrics <= bound_metrics):
        return "contract_partial", selected
    return "executable", selected


def _loaded_at(snapshot: DatasetSnapshot) -> datetime:
    parsed = datetime.fromisoformat(snapshot.loaded_at.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("coverage_snapshot_loaded_at_invalid")
    return parsed.astimezone(timezone.utc)


def _metrics(registry, contract, dataset_id):
    requested = set(str(item) for item in (*contract.get("required_metrics", ()), *contract.get("allowed_metrics", ()), *contract.get("optional_metrics", ())))
    return tuple(sorted(item for item in requested if dataset_id in registry.metric_sources(item)))


def _datasets(registry, contract):
    configured = tuple(str(item) for item in contract.get("allowed_datasets", ()))
    if configured:
        return tuple(sorted(configured))
    if contract.get("source_mode") == "requested_context_sources":
        return tuple(
            sorted(
                dataset_id
                for dataset_id in registry.dataset_ids
                if {"event_id", "event_start_date", "source_family"}
                <= set(registry.dataset(dataset_id).get("schema_fields", ()))
            )
        )
    return ()


def _dimensions(registry, contract, dataset_id):
    if not contract.get("dimension_mode"):
        return ()
    return tuple(sorted(item for item in registry._payload["dimensions"] if dataset_id in registry.dimension_sources(item)))


def _capability_families(registry):
    output = {item: set() for item in registry.capability_ids}
    for family in registry.question_family_ids:
        obligation = registry.question_family_obligation(family)
        values = [*obligation.get("required_capabilities", ()), *obligation.get("independent_capabilities", ())]
        for rule in obligation.get("conditional_rules", ()):
            values.extend(rule.get("add", ()))
        for capability in values:
            if capability in output:
                output[capability].add(family)
    return {key: tuple(sorted(value)) for key, value in output.items()}


def _owner(state, dataset_id):
    if state == "contract_partial":
        return "analysis_contract_owner"
    if state in {"source_unbound", "snapshot_unavailable_as_of"}:
        return "data_operations_owner"
    if state == "degraded":
        return "data_quality_owner"
    return "runtime_owner"


def _impact(state, capability, dataset):
    if state == "snapshot_unavailable_as_of":
        return (
            f"no authoritative {dataset} release was visible at the audit as_of; "
            f"{capability} cannot be assessed for that transaction-time boundary"
        )
    return "current capability path is executable" if state == "executable" else f"{capability} cannot publish at its configured claim ceiling from {dataset}"


def _next_action(state, dataset):
    actions = {
        "executable": "retain release and contract monitoring",
        "degraded": f"review {dataset} evidence readiness",
        "source_unbound": f"publish an authoritative {dataset} snapshot release",
        "contract_partial": f"complete the reviewed query contract for {dataset}",
        "snapshot_unavailable_as_of": (
            f"select an existing {dataset} release with loaded_at at or before the audit as_of; "
            "if current coverage is intended, advance the audit as_of to the current authority visible time; "
            "otherwise record that no historical authority exists"
        ),
    }
    return actions[state]
