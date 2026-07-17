from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
import re
from typing import Any
from zoneinfo import ZoneInfo

from bi_agent.runtime.analysis_contracts import (
    AnalysisContract,
    analysis_contract_from_dict,
    analysis_contract_signature,
    query_contract_signature,
    stable_contract_signature,
)
from bi_agent.runtime.baseline_semantics import (
    BaselineSemanticError,
    CANONICAL_BASELINE_IDS,
    canonical_baseline_ids,
)
from bi_agent.runtime.evidence_authority import (
    EvidenceIntegrityError,
    canonical_digest,
    canonical_value,
)
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)
from bi_agent.conversation.clarification_options import (
    clarification_labels_match,
)


_MATERIAL_AUTHORITY_KEYS = frozenset(
    {
        "schema_version",
        "source_run_id",
        "thread_id",
        "topic_id",
        "intent_material",
        "route_material_slots",
        "execution_material",
        "route_control",
        "material_authority_signature",
    }
)
_ROUTE_CONTROL_KEYS = frozenset({"obligation_rejection_history"})
_OBLIGATION_REJECTION_KEYS = frozenset({"action", "capability", "reason"})
_LOCAL_OBLIGATION_REJECTION_REASONS = frozenset(
    {
        "diagnostic_question_family_incompatible",
        "unknown_diagnostic_rejected",
    }
)
_INTENT_MATERIAL_KEYS = frozenset(
    {
        "primary_question_family",
        "question_families",
        "primary_target_metric",
        "target_metrics",
        "goal_bindings",
        "explicit_focus",
        "component_ids",
        "association_metric_ids",
        "dimension_ids",
        "baselines",
        "context_sources",
        "claim_types",
        "required_outcomes",
        "analysis_axis_ids",
        "scope",
        "time_window",
    }
)
_ROUTE_MATERIAL_KEYS = frozenset(
    {
        "target_metrics",
        "component_ids",
        "association_metric_ids",
        "dimension_ids",
        "baselines",
        "context_sources",
        "claim_types",
        "required_outcomes",
        "analysis_axis_ids",
        "diagnostic_tags",
        "scope",
    }
)
_MATERIAL_LIST_AXES = (
    "target_metrics",
    "component_ids",
    "association_metric_ids",
    "dimension_ids",
    "baselines",
    "context_sources",
    "claim_types",
    "required_outcomes",
    "analysis_axis_ids",
)
_EXECUTION_MATERIAL_KEYS = frozenset(
    {
        "schema_version",
        "target_semantic",
        "as_of",
        "business_timezone",
        "context_window_specs",
        "fixed_window_bounds",
        "filters",
        "grain",
        "dataset_requirements",
        "metric_dataset_overrides",
        "dimension_dataset_overrides",
        "requested_context_sources",
        "accepted_graph",
        "runtime_contract_version",
        "runtime_registry_digest",
        "run_mode_class",
        "source_query_contracts",
    }
)
_SOURCE_QUERY_CONTRACT_KEYS = frozenset(
    {
        "contract_signature",
        "dataset_snapshot_refs",
        "owner_capability_ids",
    }
)
_FIXED_WINDOW_IDS = (
    "target_day",
    "previous_day",
    "rolling_7_day_baseline",
    "same_weekday_last_week",
    "pattern_history",
    "anomaly_history",
)
_ANALYSIS_CONTEXT_WINDOW_FIELDS = {
    "target_date": ("target_day", 0),
    "previous_day": ("previous_day", 0),
    "rolling_7_day_start": ("rolling_7_day_baseline", 0),
    "rolling_7_day_end": ("rolling_7_day_baseline", 1),
    "same_weekday_last_week": ("same_weekday_last_week", 0),
    "pattern_history_start": ("pattern_history", 0),
    "anomaly_history_start": ("anomaly_history", 0),
}
_COMPLETED_MATERIAL_AUTHORITY_RECORD_KEYS = frozenset(
    {
        "schema_version",
        "source_run_id",
        "thread_id",
        "topic_id",
        "analysis_contract_ref",
        "analysis_contract_signature",
        "analysis_contract_digest",
        "material_authority",
        "material_authority_digest",
        "record_digest",
    }
)
_RESOLVED_COMPLETED_MATERIAL_AUTHORITY_KEYS = frozenset(
    {
        "source_run_id",
        "thread_id",
        "topic_id",
        "analysis_contract",
        "analysis_contract_signature",
        "material_authority",
    }
)
_PRIOR_TOPIC_MATERIAL_CONTEXT_KEYS = frozenset(
    {
        "schema_version",
        "thread_id",
        "topic_id",
        "source_run_ids",
        "source_result_refs",
        "material_projection",
        "authorities",
        "context_digest",
    }
)
_PRIOR_TOPIC_MATERIAL_PROJECTION_KEYS = frozenset(
    {"intent_material", "route_material_slots"}
)


def build_material_authority(
    *,
    source_run_id: str,
    thread_id: str,
    topic_id: str,
    original_intent: Mapping[str, Any],
    material_slots: Mapping[str, Any],
    runtime_material: Mapping[str, Any] | None = None,
    obligation_rejection_history: Any = (),
) -> dict[str, Any]:
    if not isinstance(original_intent, Mapping):
        raise EvidenceIntegrityError("material_authority_original_intent_invalid")
    if not isinstance(material_slots, Mapping):
        raise EvidenceIntegrityError("material_authority_route_invalid")
    route_material = _route_material_projection(material_slots)
    intent_material = _intent_material_projection(
        original_intent,
        route_material=route_material,
    )
    body = {
        "schema_version": "4",
        "source_run_id": _required(source_run_id, "source_run_id"),
        "thread_id": _required(thread_id, "thread_id"),
        "topic_id": _required(topic_id, "topic_id"),
        "intent_material": intent_material,
        "route_material_slots": route_material,
        "execution_material": (
            _execution_material_projection(runtime_material)
            if runtime_material is not None
            else None
        ),
        "route_control": _route_control_projection(
            obligation_rejection_history
        ),
    }
    return {
        **body,
        "material_authority_signature": stable_contract_signature(body),
    }


def validate_material_authority(
    value: Mapping[str, Any],
    *,
    source_run_id: str,
    thread_id: str,
    topic_id: str,
    require_execution_material: bool = False,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _MATERIAL_AUTHORITY_KEYS:
        raise EvidenceIntegrityError("material_authority_shape_invalid")
    if str(value.get("schema_version") or "") != "4":
        raise EvidenceIntegrityError("material_authority_version_invalid")
    if (
        str(value.get("source_run_id") or "") != source_run_id
        or str(value.get("thread_id") or "") != thread_id
        or str(value.get("topic_id") or "") != topic_id
    ):
        raise EvidenceIntegrityError("material_authority_owner_mismatch")
    intent_material = value.get("intent_material")
    if (
        not isinstance(intent_material, Mapping)
        or set(intent_material) != _INTENT_MATERIAL_KEYS
    ):
        raise EvidenceIntegrityError("material_authority_intent_shape_invalid")
    route_material = value.get("route_material_slots")
    if (
        not isinstance(route_material, Mapping)
        or set(route_material) != _ROUTE_MATERIAL_KEYS
    ):
        raise EvidenceIntegrityError("material_authority_route_shape_invalid")
    route_control = value.get("route_control")
    if (
        not isinstance(route_control, Mapping)
        or set(route_control) != _ROUTE_CONTROL_KEYS
    ):
        raise EvidenceIntegrityError(
            "material_authority_route_control_shape_invalid"
        )
    _validate_intent_material(intent_material)
    _validate_route_material(route_material)
    raw_execution_material = value.get("execution_material")
    if raw_execution_material is None:
        if require_execution_material:
            raise EvidenceIntegrityError(
                "material_authority_execution_material_missing"
            )
    else:
        _execution_material_projection(raw_execution_material)
    _route_control_projection(
        route_control.get("obligation_rejection_history")
    )
    signature = str(value.get("material_authority_signature") or "")
    body = {
        key: canonical_value(item)
        for key, item in value.items()
        if key != "material_authority_signature"
    }
    if not signature or signature != stable_contract_signature(body):
        raise EvidenceIntegrityError("material_authority_signature_invalid")
    return {**body, "material_authority_signature": signature}


def build_execution_material(
    *,
    proposal: Mapping[str, Any],
    accepted_graph: Iterable[str],
    as_of: str | datetime,
    run_mode: str,
    runtime_contract_version: str,
    runtime_registry_digest: str,
    analysis_contract: AnalysisContract | Mapping[str, Any],
    query_contracts: Iterable[Mapping[str, Any] | Any],
    capability_execution_plans: Iterable[Mapping[str, Any] | Any] = (),
) -> dict[str, Any]:
    typed_contract = _typed_analysis_contract(analysis_contract)
    contract_runtime = _contract_runtime_projection(typed_contract)
    if not _same_as_of(as_of, contract_runtime["as_of"]):
        raise EvidenceIntegrityError("execution_material_as_of_mismatch")
    raw_proposal = dict(proposal)
    canonical_accepted_graph = _exact_string_values(
        accepted_graph,
        reason="execution_material_accepted_graph_invalid",
    )
    material = {
        "schema_version": "3",
        **contract_runtime,
        "context_window_specs": _canonical_context_window_specs(
            raw_proposal.get("context_window_specs")
        ),
        "filters": _canonical_filters(raw_proposal.get("filters")),
        "grain": canonical_execution_grain(raw_proposal.get("grain")),
        "dataset_requirements": list(
            _exact_string_values(
                raw_proposal.get("dataset_requirements"),
                reason="execution_material_dataset_requirements_invalid",
            )
        ),
        "metric_dataset_overrides": _canonical_overrides(
            raw_proposal.get("metric_dataset_overrides"),
            reason="execution_material_metric_dataset_overrides_invalid",
        ),
        "dimension_dataset_overrides": _canonical_overrides(
            raw_proposal.get("dimension_dataset_overrides"),
            reason="execution_material_dimension_dataset_overrides_invalid",
        ),
        "requested_context_sources": list(
            _exact_string_values(
                raw_proposal.get("requested_context_sources"),
                reason="execution_material_requested_context_sources_invalid",
            )
        ),
        "accepted_graph": list(canonical_accepted_graph),
        "runtime_contract_version": _required_execution_token(
            runtime_contract_version,
            "runtime_contract_version",
        ),
        "runtime_registry_digest": _required_execution_token(
            runtime_registry_digest,
            "runtime_registry_digest",
        ),
        "run_mode_class": _run_mode_class(run_mode),
        "source_query_contracts": _source_query_contract_projection(
            query_contracts,
            capability_execution_plans=capability_execution_plans,
            accepted_graph=canonical_accepted_graph,
        ),
    }
    return _execution_material_projection(material)


def _typed_analysis_contract(
    value: AnalysisContract | Mapping[str, Any],
) -> AnalysisContract:
    if isinstance(value, AnalysisContract):
        return value
    if not isinstance(value, Mapping):
        raise EvidenceIntegrityError("execution_material_contract_invalid")
    payload = dict(value)
    payload.pop("contract_signature", None)
    try:
        return analysis_contract_from_dict(payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise EvidenceIntegrityError(
            "execution_material_contract_invalid"
        ) from exc


def _contract_runtime_projection(
    contract: AnalysisContract,
) -> dict[str, Any]:
    as_of = _canonical_as_of(contract.as_of)
    timezone_name = _required_execution_token(
        contract.business_timezone,
        "business_timezone",
    )
    try:
        ZoneInfo(timezone_name)
    except (KeyError, ValueError) as exc:
        raise EvidenceIntegrityError(
            "execution_material_business_timezone_invalid"
        ) from exc
    windows: dict[str, list[str]] = {}
    legacy_windows: list[tuple[str, str, date, date]] = []
    for window in contract.resolved_windows:
        window_id = str(window.window_id or "")
        if (
            window_id not in {*_FIXED_WINDOW_IDS, "target", "baseline"}
            and not _is_context_window_id(window_id)
        ):
            raise EvidenceIntegrityError(
                "execution_material_fixed_window_bounds_invalid"
            )
        try:
            start = date.fromisoformat(window.start_inclusive)
            end_exclusive = date.fromisoformat(window.end_exclusive)
        except ValueError as exc:
            raise EvidenceIntegrityError(
                "execution_material_fixed_window_bounds_invalid"
            ) from exc
        end = end_exclusive - timedelta(days=1)
        if start > end or str(window.timezone or "") != timezone_name:
            raise EvidenceIntegrityError(
                "execution_material_fixed_window_bounds_invalid"
            )
        if _is_context_window_id(window_id) and (
            str(window.role or "") != "reference"
            or tuple(window.capability_refs)
            != (window_id.split("__", 2)[1],)
        ):
            raise EvidenceIntegrityError(
                "execution_material_fixed_window_bounds_invalid"
            )
        if window_id in _FIXED_WINDOW_IDS or _is_context_window_id(window_id):
            if window_id in windows:
                raise EvidenceIntegrityError(
                    "execution_material_fixed_window_bounds_invalid"
                )
            windows[window_id] = [start.isoformat(), end.isoformat()]
        else:
            legacy_windows.append(
                (window_id, str(window.role or ""), start, end)
            )
    for window_id, role, start, end in legacy_windows:
        if (
            window_id != "target"
            or role != "target"
            or start != end
            or "target_day" in windows
        ):
            continue
        windows["target_day"] = [start.isoformat(), end.isoformat()]
    target = windows.get("target_day")
    if target is None or target[0] != target[1]:
        raise EvidenceIntegrityError(
            "execution_material_target_semantic_invalid"
        )
    target_day = date.fromisoformat(target[0])
    for window_id, role, start, end in legacy_windows:
        if window_id == "target" and role == "target" and start == end:
            continue
        canonical_id = ""
        if window_id == "baseline" and role == "baseline":
            if start == end == target_day - timedelta(days=1):
                canonical_id = "previous_day"
            elif start == end == target_day - timedelta(days=7):
                canonical_id = "same_weekday_last_week"
            elif (
                start == target_day - timedelta(days=7)
                and end == target_day - timedelta(days=1)
            ):
                canonical_id = "rolling_7_day_baseline"
        if not canonical_id or canonical_id in windows:
            raise EvidenceIntegrityError(
                "execution_material_fixed_window_bounds_invalid"
            )
        windows[canonical_id] = [start.isoformat(), end.isoformat()]
    return {
        "target_semantic": target[0],
        "as_of": as_of,
        "business_timezone": timezone_name,
        "fixed_window_bounds": {
            window_id: windows[window_id]
            for window_id in (
                *_FIXED_WINDOW_IDS,
                *sorted(
                    item
                    for item in windows
                    if item not in _FIXED_WINDOW_IDS
                ),
            )
            if window_id in windows
        },
    }


def _query_contract_projection(
    values: Iterable[Mapping[str, Any] | Any],
) -> tuple[dict[str, Any], ...]:
    if (
        isinstance(values, (str, bytes, Mapping))
        or not isinstance(values, Iterable)
    ):
        raise EvidenceIntegrityError(
            "execution_material_source_query_contracts_invalid"
        )
    output: list[dict[str, Any]] = []
    query_ids: set[str] = set()
    for value in values:
        if isinstance(value, Mapping):
            contract_value: Mapping[str, Any] | Any = dict(value)
            embedded = str(value.get("contract_signature") or "")
            snapshot_refs = value.get("dataset_snapshot_refs")
            query_id = str(value.get("query_contract_id") or "")
        elif all(
            hasattr(value, field)
            for field in (
                "query_contract_id",
                "contract_signature",
                "dataset_snapshot_refs",
            )
        ):
            contract_value = value
            embedded = str(getattr(value, "contract_signature") or "")
            snapshot_refs = getattr(value, "dataset_snapshot_refs")
            query_id = str(getattr(value, "query_contract_id") or "")
        else:
            raise EvidenceIntegrityError(
                "execution_material_source_query_contracts_invalid"
            )
        if not query_id or query_id != query_id.strip() or query_id in query_ids:
            raise EvidenceIntegrityError(
                "execution_material_source_query_contracts_invalid"
            )
        query_ids.add(query_id)
        signature = query_contract_signature(contract_value)
        if embedded and embedded != signature:
            raise EvidenceIntegrityError(
                "execution_material_source_query_contracts_invalid"
            )
        snapshots = _exact_string_values(
            snapshot_refs,
            reason="execution_material_source_query_contracts_invalid",
        )
        record = {
            "query_contract_id": query_id,
            "contract_signature": signature,
            "dataset_snapshot_refs": list(snapshots),
        }
        output.append(record)
    return tuple(output)


def _current_query_contract_projection(
    values: Iterable[Mapping[str, Any] | Any],
) -> dict[str, tuple[str, ...]]:
    output: dict[str, tuple[str, ...]] = {}
    for record in _query_contract_projection(values):
        signature = str(record["contract_signature"])
        snapshots = tuple(record["dataset_snapshot_refs"])
        existing = output.get(signature)
        if existing is not None and existing != snapshots:
            raise EvidenceIntegrityError(
                "execution_material_source_query_contracts_invalid"
            )
        output[signature] = snapshots
    return output


def _source_query_contract_projection(
    values: Iterable[Mapping[str, Any] | Any],
    *,
    capability_execution_plans: Iterable[Mapping[str, Any] | Any],
    accepted_graph: Iterable[str],
) -> list[dict[str, Any]]:
    query_records = _query_contract_projection(values)
    graph = _exact_string_values(
        accepted_graph,
        reason="execution_material_accepted_graph_invalid",
    )
    owners_by_query_id = _query_owner_capability_ids(
        capability_execution_plans,
        query_ids=tuple(
            str(record["query_contract_id"]) for record in query_records
        ),
        accepted_graph=graph,
    )
    owners_by_signature: dict[str, set[str]] = {}
    snapshots_by_signature: dict[str, tuple[str, ...]] = {}
    for record in query_records:
        query_id = str(record["query_contract_id"])
        signature = str(record["contract_signature"])
        snapshots = tuple(record["dataset_snapshot_refs"])
        existing = snapshots_by_signature.get(signature)
        if existing is not None and existing != snapshots:
            raise EvidenceIntegrityError(
                "execution_material_source_query_contracts_invalid"
            )
        snapshots_by_signature[signature] = snapshots
        owners_by_signature.setdefault(signature, set()).update(
            owners_by_query_id[query_id]
        )
    return [
        {
            "contract_signature": signature,
            "dataset_snapshot_refs": list(snapshots_by_signature[signature]),
            "owner_capability_ids": [
                capability
                for capability in graph
                if capability in owners_by_signature[signature]
            ],
        }
        for signature in sorted(snapshots_by_signature)
    ]


def _query_owner_capability_ids(
    values: Iterable[Mapping[str, Any] | Any],
    *,
    query_ids: tuple[str, ...],
    accepted_graph: tuple[str, ...],
) -> dict[str, tuple[str, ...]]:
    reason = "execution_material_source_query_contracts_invalid"
    if (
        isinstance(values, (str, bytes, Mapping))
        or not isinstance(values, Iterable)
    ):
        raise EvidenceIntegrityError(reason)
    known_query_ids = set(query_ids)
    owners: dict[str, set[str]] = {
        query_id: set() for query_id in query_ids
    }
    seen_capabilities: set[str] = set()
    for plan in values:
        if isinstance(plan, Mapping):
            capability_id = plan.get("capability_id")
        else:
            capability_id = getattr(plan, "capability_id", None)
        if (
            not isinstance(capability_id, str)
            or not capability_id
            or capability_id != capability_id.strip()
            or capability_id in seen_capabilities
            or capability_id not in accepted_graph
        ):
            raise EvidenceIntegrityError(reason)
        seen_capabilities.add(capability_id)
        for slot_field in ("required_input_slots", "optional_input_slots"):
            slots = (
                plan.get(slot_field)
                if isinstance(plan, Mapping)
                else getattr(plan, slot_field, None)
            )
            if (
                isinstance(slots, (str, bytes, Mapping))
                or not isinstance(slots, Iterable)
            ):
                raise EvidenceIntegrityError(reason)
            for slot in slots:
                if not isinstance(slot, Mapping) and not all(
                    hasattr(slot, field)
                    for field in (
                        "query_contract_refs",
                        "validation_query_contract_refs",
                    )
                ):
                    raise EvidenceIntegrityError(reason)
                for ref_field in (
                    "query_contract_refs",
                    "validation_query_contract_refs",
                ):
                    refs = (
                        slot.get(ref_field)
                        if isinstance(slot, Mapping)
                        else getattr(slot, ref_field)
                    )
                    for query_id in _exact_string_values(
                        refs,
                        reason=reason,
                    ):
                        if query_id not in known_query_ids:
                            raise EvidenceIntegrityError(reason)
                        owners[query_id].add(capability_id)
    if any(not query_owners for query_owners in owners.values()):
        raise EvidenceIntegrityError(reason)
    return {
        query_id: tuple(
            capability
            for capability in accepted_graph
            if capability in owners[query_id]
        )
        for query_id in query_ids
    }


def validate_material_authority_contract_overlap(
    material_authority: Mapping[str, Any],
    analysis_contract: AnalysisContract,
    *,
    runtime_registry: RuntimeContractRegistry | None = None,
) -> None:
    intent_material = material_authority.get("intent_material")
    route_material = material_authority.get("route_material_slots")
    if not isinstance(intent_material, Mapping) or not isinstance(
        route_material, Mapping
    ):
        raise EvidenceIntegrityError("material_authority_shape_invalid")
    execution_material = material_authority.get("execution_material")
    if not isinstance(execution_material, Mapping):
        raise EvidenceIntegrityError(
            "material_authority_execution_material_missing"
        )
    runtime_registry = runtime_registry or RuntimeContractRegistry.from_path(
        CANONICAL_RUNTIME_BINDINGS_PATH
    )
    if (
        execution_material.get("runtime_contract_version")
        != runtime_registry.contract_version
        or execution_material.get("runtime_registry_digest")
        != runtime_registry.source_payload_digest
    ):
        raise EvidenceIntegrityError(
            "material_authority_contract_runtime_registry_mismatch"
        )
    _validate_goal_plan_material_overlap(
        intent_material,
        route_material,
        runtime_registry,
    )
    if tuple(intent_material.get("question_families") or ()) != tuple(
        analysis_contract.question_families
    ):
        raise EvidenceIntegrityError(
            "material_authority_contract_question_families_mismatch"
        )
    unresolved_target_refs = {
        contract_ref
        for contract_ref in analysis_contract.target_metric_refs
        if not any(
            binding.contract_ref == contract_ref
            for binding in analysis_contract.metric_bindings
        )
    }
    if unresolved_target_refs:
        try:
            from bi_agent.runtime.runtime_persistence import (
                _validate_analysis_target_metric_refs,
            )

            _validate_analysis_target_metric_refs(analysis_contract)
        except EvidenceIntegrityError as exc:
            raise EvidenceIntegrityError(
                "material_authority_contract_target_metrics_unresolvable"
            ) from exc
    intent_target_metrics = tuple(intent_material.get("target_metrics") or ())
    route_target_metrics = tuple(route_material.get("target_metrics") or ())
    if intent_target_metrics != route_target_metrics:
        raise EvidenceIntegrityError(
            "material_authority_contract_target_metrics_mismatch"
        )
    contract_target_metrics = _contract_target_metric_ids(
        analysis_contract,
        runtime_registry=runtime_registry,
        material_target_metric_ids=intent_target_metrics,
    )
    if any(
        tuple(targets or ()) != contract_target_metrics
        for targets in (
            intent_material.get("target_metrics"),
            route_material.get("target_metrics"),
        )
    ):
        raise EvidenceIntegrityError(
            "material_authority_contract_target_metrics_mismatch"
        )
    route_component_ids = tuple(route_material.get("component_ids") or ())
    route_association_metric_ids = tuple(
        route_material.get("association_metric_ids") or ()
    )
    contract_requested_metric_ids = tuple(
        analysis_contract.scope.get("requested_metric_ids") or ()
    )
    expected_requested_metric_ids = tuple(
        dict.fromkeys(
            (
                *route_target_metrics,
                *route_component_ids,
                *route_association_metric_ids,
            )
        )
    )
    if contract_requested_metric_ids != expected_requested_metric_ids:
        raise EvidenceIntegrityError(
            "material_authority_contract_component_ids_mismatch"
        )
    route_dimension_ids = tuple(route_material.get("dimension_ids") or ())
    if tuple(
        analysis_contract.scope.get("requested_dimension_ids") or ()
    ) != route_dimension_ids:
        raise EvidenceIntegrityError(
            "material_authority_contract_dimension_ids_mismatch"
        )
    route_claim_types = tuple(route_material.get("claim_types") or ())
    contract_claim_types = tuple(analysis_contract.claim_intents)
    gap_claim_types = {
        claim_type
        for gap in analysis_contract.contract_gaps
        for claim_type in gap.affected_claim_types
    }
    if (
        any(
            claim_type not in set(contract_claim_types) | gap_claim_types
            for claim_type in route_claim_types
        )
        or any(
            claim_type != "unbound_claim_intent"
            and claim_type not in set(route_claim_types)
            for claim_type in contract_claim_types
        )
    ):
        raise EvidenceIntegrityError(
            "material_authority_contract_claim_types_mismatch"
        )
    contract_scope = _contract_material_scope(analysis_contract.scope)
    if any(
        _material_scope(scope) != contract_scope
        for scope in (
            intent_material.get("scope"),
            route_material.get("scope"),
        )
    ):
        raise EvidenceIntegrityError(
            "material_authority_contract_scope_mismatch"
        )
    expected_runtime = _contract_runtime_projection(analysis_contract)
    for axis in (
        "target_semantic",
        "as_of",
        "business_timezone",
        "fixed_window_bounds",
    ):
        if canonical_value(execution_material.get(axis)) != canonical_value(
            expected_runtime[axis]
        ):
            raise EvidenceIntegrityError(
                f"material_authority_contract_{axis}_mismatch"
            )
    contract_capabilities = set(analysis_contract.capability_requirements)
    if any(
        capability not in contract_capabilities
        for capability in execution_material.get("accepted_graph") or ()
    ):
        raise EvidenceIntegrityError(
            "material_authority_contract_accepted_graph_mismatch"
        )


def preflight_completed_material_authority(
    *,
    material_authority: Mapping[str, Any],
    analysis_contract: Mapping[str, Any],
    run_id: str,
    thread_id: str,
    topic_id: str,
    runtime_registry: RuntimeContractRegistry | None = None,
) -> tuple[str, ...]:
    if not isinstance(analysis_contract, Mapping):
        raise EvidenceIntegrityError(
            "completed_material_authority_contract_invalid"
        )
    contract_payload = dict(analysis_contract)
    embedded_signature = str(
        contract_payload.pop("contract_signature", "") or ""
    )
    try:
        typed_contract = analysis_contract_from_dict(contract_payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise EvidenceIntegrityError(
            "completed_material_authority_contract_invalid"
        ) from exc
    if (
        not embedded_signature
        or analysis_contract_signature(typed_contract) != embedded_signature
    ):
        raise EvidenceIntegrityError(
            "completed_material_authority_contract_invalid"
        )
    if not isinstance(material_authority, Mapping):
        raise EvidenceIntegrityError(
            "completed_material_authority_carrier_invalid"
        )
    validated_material = validate_material_authority(
        material_authority,
        source_run_id=_required(run_id, "source_run_id"),
        thread_id=thread_id,
        topic_id=topic_id,
        require_execution_material=True,
    )
    registry = runtime_registry or RuntimeContractRegistry.from_path(
        CANONICAL_RUNTIME_BINDINGS_PATH
    )
    execution_material = validated_material["execution_material"]
    if (
        execution_material["runtime_contract_version"]
        != registry.contract_version
        or execution_material["runtime_registry_digest"]
        != registry.source_payload_digest
    ):
        raise EvidenceIntegrityError(
            "completed_material_authority_runtime_registry_mismatch"
        )
    validate_material_authority_contract_overlap(
        validated_material,
        typed_contract,
        runtime_registry=registry,
    )
    return _contract_target_metric_ids(
        typed_contract,
        runtime_registry=registry,
        material_target_metric_ids=tuple(
            validated_material["intent_material"].get("target_metrics") or ()
        ),
    )


def validate_completed_followup_authority(
    *,
    source_run_id: str,
    thread_id: str,
    topic_id: str,
    analysis_contract: Mapping[str, Any],
    stored_contract_signature: str,
    analysis_run_id: str,
    run_status: str,
    run_thread_id: str,
    run_topic_id: str,
    request_analysis_contract: Mapping[str, Any],
    material_authority: Mapping[str, Any],
    authority_record: Mapping[str, Any],
    authority_event_ref: str,
    authority_event_run_id: str,
    authority_event_thread_id: str,
    authority_event_topic_id: str,
) -> dict[str, Any]:
    if analysis_run_id != source_run_id:
        raise EvidenceIntegrityError("completed_followup_source_run_mismatch")
    if run_status != "completed":
        raise EvidenceIntegrityError("completed_followup_source_run_not_complete")
    if (run_thread_id, run_topic_id) != (thread_id, topic_id):
        raise EvidenceIntegrityError("completed_followup_owner_mismatch")
    if not isinstance(material_authority, Mapping):
        raise EvidenceIntegrityError("completed_followup_material_authority_missing")
    validated_material = validate_material_authority(
        material_authority,
        source_run_id=source_run_id,
        thread_id=thread_id,
        topic_id=topic_id,
        require_execution_material=True,
    )
    if not isinstance(analysis_contract, Mapping):
        raise EvidenceIntegrityError("completed_followup_contract_payload_invalid")
    contract_payload = dict(analysis_contract)
    embedded_signature = str(contract_payload.pop("contract_signature", "") or "")
    try:
        typed_contract = analysis_contract_from_dict(contract_payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise EvidenceIntegrityError(
            "completed_followup_contract_payload_invalid"
        ) from exc
    expected_signature = analysis_contract_signature(typed_contract)
    if (
        not stored_contract_signature
        or expected_signature != stored_contract_signature
        or not embedded_signature
        or embedded_signature != stored_contract_signature
    ):
        raise EvidenceIntegrityError(
            "completed_followup_contract_signature_invalid"
        )
    if typed_contract.analysis_contract_id != f"analysis:{source_run_id}:1":
        raise EvidenceIntegrityError("completed_followup_contract_run_mismatch")
    authoritative_copy = {
        **typed_contract.to_dict(),
        "contract_signature": stored_contract_signature,
    }
    if (
        not isinstance(request_analysis_contract, Mapping)
        or canonical_value(request_analysis_contract)
        != canonical_value(authoritative_copy)
    ):
        raise EvidenceIntegrityError("completed_followup_request_contract_mismatch")
    validate_material_authority_contract_overlap(
        validated_material,
        typed_contract,
    )
    validate_completed_material_authority_record(
        authority_record,
        source_run_id=source_run_id,
        thread_id=thread_id,
        topic_id=topic_id,
        analysis_contract=authoritative_copy,
        material_authority=validated_material,
        event_ref=authority_event_ref,
        event_run_id=authority_event_run_id,
        event_thread_id=authority_event_thread_id,
        event_topic_id=authority_event_topic_id,
    )
    return {
        "source_run_id": source_run_id,
        "thread_id": thread_id,
        "topic_id": topic_id,
        "analysis_contract": typed_contract.to_dict(),
        "analysis_contract_signature": stored_contract_signature,
        "material_authority": validated_material,
    }


def build_completed_material_authority_record(
    *,
    source_run_id: str,
    thread_id: str,
    topic_id: str,
    analysis_contract: Mapping[str, Any],
    material_authority: Mapping[str, Any],
) -> dict[str, Any]:
    contract = canonical_value(analysis_contract)
    material = canonical_value(material_authority)
    body = {
        "schema_version": "completed-material-authority.v1",
        "source_run_id": source_run_id,
        "thread_id": thread_id,
        "topic_id": topic_id,
        "analysis_contract_ref": str(contract.get("analysis_contract_id") or ""),
        "analysis_contract_signature": str(contract.get("contract_signature") or ""),
        "analysis_contract_digest": canonical_digest(contract),
        "material_authority": material,
        "material_authority_digest": canonical_digest(material),
    }
    record = {**body, "record_digest": canonical_digest(body)}
    validate_completed_material_authority_record(
        record,
        source_run_id=source_run_id,
        thread_id=thread_id,
        topic_id=topic_id,
        analysis_contract=contract,
        material_authority=material,
        event_ref=f"completed-material-authority:{source_run_id}",
        event_run_id=source_run_id,
        event_thread_id=thread_id,
        event_topic_id=topic_id,
    )
    return record


def validate_completed_material_authority_record(
    value: Mapping[str, Any],
    *,
    source_run_id: str,
    thread_id: str,
    topic_id: str,
    analysis_contract: Mapping[str, Any],
    material_authority: Mapping[str, Any],
    event_ref: str,
    event_run_id: str,
    event_thread_id: str,
    event_topic_id: str,
) -> dict[str, Any]:
    if (
        not isinstance(value, Mapping)
        or set(value) != _COMPLETED_MATERIAL_AUTHORITY_RECORD_KEYS
        or value.get("schema_version") != "completed-material-authority.v1"
    ):
        raise EvidenceIntegrityError("completed_followup_authority_record_mismatch")
    owners = (
        str(value.get("source_run_id") or ""),
        str(value.get("thread_id") or ""),
        str(value.get("topic_id") or ""),
        event_run_id,
        event_thread_id,
        event_topic_id,
    )
    if owners != (
        source_run_id,
        thread_id,
        topic_id,
        source_run_id,
        thread_id,
        topic_id,
    ):
        raise EvidenceIntegrityError("completed_followup_authority_record_mismatch")
    if event_ref != f"completed-material-authority:{source_run_id}":
        raise EvidenceIntegrityError("completed_followup_authority_record_mismatch")
    contract = canonical_value(analysis_contract)
    material = canonical_value(material_authority)
    body = {
        key: canonical_value(item)
        for key, item in value.items()
        if key != "record_digest"
    }
    if (
        value.get("analysis_contract_ref")
        != contract.get("analysis_contract_id")
        or value.get("analysis_contract_signature")
        != contract.get("contract_signature")
        or value.get("analysis_contract_digest") != canonical_digest(contract)
        or canonical_value(value.get("material_authority")) != material
        or value.get("material_authority_digest") != canonical_digest(material)
        or value.get("record_digest") != canonical_digest(body)
    ):
        raise EvidenceIntegrityError("completed_followup_authority_record_mismatch")
    return {**body, "record_digest": str(value["record_digest"])}


def validate_resolved_completed_material_authority(
    value: Mapping[str, Any],
    *,
    source_run_id: str,
    thread_id: str,
    topic_id: str,
) -> dict[str, Any]:
    if (
        not isinstance(value, Mapping)
        or set(value) != _RESOLVED_COMPLETED_MATERIAL_AUTHORITY_KEYS
    ):
        raise EvidenceIntegrityError(
            "prior_topic_completed_authority_shape_invalid"
        )
    if (
        str(value.get("source_run_id") or "") != source_run_id
        or str(value.get("thread_id") or "") != thread_id
        or str(value.get("topic_id") or "") != topic_id
    ):
        raise EvidenceIntegrityError(
            "prior_topic_completed_authority_owner_mismatch"
        )
    material = validate_material_authority(
        value.get("material_authority"),
        source_run_id=source_run_id,
        thread_id=thread_id,
        topic_id=topic_id,
        require_execution_material=True,
    )
    contract_payload = value.get("analysis_contract")
    if not isinstance(contract_payload, Mapping):
        raise EvidenceIntegrityError(
            "prior_topic_completed_authority_contract_invalid"
        )
    signature = str(value.get("analysis_contract_signature") or "")
    try:
        typed_contract = analysis_contract_from_dict(contract_payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise EvidenceIntegrityError(
            "prior_topic_completed_authority_contract_invalid"
        ) from exc
    if (
        not signature
        or analysis_contract_signature(typed_contract) != signature
        or typed_contract.analysis_contract_id
        != f"analysis:{source_run_id}:1"
    ):
        raise EvidenceIntegrityError(
            "prior_topic_completed_authority_contract_invalid"
        )
    validate_material_authority_contract_overlap(material, typed_contract)
    return canonical_value(
        {
            "source_run_id": source_run_id,
            "thread_id": thread_id,
            "topic_id": topic_id,
            "analysis_contract": typed_contract.to_dict(),
            "analysis_contract_signature": signature,
            "material_authority": material,
        }
    )


def build_prior_topic_material_context(
    *,
    thread_id: str,
    topic_id: str,
    source_result_refs: Iterable[str],
    authorities: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    authority_values = tuple(authorities)
    if any(
        not isinstance(authority, Mapping)
        for authority in authority_values
    ):
        raise EvidenceIntegrityError(
            "prior_topic_completed_authority_shape_invalid"
        )
    validated = tuple(
        sorted(
            (
                validate_resolved_completed_material_authority(
                    authority,
                    source_run_id=str(
                        authority.get("source_run_id") or ""
                    ),
                    thread_id=thread_id,
                    topic_id=topic_id,
                )
                for authority in authority_values
            ),
            key=lambda item: item["source_run_id"],
        )
    )
    if not validated:
        raise EvidenceIntegrityError("prior_topic_material_context_empty")
    source_run_ids = [item["source_run_id"] for item in validated]
    if len(set(source_run_ids)) != len(source_run_ids):
        raise EvidenceIntegrityError(
            "prior_topic_completed_authority_duplicate"
        )
    projections = tuple(
        canonical_value(
            {
                "intent_material": item["material_authority"][
                    "intent_material"
                ],
                "route_material_slots": item["material_authority"][
                    "route_material_slots"
                ],
            }
        )
        for item in validated
    )
    if any(projection != projections[0] for projection in projections[1:]):
        raise EvidenceIntegrityError("prior_topic_material_conflict")
    result_refs = sorted({str(ref) for ref in source_result_refs if str(ref)})
    if not result_refs:
        raise EvidenceIntegrityError("prior_topic_result_refs_missing")
    body = {
        "schema_version": "prior-topic-material.v2",
        "thread_id": thread_id,
        "topic_id": topic_id,
        "source_run_ids": source_run_ids,
        "source_result_refs": result_refs,
        "material_projection": projections[0],
        "authorities": list(validated),
    }
    return {**body, "context_digest": canonical_digest(body)}


def validate_prior_topic_material_context(
    value: Mapping[str, Any],
    *,
    thread_id: str,
    topic_id: str,
) -> dict[str, Any]:
    if (
        not isinstance(value, Mapping)
        or set(value) != _PRIOR_TOPIC_MATERIAL_CONTEXT_KEYS
        or value.get("schema_version") != "prior-topic-material.v2"
        or value.get("thread_id") != thread_id
        or value.get("topic_id") != topic_id
    ):
        raise EvidenceIntegrityError(
            "prior_topic_material_context_shape_invalid"
        )
    authorities = value.get("authorities")
    result_refs = value.get("source_result_refs")
    if not isinstance(authorities, list) or not isinstance(result_refs, list):
        raise EvidenceIntegrityError(
            "prior_topic_material_context_shape_invalid"
        )
    rebuilt = build_prior_topic_material_context(
        thread_id=thread_id,
        topic_id=topic_id,
        source_result_refs=result_refs,
        authorities=authorities,
    )
    projection = value.get("material_projection")
    if (
        not isinstance(projection, Mapping)
        or set(projection) != _PRIOR_TOPIC_MATERIAL_PROJECTION_KEYS
        or canonical_value(value) != rebuilt
    ):
        raise EvidenceIntegrityError(
            "prior_topic_material_context_mismatch"
        )
    return rebuilt


def _execution_material_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _EXECUTION_MATERIAL_KEYS:
        raise EvidenceIntegrityError(
            "material_authority_execution_material_shape_invalid"
        )
    if str(value.get("schema_version") or "") != "3":
        raise EvidenceIntegrityError(
            "material_authority_execution_material_version_invalid"
        )
    target_semantic = _canonical_target_date(value.get("target_semantic"))
    as_of = _canonical_as_of(value.get("as_of"))
    timezone_name = _required_execution_token(
        value.get("business_timezone"), "business_timezone"
    )
    try:
        ZoneInfo(timezone_name)
    except (KeyError, ValueError) as exc:
        raise EvidenceIntegrityError(
            "execution_material_business_timezone_invalid"
        ) from exc
    fixed_bounds = _canonical_fixed_window_bounds(
        value.get("fixed_window_bounds")
    )
    target_bounds = fixed_bounds.get("target_day")
    if target_bounds != [target_semantic, target_semantic]:
        raise EvidenceIntegrityError(
            "execution_material_target_semantic_invalid"
        )
    accepted_graph = _exact_string_values(
        value.get("accepted_graph"),
        reason="execution_material_accepted_graph_invalid",
    )
    source_queries_raw = value.get("source_query_contracts")
    if not isinstance(source_queries_raw, (list, tuple)):
        raise EvidenceIntegrityError(
            "execution_material_source_query_contracts_invalid"
        )
    source_queries: list[dict[str, Any]] = []
    signatures: list[str] = []
    for record in source_queries_raw:
        if (
            not isinstance(record, Mapping)
            or set(record) != _SOURCE_QUERY_CONTRACT_KEYS
        ):
            raise EvidenceIntegrityError(
                "execution_material_source_query_contracts_invalid"
            )
        signature = _required_execution_token(
            record.get("contract_signature"),
            "source_query_contracts",
        )
        if signature in signatures:
            raise EvidenceIntegrityError(
                "execution_material_source_query_contracts_invalid"
            )
        signatures.append(signature)
        owner_capability_ids = _exact_string_values(
            record.get("owner_capability_ids"),
            reason="execution_material_source_query_contracts_invalid",
        )
        expected_owner_order = tuple(
            capability
            for capability in accepted_graph
            if capability in set(owner_capability_ids)
        )
        if (
            not owner_capability_ids
            or owner_capability_ids != expected_owner_order
        ):
            raise EvidenceIntegrityError(
                "execution_material_source_query_contracts_invalid"
            )
        source_queries.append(
            {
                "contract_signature": signature,
                "dataset_snapshot_refs": list(
                    _exact_string_values(
                        record.get("dataset_snapshot_refs"),
                        reason=(
                            "execution_material_source_query_contracts_invalid"
                        ),
                    )
                ),
                "owner_capability_ids": list(owner_capability_ids),
            }
        )
    if signatures != sorted(signatures):
        raise EvidenceIntegrityError(
            "execution_material_source_query_contracts_invalid"
        )
    return {
        "schema_version": "3",
        "target_semantic": target_semantic,
        "as_of": as_of,
        "business_timezone": timezone_name,
        "context_window_specs": _canonical_context_window_specs(
            value.get("context_window_specs")
        ),
        "fixed_window_bounds": fixed_bounds,
        "filters": _canonical_filters(value.get("filters")),
        "grain": canonical_execution_grain(value.get("grain")),
        "dataset_requirements": list(
            _exact_string_values(
                value.get("dataset_requirements"),
                reason="execution_material_dataset_requirements_invalid",
            )
        ),
        "metric_dataset_overrides": _canonical_overrides(
            value.get("metric_dataset_overrides"),
            reason="execution_material_metric_dataset_overrides_invalid",
        ),
        "dimension_dataset_overrides": _canonical_overrides(
            value.get("dimension_dataset_overrides"),
            reason="execution_material_dimension_dataset_overrides_invalid",
        ),
        "requested_context_sources": list(
            _exact_string_values(
                value.get("requested_context_sources"),
                reason="execution_material_requested_context_sources_invalid",
            )
        ),
        "accepted_graph": list(accepted_graph),
        "runtime_contract_version": _required_execution_token(
            value.get("runtime_contract_version"),
            "runtime_contract_version",
        ),
        "runtime_registry_digest": _required_execution_token(
            value.get("runtime_registry_digest"),
            "runtime_registry_digest",
        ),
        "run_mode_class": _canonical_run_mode_class(
            value.get("run_mode_class")
        ),
        "source_query_contracts": source_queries,
    }


def _canonical_execution_proposal_axis(axis: str, value: Any) -> Any:
    if axis == "filters":
        return _canonical_filters(value)
    if axis == "grain":
        return canonical_execution_grain(value)
    if axis == "dataset_requirements":
        return list(
            _exact_string_values(
                value,
                reason="clarification_source_material_dataset_requirements_mismatch",
            )
        )
    if axis in {
        "metric_dataset_overrides",
        "dimension_dataset_overrides",
    }:
        return _canonical_overrides(
            value,
            reason=f"clarification_source_material_{axis}_mismatch",
        )
    raise EvidenceIntegrityError(
        f"clarification_source_material_{axis}_mismatch"
    )


def _canonical_filters(value: Any) -> list[Any]:
    if value in (None, (), [], {}):
        return []
    raw = [value] if isinstance(value, Mapping) else value
    if (
        not isinstance(raw, (list, tuple))
        or any(not isinstance(item, Mapping) for item in raw)
    ):
        raise EvidenceIntegrityError("execution_material_filters_invalid")
    return [canonical_value(dict(item)) for item in raw]


def canonical_execution_grain(value: Any) -> str:
    if value in (None, ""):
        return "window_id"
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
    ):
        raise EvidenceIntegrityError("execution_material_grain_invalid")
    return value


def _canonical_overrides(value: Any, *, reason: str) -> dict[str, str]:
    if value in (None, {}):
        return {}
    if not isinstance(value, Mapping):
        raise EvidenceIntegrityError(reason)
    output: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        if (
            not isinstance(raw_key, str)
            or raw_key != raw_key.strip()
            or not raw_key
            or not isinstance(raw_value, str)
            or raw_value != raw_value.strip()
            or not raw_value
        ):
            raise EvidenceIntegrityError(reason)
        output[raw_key] = raw_value
    return {key: output[key] for key in sorted(output)}


def _canonical_context_window_specs(value: Any) -> list[dict[str, Any]]:
    if value in (None, (), []):
        return []
    if not isinstance(value, (list, tuple)):
        raise EvidenceIntegrityError(
            "execution_material_context_window_specs_invalid"
        )
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, int]] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {
            "capability_id",
            "relation",
            "unit",
            "count",
        }:
            raise EvidenceIntegrityError(
                "execution_material_context_window_specs_invalid"
            )
        capability_id = str(item.get("capability_id") or "")
        relation = str(item.get("relation") or "")
        unit = str(item.get("unit") or "")
        count = item.get("count")
        identity = (capability_id, relation, unit, count)
        if (
            not re.fullmatch(r"[a-z][a-z0-9_]*", capability_id)
            or relation != "trailing_complete_periods"
            or unit not in {"day", "week", "month", "quarter"}
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count <= 0
            or identity in seen
        ):
            raise EvidenceIntegrityError(
                "execution_material_context_window_specs_invalid"
            )
        seen.add(identity)
        output.append(
            {
                "capability_id": capability_id,
                "relation": relation,
                "unit": unit,
                "count": count,
            }
        )
    return output


def _is_context_window_id(window_id: str) -> bool:
    return bool(
        re.fullmatch(
            r"context__[a-z][a-z0-9_]*__"
            r"trailing_complete_periods__[1-9][0-9]*_"
            r"(?:day|week|month|quarter)",
            window_id,
        )
    )


def _canonical_fixed_window_bounds(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, Mapping) or not value:
        raise EvidenceIntegrityError(
            "execution_material_fixed_window_bounds_invalid"
        )
    unknown = {
        window_id
        for window_id in set(value) - set(_FIXED_WINDOW_IDS)
        if not isinstance(window_id, str)
        or not _is_context_window_id(window_id)
    }
    if unknown:
        raise EvidenceIntegrityError(
            "execution_material_fixed_window_bounds_invalid"
        )
    output: dict[str, list[str]] = {}
    ordered_window_ids = (
        *_FIXED_WINDOW_IDS,
        *sorted(
            window_id
            for window_id in value
            if window_id not in _FIXED_WINDOW_IDS
        ),
    )
    for window_id in ordered_window_ids:
        if window_id not in value:
            continue
        bounds = value[window_id]
        if (
            not isinstance(bounds, (list, tuple))
            or len(bounds) != 2
            or any(not isinstance(item, str) for item in bounds)
        ):
            raise EvidenceIntegrityError(
                "execution_material_fixed_window_bounds_invalid"
            )
        try:
            start = date.fromisoformat(bounds[0])
            end = date.fromisoformat(bounds[1])
        except ValueError as exc:
            raise EvidenceIntegrityError(
                "execution_material_fixed_window_bounds_invalid"
            ) from exc
        if start > end:
            raise EvidenceIntegrityError(
                "execution_material_fixed_window_bounds_invalid"
            )
        output[window_id] = [start.isoformat(), end.isoformat()]
    if "target_day" not in output:
        raise EvidenceIntegrityError(
            "execution_material_fixed_window_bounds_invalid"
        )
    return output


def _canonical_target_date(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise EvidenceIntegrityError(
            "execution_material_target_semantic_invalid"
        )
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise EvidenceIntegrityError(
            "execution_material_target_semantic_invalid"
        ) from exc


def _resolve_target_semantic(
    value: Any,
    *,
    as_of: Any,
    timezone_name: str,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("target_semantic_invalid")
    token = value.strip()
    try:
        return date.fromisoformat(token).isoformat()
    except ValueError:
        pass
    parsed_as_of = _parse_as_of(as_of)
    local_day = parsed_as_of.astimezone(ZoneInfo(timezone_name)).date()
    if token in {"yesterday", "昨天", "昨日", "latest_complete_day"}:
        return (local_day - timedelta(days=1)).isoformat()
    if token in {"today", "今天", "今日"}:
        return local_day.isoformat()
    raise ValueError("target_semantic_invalid")


def _canonical_as_of(value: Any) -> str:
    return _parse_as_of(value).isoformat()


def _parse_as_of(value: Any) -> datetime:
    try:
        parsed = (
            datetime.fromisoformat(value)
            if isinstance(value, str)
            else value
        )
    except ValueError as exc:
        raise EvidenceIntegrityError("execution_material_as_of_invalid") from exc
    if not isinstance(parsed, datetime) or parsed.tzinfo is None:
        raise EvidenceIntegrityError("execution_material_as_of_invalid")
    return parsed


def _same_as_of(left: Any, right: Any) -> bool:
    try:
        return _parse_as_of(left).astimezone(timezone.utc) == _parse_as_of(
            right
        ).astimezone(timezone.utc)
    except EvidenceIntegrityError:
        return False


def _exact_string_values(value: Any, *, reason: str) -> tuple[str, ...]:
    raw = () if value is None else value
    if (
        isinstance(raw, (str, bytes, Mapping))
        or not isinstance(raw, Iterable)
    ):
        raise EvidenceIntegrityError(reason)
    values = tuple(raw)
    if (
        any(
            not isinstance(item, str)
            or not item
            or item != item.strip()
            for item in values
        )
        or len(values) != len(set(values))
    ):
        raise EvidenceIntegrityError(reason)
    return values


def _required_execution_token(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
    ):
        raise EvidenceIntegrityError(f"execution_material_{field}_invalid")
    return value


def _run_mode_class(value: Any) -> str:
    mode = str(value or "production")
    if mode in {"production", "live"}:
        return "authoritative"
    raise EvidenceIntegrityError("execution_material_run_mode_class_invalid")


def _canonical_run_mode_class(value: Any) -> str:
    if value != "authoritative":
        raise EvidenceIntegrityError(
            "execution_material_run_mode_class_invalid"
        )
    return str(value)


def _canonical_baseline_sequence(value: Any) -> tuple[str, ...]:
    items = (
        tuple(value)
        if isinstance(value, (list, tuple))
        else (value,)
    )
    output: list[str] = []
    for item in items:
        canonical = canonical_baseline_ids(item)
        if len(canonical) != 1 or canonical[0] in output:
            raise BaselineSemanticError("baseline_semantics_conflict")
        output.append(canonical[0])
    return tuple(output)


def _contract_target_metric_ids(
    analysis_contract: AnalysisContract,
    *,
    runtime_registry: RuntimeContractRegistry | None = None,
    material_target_metric_ids: tuple[str, ...] = (),
) -> tuple[str, ...]:
    requested_metric_ids = tuple(
        analysis_contract.scope.get("requested_metric_ids") or ()
    )
    requested_metric_set = set(requested_metric_ids)
    material_metric_set = set(material_target_metric_ids)
    metric_ids: list[str] = []
    for contract_ref in analysis_contract.target_metric_refs:
        bound_matches = tuple(
            dict.fromkeys(
                binding.metric_id
                for binding in analysis_contract.metric_bindings
                if binding.contract_ref == contract_ref
            )
        )
        if bound_matches:
            if len(bound_matches) != 1:
                raise EvidenceIntegrityError(
                    "material_authority_contract_target_metrics_unresolvable"
                )
            matches = bound_matches
        elif runtime_registry is None:
            matches = ()
        else:
            matches = runtime_registry.metric_ids_for_contract_ref(
                contract_ref
            )
        if (
            not bound_matches
            and len(matches) != 1
            and requested_metric_ids
            and material_target_metric_ids
        ):
            matches = tuple(
                metric_id
                for metric_id in matches
                if metric_id in requested_metric_set
                and metric_id in material_metric_set
            )
        if len(matches) != 1:
            raise EvidenceIntegrityError(
                "material_authority_contract_target_metrics_unresolvable"
            )
        if matches[0] not in metric_ids:
            metric_ids.append(matches[0])
    if not metric_ids:
        raise EvidenceIntegrityError(
            "material_authority_contract_target_metrics_unresolvable"
        )
    resolved_metric_ids = tuple(metric_ids)
    resolved_metric_set = set(resolved_metric_ids)
    if requested_metric_ids and tuple(
        metric_id
        for metric_id in requested_metric_ids
        if metric_id in resolved_metric_set
    ) != resolved_metric_ids:
        raise EvidenceIntegrityError(
            "material_authority_contract_target_metrics_unresolvable"
        )
    if (
        material_target_metric_ids
        and resolved_metric_ids != material_target_metric_ids
    ):
        raise EvidenceIntegrityError(
            "material_authority_contract_target_metrics_mismatch"
        )
    return resolved_metric_ids


def _contract_material_scope(scope: Mapping[str, Any]) -> Any:
    return _material_scope(
        {
            str(key): value
            for key, value in scope.items()
            if key
            not in {"requested_metric_ids", "requested_dimension_ids"}
            and value not in (None, "", {}, [])
        }
    )


def _material_scope(scope: Any) -> Any:
    if scope in (None, "", {}, []):
        return {"type": "full_sample"}
    if isinstance(scope, str):
        return {"type": scope}
    if isinstance(scope, Mapping):
        return canonical_value(scope)
    raise EvidenceIntegrityError("material_authority_contract_scope_mismatch")


def _route_control_projection(value: Any) -> dict[str, Any]:
    raw = [] if value is None else value
    if not isinstance(raw, (list, tuple)):
        raise EvidenceIntegrityError(
            "material_authority_rejection_history_invalid"
        )
    records: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != _OBLIGATION_REJECTION_KEYS:
            raise EvidenceIntegrityError(
                "material_authority_rejection_history_invalid"
            )
        action = item.get("action")
        capability = item.get("capability")
        reason = item.get("reason")
        if (
            not all(
                isinstance(field, str)
                and field
                and field == field.strip()
                for field in (action, capability, reason)
            )
            or action != "rejected"
            or reason not in _LOCAL_OBLIGATION_REJECTION_REASONS
        ):
            raise EvidenceIntegrityError(
                "material_authority_rejection_history_invalid"
            )
        record_key = (action, capability, reason)
        if record_key in seen:
            raise EvidenceIntegrityError(
                "material_authority_rejection_history_invalid"
            )
        seen.add(record_key)
        records.append(
            {
                "action": action,
                "capability": capability,
                "reason": reason,
            }
        )
    return {"obligation_rejection_history": records}


def _intent_material_projection(
    original: Mapping[str, Any],
    *,
    route_material: Mapping[str, Any],
) -> dict[str, Any]:
    raw_families = original.get("question_families")
    families = _string_sequence(
        raw_families,
        reason="material_authority_question_families_invalid",
        required=True,
    )
    primary_family = str(original.get("primary_question_family") or "")
    question_family = str(original.get("question_family") or "")
    secondary = _string_sequence(
        original.get("secondary_question_families"),
        reason="material_authority_question_families_invalid",
    )
    if (
        not primary_family
        or primary_family != question_family
        or primary_family != families[0]
        or secondary != families[1:]
    ):
        raise EvidenceIntegrityError("material_authority_question_families_invalid")
    target_metrics = list(route_material["target_metrics"])
    primary_target = str(original.get("target_metric") or "")
    if (
        not primary_target
        or target_metrics != [primary_target]
    ):
        raise EvidenceIntegrityError("material_authority_target_metrics_invalid")
    goal_bindings = _goal_bindings(original.get("goal_bindings"))
    explicit_focus = _explicit_focus(original.get("explicit_focus"))
    goal_material = _compiled_goal_material_projection(
        goal_bindings=goal_bindings,
        target_metric=primary_target,
        explicit_focus=explicit_focus,
    )
    return {
        "primary_question_family": primary_family,
        "question_families": families,
        "primary_target_metric": primary_target,
        "target_metrics": target_metrics,
        "goal_bindings": goal_bindings,
        "explicit_focus": explicit_focus,
        "component_ids": goal_material["component_ids"],
        "association_metric_ids": goal_material["association_metric_ids"],
        "dimension_ids": goal_material["dimension_ids"],
        "baselines": _intent_baseline_projection(
            original,
            route_baselines=route_material["baselines"],
        ),
        "context_sources": goal_material["context_sources"],
        "claim_types": goal_material["claim_types"],
        "required_outcomes": goal_material["required_outcomes"],
        "analysis_axis_ids": goal_material["analysis_axis_ids"],
        "scope": _canonical_scope(original.get("scope")),
        "time_window": _canonical_time_window(original.get("time_window")),
    }


def _route_material_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    unknown = set(value) - _ROUTE_MATERIAL_KEYS
    required = set(_MATERIAL_LIST_AXES) | {"scope"}
    if unknown or not required.issubset(value):
        raise EvidenceIntegrityError("material_authority_route_shape_invalid")
    projected = {
        axis: _string_sequence(
            value.get(axis),
            reason=f"material_authority_{axis}_invalid",
            required=axis == "target_metrics",
        )
        for axis in _MATERIAL_LIST_AXES
    }
    projected["diagnostic_tags"] = _string_sequence(
        value.get("diagnostic_tags"),
        reason="material_authority_diagnostic_tags_invalid",
    )
    projected["scope"] = _canonical_scope(value.get("scope"))
    return projected


def _validate_intent_material(value: Mapping[str, Any]) -> None:
    families = _string_sequence(
        value.get("question_families"),
        reason="material_authority_question_families_invalid",
        required=True,
    )
    primary_family = str(value.get("primary_question_family") or "")
    if not primary_family or primary_family != families[0]:
        raise EvidenceIntegrityError("material_authority_question_families_invalid")
    targets = _string_sequence(
        value.get("target_metrics"),
        reason="material_authority_target_metrics_invalid",
        required=True,
    )
    if str(value.get("primary_target_metric") or "") != targets[0]:
        raise EvidenceIntegrityError("material_authority_target_metrics_invalid")
    _goal_bindings(value.get("goal_bindings"))
    _explicit_focus(value.get("explicit_focus"))
    for axis in (
        "component_ids",
        "association_metric_ids",
        "dimension_ids",
        "baselines",
        "context_sources",
        "claim_types",
        "required_outcomes",
        "analysis_axis_ids",
    ):
        _string_sequence(
            value.get(axis),
            reason=f"material_authority_{axis}_invalid",
        )
    _canonical_scope(value.get("scope"))
    _canonical_time_window(value.get("time_window"))


def _validate_route_material(value: Mapping[str, Any]) -> None:
    for axis in _MATERIAL_LIST_AXES:
        _string_sequence(
            value.get(axis),
            reason=f"material_authority_{axis}_invalid",
            required=axis == "target_metrics",
        )
    _string_sequence(
        value.get("diagnostic_tags"),
        reason="material_authority_diagnostic_tags_invalid",
    )
    _canonical_scope(value.get("scope"))


def _string_sequence(
    value: Any,
    *,
    reason: str,
    required: bool = False,
) -> list[str]:
    raw = [] if value is None else value
    if (
        not isinstance(raw, (list, tuple))
        or any(not isinstance(item, str) or not item for item in raw)
        or len(raw) != len(set(raw))
        or (required and not raw)
    ):
        raise EvidenceIntegrityError(reason)
    return list(raw)


def _goal_bindings(value: Any) -> list[dict[str, str]]:
    if (
        not isinstance(value, (list, tuple))
        or not value
        or any(
            not isinstance(item, Mapping)
            or set(item) != {"goal_id", "role"}
            or not isinstance(item.get("goal_id"), str)
            or not str(item.get("goal_id") or "").strip()
            or item.get("role") not in {"primary", "supporting"}
            for item in value
        )
        or len({str(item["goal_id"]) for item in value}) != len(value)
        or sum(item.get("role") == "primary" for item in value) != 1
    ):
        raise EvidenceIntegrityError(
            "material_authority_goal_bindings_invalid"
        )
    return [
        {"goal_id": str(item["goal_id"]), "role": str(item["role"])}
        for item in value
    ]


def _explicit_focus(value: Any) -> dict[str, list[str]]:
    fields = {"component_ids", "dimension_ids", "context_source_ids"}
    if not isinstance(value, Mapping) or set(value) != fields:
        raise EvidenceIntegrityError(
            "material_authority_explicit_focus_invalid"
        )
    return {
        field: _string_sequence(
            value.get(field),
            reason=f"material_authority_explicit_focus_{field}_invalid",
        )
        for field in sorted(fields)
    }


def _compiled_goal_material_projection(
    *,
    goal_bindings: Any,
    target_metric: str,
    explicit_focus: Any,
    runtime_registry: RuntimeContractRegistry | None = None,
) -> dict[str, Any]:
    registry = runtime_registry or RuntimeContractRegistry.from_path(
        CANONICAL_RUNTIME_BINDINGS_PATH
    )
    try:
        plan = registry.compile_goal_analysis_plan(
            goal_bindings=goal_bindings,
            target_metric=target_metric,
            explicit_focus=explicit_focus,
        )
    except (KeyError, TypeError, ValueError) as exc:
        reason = str(exc)
        axis = (
            "explicit_focus"
            if "explicit_focus" in reason
            else "goal_bindings"
            if "goal" in reason
            else "target_metrics"
        )
        raise EvidenceIntegrityError(
            f"material_authority_{axis}_invalid"
        ) from exc
    axes = tuple(
        axis
        for axis in plan.get("analysis_axes") or ()
        if isinstance(axis, Mapping)
    )
    dimension_ids = list(
        dict.fromkeys(
            str(dimension_id)
            for axis in axes
            for dimension_id in axis.get("dimension_refs") or ()
            if str(dimension_id)
        )
    )
    component_ids = list(
        dict.fromkeys(
            str(metric_id)
            for axis in axes
            if str(axis.get("axis_kind") or "") == "formula_tree"
            for metric_id in axis.get("metric_refs") or ()
            if str(metric_id) and str(metric_id) != target_metric
        )
    )
    association_metric_ids = list(
        dict.fromkeys(
            str(metric_id)
            for axis in axes
            if str(axis.get("axis_kind") or "") == "cross_source_context"
            for metric_id in axis.get("metric_refs") or ()
            if str(metric_id) and str(metric_id) != target_metric
        )
    )
    claim_types_by_role: dict[str, list[str]] = {
        "required": [],
        "auxiliary": [],
        "conditional": [],
    }
    for axis in axes:
        role = str(axis.get("role") or "")
        if role not in claim_types_by_role:
            continue
        for capability_id in axis.get("capability_refs") or ():
            try:
                capability = registry.capability_inputs(str(capability_id))
            except KeyError as exc:
                raise EvidenceIntegrityError(
                    "material_authority_analysis_axis_ids_invalid"
                ) from exc
            claim_types_by_role[role].extend(
                str(claim_type)
                for claim_type in capability.get("supported_claim_types") or ()
                if str(claim_type)
            )
    required_claim_types = list(
        dict.fromkeys(claim_types_by_role["required"])
    )
    auxiliary_claim_types = [
        claim_type
        for claim_type in dict.fromkeys(
            (
                *claim_types_by_role["auxiliary"],
                *claim_types_by_role["conditional"],
            )
        )
        if claim_type not in set(required_claim_types)
    ]
    return {
        "goal_bindings": _goal_bindings(plan.get("goal_bindings")),
        "explicit_focus": _explicit_focus(plan.get("explicit_focus")),
        "component_ids": component_ids,
        "association_metric_ids": association_metric_ids,
        "dimension_ids": dimension_ids,
        "context_sources": list(explicit_focus["context_source_ids"]),
        "claim_types": list(
            dict.fromkeys((*required_claim_types, *auxiliary_claim_types))
        ),
        "required_claim_types": required_claim_types,
        "required_outcomes": _string_sequence(
            plan.get("required_outcomes"),
            reason="material_authority_required_outcomes_invalid",
            required=True,
        ),
        "analysis_axis_ids": _string_sequence(
            [str(axis.get("axis_id") or "") for axis in axes],
            reason="material_authority_analysis_axis_ids_invalid",
            required=True,
        ),
    }


def _validate_goal_plan_material_overlap(
    intent_material: Mapping[str, Any],
    route_material: Mapping[str, Any],
    runtime_registry: RuntimeContractRegistry,
) -> None:
    expected = _compiled_goal_material_projection(
        goal_bindings=intent_material.get("goal_bindings"),
        target_metric=str(intent_material.get("primary_target_metric") or ""),
        explicit_focus=intent_material.get("explicit_focus"),
        runtime_registry=runtime_registry,
    )
    for axis in (
        "goal_bindings",
        "explicit_focus",
        "component_ids",
        "association_metric_ids",
        "dimension_ids",
        "context_sources",
        "claim_types",
        "required_outcomes",
        "analysis_axis_ids",
    ):
        if canonical_value(intent_material.get(axis)) != canonical_value(
            expected[axis]
        ):
            raise EvidenceIntegrityError(
                f"material_authority_contract_{axis}_mismatch"
            )
    for axis in (
        "component_ids",
        "association_metric_ids",
        "dimension_ids",
        "context_sources",
        "required_outcomes",
        "analysis_axis_ids",
    ):
        if canonical_value(route_material.get(axis)) != canonical_value(
            expected[axis]
        ):
            raise EvidenceIntegrityError(
                f"material_authority_contract_{axis}_mismatch"
            )
    route_claim_types = tuple(route_material.get("claim_types") or ())
    known_claim_types = {
        str(claim_type)
        for capability_id in runtime_registry.capability_ids
        for claim_type in runtime_registry.capability_inputs(
            capability_id
        ).get("supported_claim_types", ())
    }
    if (
        any(claim_type not in known_claim_types for claim_type in route_claim_types)
        or any(
            claim_type not in set(route_claim_types)
            for claim_type in expected["required_claim_types"]
        )
    ):
        raise EvidenceIntegrityError(
            "material_authority_contract_claim_types_mismatch"
        )


def _intent_baseline_projection(
    original: Mapping[str, Any],
    *,
    route_baselines: Any,
) -> list[str]:
    reviewed = _string_sequence(
        route_baselines,
        reason="material_authority_baselines_invalid",
    )
    if any(item not in CANONICAL_BASELINE_IDS for item in reviewed):
        raise EvidenceIntegrityError("material_authority_baselines_invalid")

    candidates = original.get("baseline_candidates")
    selected_values: list[Any] = []
    time_window = original.get("time_window")
    if isinstance(time_window, Mapping) and "baseline" in time_window:
        selected_values.append(time_window.get("baseline"))
    if original.get("baseline") not in (None, "", {}, []):
        selected_values.append(original.get("baseline"))

    try:
        candidate_ids = canonical_baseline_ids(candidates)
    except BaselineSemanticError as exc:
        raise EvidenceIntegrityError(
            "material_authority_baselines_invalid"
        ) from exc
    try:
        selected_ids = canonical_baseline_ids(selected_values)
    except BaselineSemanticError as exc:
        if not candidate_ids:
            raise EvidenceIntegrityError(
                "material_authority_baselines_invalid"
            ) from exc
        selected_ids = ()

    binding = original.get("baseline_binding")
    if binding is not None:
        if not isinstance(binding, Mapping) or type(
            binding.get("confirmed")
        ) is not bool:
            raise EvidenceIntegrityError("material_authority_baselines_invalid")
        try:
            binding_ids = canonical_baseline_ids(binding.get("candidates"))
        except BaselineSemanticError as exc:
            raise EvidenceIntegrityError(
                "material_authority_baselines_invalid"
            ) from exc
        if binding_ids != candidate_ids:
            raise EvidenceIntegrityError("material_authority_baselines_invalid")
        if not binding["confirmed"]:
            if reviewed or (
                selected_ids
                and not set(selected_ids).issubset(candidate_ids)
            ):
                raise EvidenceIntegrityError(
                    "material_authority_baselines_invalid"
                )
            return []
        if not binding_ids:
            raise EvidenceIntegrityError("material_authority_baselines_invalid")

    if candidate_ids and any(item not in reviewed for item in candidate_ids):
        raise EvidenceIntegrityError("material_authority_baselines_invalid")
    if selected_ids and any(item not in reviewed for item in selected_ids):
        raise EvidenceIntegrityError("material_authority_baselines_invalid")
    if selected_ids and candidate_ids and not set(selected_ids).issubset(
        candidate_ids
    ):
        raise EvidenceIntegrityError("material_authority_baselines_invalid")
    explicit_ids = {*candidate_ids, *selected_ids}
    if not explicit_ids:
        return []
    selected = [item for item in reviewed if item in explicit_ids]
    if not selected:
        raise EvidenceIntegrityError("material_authority_baselines_invalid")
    return selected


def _canonical_time_window(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        if not value or value != value.strip():
            raise EvidenceIntegrityError("material_authority_time_window_invalid")
        return value
    if not isinstance(value, Mapping) or not value:
        raise EvidenceIntegrityError("material_authority_time_window_invalid")
    return canonical_value(value)


def _canonical_scope(value: Any) -> Any:
    if value is None:
        return None
    if not isinstance(value, (str, Mapping)):
        raise EvidenceIntegrityError("material_authority_scope_invalid")
    return canonical_value(value)


def clarification_resolution_source_request_digest(
    source_request: Mapping[str, Any],
) -> str:
    """Bind one accepted clarification to the exact source request it answered."""

    if not isinstance(source_request, Mapping):
        raise EvidenceIntegrityError(
            "clarification_resolution_source_request_invalid"
        )
    return canonical_digest(dict(source_request))


def clarification_resolution_digest(
    *,
    resolution_id: str,
    source_run_id: str,
    thread_id: str,
    topic_id: str,
    owner_id: str,
    submission: Mapping[str, Any],
    message_id: str,
    source_request_digest: str,
    accepted_choice: Mapping[str, Any],
) -> str:
    """Sign the durable user decision independently from execution attempts."""

    fields = {
        "resolution_id": resolution_id,
        "source_run_id": source_run_id,
        "thread_id": thread_id,
        "topic_id": topic_id,
        "owner_id": owner_id,
        "message_id": message_id,
        "source_request_digest": source_request_digest,
    }
    normalized = {
        key: str(value or "").strip() for key, value in fields.items()
    }
    if any(not value for value in normalized.values()):
        raise EvidenceIntegrityError(
            "clarification_resolution_digest_material_invalid"
        )
    if not isinstance(submission, Mapping) or not isinstance(
        accepted_choice, Mapping
    ):
        raise EvidenceIntegrityError(
            "clarification_resolution_digest_material_invalid"
        )
    body = {
        "schema_version": "clarification-resolution.v1",
        **normalized,
        "submission": canonical_value(dict(submission)),
        "accepted_choice": canonical_value(dict(accepted_choice)),
    }
    return canonical_digest(body)


def clarification_attempt_request_digest(
    *,
    producer_kind: str,
    scope_ref: str,
    thread_id: str,
    text: str,
    request_payload: Mapping[str, Any],
) -> str:
    """Reproduce the Gateway dispatch digest from durable request material."""

    tokens = tuple(
        str(value or "").strip()
        for value in (producer_kind, scope_ref, thread_id, text)
    )
    if any(not value for value in tokens) or not isinstance(
        request_payload, Mapping
    ):
        raise EvidenceIntegrityError(
            "clarification_resolution_attempt_request_invalid"
        )
    return canonical_digest(
        {
            "producerKind": tokens[0],
            "scopeRef": tokens[1],
            "threadId": tokens[2],
            "text": tokens[3],
            "requestPayload": dict(request_payload),
        }
    )


def validate_clarification_resolution_choice(
    *,
    source_request: Mapping[str, Any],
    submission: Mapping[str, Any],
    accepted_choice: Mapping[str, Any],
) -> dict[str, Any]:
    """Prove that a persisted choice is one action offered by the source run."""

    if not all(
        isinstance(value, Mapping)
        for value in (source_request, submission, accepted_choice)
    ):
        raise EvidenceIntegrityError(
            "clarification_resolution_choice_material_invalid"
        )
    envelope = source_request.get("clarification_source_envelope")
    clarification = (
        envelope.get("clarification")
        if isinstance(envelope, Mapping)
        else None
    )
    actions = tuple(
        canonical_value(dict(action))
        for action in (
            clarification.get("choice_actions")
            if isinstance(clarification, Mapping)
            else ()
        )
        if isinstance(action, Mapping)
    )
    if not actions:
        raise EvidenceIntegrityError(
            "clarification_resolution_source_choices_missing"
        )

    selected_option_id = submission.get("selectedOptionId")
    if selected_option_id is not None:
        selected_option_id = str(selected_option_id).strip()
        if not selected_option_id:
            raise EvidenceIntegrityError(
                "clarification_resolution_selected_option_invalid"
            )
        matches = tuple(
            action
            for action in actions
            if str(action.get("choice_id") or "") == selected_option_id
        )
    else:
        answer = str(submission.get("answer") or "").strip()
        if not answer:
            raise EvidenceIntegrityError(
                "clarification_resolution_answer_invalid"
            )
        matches = tuple(
            action
            for action in actions
            if clarification_labels_match(
                action.get("business_label")
                or action.get("business_semantics"),
                answer,
            )
        )
        if not matches:
            matches = tuple(
                action
                for action in actions
                if str(action.get("action_kind") or "")
                == "user_redirect"
            )

    if len(matches) != 1:
        raise EvidenceIntegrityError(
            "clarification_resolution_choice_ambiguous"
        )
    expected = matches[0]
    if canonical_value(dict(accepted_choice)) != expected:
        raise EvidenceIntegrityError(
            "clarification_resolution_accepted_choice_mismatch"
        )
    return expected


def validate_clarification_resolution_attempt(
    *,
    resolution: Mapping[str, Any],
    attempt: Mapping[str, Any],
    source_run: Mapping[str, Any],
    attempt_run: Mapping[str, Any],
    dispatch: Mapping[str, Any],
    source_run_id: str,
    attempt_run_id: str,
    thread_id: str,
    owner_id: str,
    answer: str,
    selected_option_id: str | None,
    source: str,
) -> dict[str, Any]:
    """Validate one initial or retry attempt against one durable resolution."""

    if not all(
        isinstance(value, Mapping)
        for value in (resolution, attempt, source_run, attempt_run, dispatch)
    ):
        raise EvidenceIntegrityError(
            "clarification_resolution_attempt_material_invalid"
        )
    normalized_inputs = {
        "source_run_id": str(source_run_id or "").strip(),
        "attempt_run_id": str(attempt_run_id or "").strip(),
        "thread_id": str(thread_id or "").strip(),
        "owner_id": str(owner_id or "").strip(),
        "answer": str(answer or "").strip(),
        "source": str(source or "").strip(),
    }
    if any(not value for value in normalized_inputs.values()):
        raise EvidenceIntegrityError(
            "clarification_resolution_attempt_input_invalid"
        )

    resolution_id = str(resolution.get("resolution_id") or "")
    resolution_status = str(resolution.get("status") or "")
    attempt_number = attempt.get("attempt_number")
    previous_attempt_run_id = attempt.get("previous_attempt_run_id")
    submission = resolution.get("submission")
    accepted_choice = resolution.get("accepted_choice")
    source_request = source_run.get("request")
    if (
        not resolution_id
        or resolution_status != "accepted"
        or not str(resolution.get("accepted_at") or "")
        or not isinstance(attempt_number, int)
        or isinstance(attempt_number, bool)
        or attempt_number <= 0
        or not isinstance(submission, Mapping)
        or not isinstance(accepted_choice, Mapping)
        or not isinstance(source_request, Mapping)
    ):
        raise EvidenceIntegrityError(
            "clarification_resolution_attempt_material_invalid"
        )
    if (
        str(source_run.get("status") or "")
        != "waiting_for_clarification"
        or str(resolution.get("message_thread_id") or "")
        != normalized_inputs["thread_id"]
        or str(resolution.get("message_role") or "") != "user"
        or str(resolution.get("message_text") or "").strip()
        != normalized_inputs["answer"]
    ):
        raise EvidenceIntegrityError(
            "clarification_resolution_source_state_invalid"
        )

    owners = (
        str(resolution.get("source_run_id") or ""),
        str(resolution.get("thread_id") or ""),
        str(resolution.get("topic_id") or ""),
        str(resolution.get("owner_id") or ""),
        str(attempt.get("resolution_id") or ""),
        str(attempt.get("attempt_run_id") or ""),
        str(source_run.get("run_id") or ""),
        str(source_run.get("thread_id") or ""),
        str(source_run.get("topic_id") or ""),
        str(source_run.get("owner_id") or ""),
        str(attempt_run.get("run_id") or ""),
        str(attempt_run.get("thread_id") or ""),
        str(dispatch.get("run_id") or ""),
        str(dispatch.get("thread_id") or ""),
    )
    expected_owners = (
        normalized_inputs["source_run_id"],
        normalized_inputs["thread_id"],
        str(resolution.get("topic_id") or ""),
        normalized_inputs["owner_id"],
        resolution_id,
        normalized_inputs["attempt_run_id"],
        normalized_inputs["source_run_id"],
        normalized_inputs["thread_id"],
        str(resolution.get("topic_id") or ""),
        normalized_inputs["owner_id"],
        normalized_inputs["attempt_run_id"],
        normalized_inputs["thread_id"],
        normalized_inputs["attempt_run_id"],
        normalized_inputs["thread_id"],
    )
    if owners != expected_owners or not expected_owners[2]:
        raise EvidenceIntegrityError(
            "clarification_resolution_attempt_owner_mismatch"
        )

    stored_selected = submission.get("selectedOptionId")
    if stored_selected is not None:
        stored_selected = str(stored_selected)
    if (
        set(submission)
        != {"sourceRunId", "answer", "selectedOptionId", "source"}
        or str(submission.get("sourceRunId") or "")
        != normalized_inputs["source_run_id"]
        or str(submission.get("answer") or "").strip()
        != normalized_inputs["answer"]
        or stored_selected != selected_option_id
        or str(submission.get("source") or "")
        != normalized_inputs["source"]
    ):
        raise EvidenceIntegrityError(
            "clarification_resolution_submission_mismatch"
        )

    computed_source_digest = clarification_resolution_source_request_digest(
        source_request
    )
    if str(resolution.get("source_request_digest") or "") != computed_source_digest:
        raise EvidenceIntegrityError(
            "clarification_resolution_source_request_digest_mismatch"
        )
    validated_choice = validate_clarification_resolution_choice(
        source_request=source_request,
        submission=submission,
        accepted_choice=accepted_choice,
    )
    computed_resolution_digest = clarification_resolution_digest(
        resolution_id=resolution_id,
        source_run_id=normalized_inputs["source_run_id"],
        thread_id=normalized_inputs["thread_id"],
        topic_id=str(resolution.get("topic_id") or ""),
        owner_id=normalized_inputs["owner_id"],
        submission=submission,
        message_id=str(resolution.get("message_id") or ""),
        source_request_digest=computed_source_digest,
        accepted_choice=validated_choice,
    )
    if str(resolution.get("resolution_digest") or "") != computed_resolution_digest:
        raise EvidenceIntegrityError(
            "clarification_resolution_digest_mismatch"
        )

    dispatch_kind = str(dispatch.get("producer_kind") or "")
    expected_request_payload = {
        "sourceRunId": normalized_inputs["source_run_id"],
        "resolutionId": resolution_id,
        "attemptRunId": normalized_inputs["attempt_run_id"],
        "answer": normalized_inputs["answer"],
        "selectedOptionId": selected_option_id,
        "source": normalized_inputs["source"],
        "retryAttempt": attempt_number > 1,
    }
    if attempt_number > 1:
        expected_request_payload["previousAttemptRunId"] = str(
            previous_attempt_run_id or ""
        )
    if canonical_value(dispatch.get("request_payload") or {}) != canonical_value(
        expected_request_payload
    ):
        raise EvidenceIntegrityError(
            "clarification_resolution_attempt_request_payload_mismatch"
        )
    dispatch_digest = clarification_attempt_request_digest(
        producer_kind=dispatch_kind,
        scope_ref=str(dispatch.get("scope_ref") or ""),
        thread_id=str(dispatch.get("thread_id") or ""),
        text=str(dispatch.get("text") or ""),
        request_payload=dispatch.get("request_payload") or {},
    )
    if (
        str(attempt.get("request_identity") or "")
        != str(dispatch.get("request_identity") or "")
        or str(attempt.get("request_digest") or "")
        != str(dispatch.get("request_digest") or "")
        or str(dispatch.get("request_digest") or "") != dispatch_digest
        or not str(attempt.get("request_identity") or "")
        or not str(attempt.get("request_digest") or "")
        or str(dispatch.get("dispatch_state") or "") != "running"
        or str(attempt_run.get("status") or "") != "running"
    ):
        raise EvidenceIntegrityError(
            "clarification_resolution_attempt_dispatch_mismatch"
        )
    if attempt_number == 1:
        if (
            previous_attempt_run_id not in (None, "")
            or dispatch_kind != "clarification_resume"
            or str(dispatch.get("scope_ref") or "")
            != resolution_id
        ):
            raise EvidenceIntegrityError(
                "clarification_resolution_initial_attempt_invalid"
            )
    else:
        if (
            not str(previous_attempt_run_id or "")
            or dispatch_kind != "clarification_retry"
            or str(dispatch.get("scope_ref") or "") != resolution_id
            or str(attempt.get("previous_resolution_id") or "")
            != resolution_id
            or attempt.get("previous_attempt_number")
            != attempt_number - 1
            or str(attempt.get("previous_attempt_status") or "")
            != "failed"
        ):
            raise EvidenceIntegrityError(
                "clarification_resolution_retry_attempt_invalid"
            )

    raw_material_patch = validated_choice.get("material_patch") or {}
    if not isinstance(raw_material_patch, Mapping):
        raise EvidenceIntegrityError(
            "clarification_resolution_material_patch_invalid"
        )
    material_patch = canonical_value(dict(raw_material_patch))
    return canonical_value(
        {
            "resolution_id": resolution_id,
            "source_run_id": normalized_inputs["source_run_id"],
            "attempt_run_id": normalized_inputs["attempt_run_id"],
            "previous_attempt_run_id": (
                str(previous_attempt_run_id)
                if previous_attempt_run_id not in (None, "")
                else None
            ),
            "attempt_number": attempt_number,
            "thread_id": normalized_inputs["thread_id"],
            "topic_id": str(resolution.get("topic_id") or ""),
            "owner_id": normalized_inputs["owner_id"],
            "request_identity": str(attempt.get("request_identity") or ""),
            "answer": normalized_inputs["answer"],
            "selected_option_id": selected_option_id,
            "source": normalized_inputs["source"],
            "accepted_choice": validated_choice,
            "source_request_digest": computed_source_digest,
            "resolution_digest": computed_resolution_digest,
            "retry_attempt": attempt_number > 1,
            "material_patch": material_patch,
            "resolution_status": resolution_status,
        }
    )


def _required(value: str, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise EvidenceIntegrityError(f"clarification_authority_{field}_missing")
    return normalized
