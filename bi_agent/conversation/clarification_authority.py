from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
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
        "requested_components",
        "requested_dimensions",
        "baselines",
        "context_sources",
        "claim_intents",
        "scope",
        "time_window",
    }
)
_ROUTE_MATERIAL_KEYS = frozenset(
    {
        "target_metrics",
        "requested_components",
        "requested_dimensions",
        "baselines",
        "context_sources",
        "claim_intents",
        "diagnostic_tags",
        "scope",
    }
)
_MATERIAL_LIST_AXES = (
    "target_metrics",
    "requested_components",
    "requested_dimensions",
    "baselines",
    "context_sources",
    "claim_intents",
)
_EXECUTION_MATERIAL_KEYS = frozenset(
    {
        "schema_version",
        "target_semantic",
        "as_of",
        "business_timezone",
        "permission_scope",
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
        "permission_scope",
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
        "schema_version": "3",
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
    if str(value.get("schema_version") or "") != "3":
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
    permission_scope: str,
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
    if str(permission_scope or "") != contract_runtime["permission_scope"]:
        raise EvidenceIntegrityError(
            "execution_material_permission_scope_mismatch"
        )
    raw_proposal = dict(proposal)
    canonical_accepted_graph = _exact_string_values(
        accepted_graph,
        reason="execution_material_accepted_graph_invalid",
    )
    material = {
        "schema_version": "1",
        **contract_runtime,
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
    permission_scope = _required_execution_token(
        contract.permission_scope,
        "permission_scope",
    )
    windows: dict[str, list[str]] = {}
    legacy_windows: list[tuple[str, str, date, date]] = []
    for window in contract.resolved_windows:
        window_id = str(window.window_id or "")
        if window_id not in {*_FIXED_WINDOW_IDS, "target", "baseline"}:
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
        if window_id in _FIXED_WINDOW_IDS:
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
        "permission_scope": permission_scope,
        "fixed_window_bounds": {
            window_id: windows[window_id]
            for window_id in _FIXED_WINDOW_IDS
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
) -> None:
    intent_material = material_authority.get("intent_material")
    route_material = material_authority.get("route_material_slots")
    if not isinstance(intent_material, Mapping) or not isinstance(
        route_material, Mapping
    ):
        raise EvidenceIntegrityError("material_authority_shape_invalid")
    if tuple(intent_material.get("question_families") or ()) != tuple(
        analysis_contract.question_families
    ):
        raise EvidenceIntegrityError(
            "material_authority_contract_question_families_mismatch"
        )
    contract_target_metrics = _contract_target_metric_ids(analysis_contract)
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
    execution_material = material_authority.get("execution_material")
    if not isinstance(execution_material, Mapping):
        raise EvidenceIntegrityError(
            "material_authority_execution_material_missing"
        )
    expected_runtime = _contract_runtime_projection(analysis_contract)
    for axis in (
        "target_semantic",
        "as_of",
        "business_timezone",
        "permission_scope",
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
    permission_scopes = {
        str(item["analysis_contract"].get("permission_scope") or "")
        for item in validated
    }
    permission_scopes.update(
        str(
            item["material_authority"]["execution_material"].get(
                "permission_scope"
            )
            or ""
        )
        for item in validated
    )
    if len(permission_scopes) != 1 or "" in permission_scopes:
        raise EvidenceIntegrityError(
            "prior_topic_permission_scope_mismatch"
        )
    result_refs = sorted({str(ref) for ref in source_result_refs if str(ref)})
    if not result_refs:
        raise EvidenceIntegrityError("prior_topic_result_refs_missing")
    body = {
        "schema_version": "prior-topic-material.v1",
        "thread_id": thread_id,
        "topic_id": topic_id,
        "source_run_ids": source_run_ids,
        "source_result_refs": result_refs,
        "permission_scope": next(iter(permission_scopes)),
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
        or value.get("schema_version") != "prior-topic-material.v1"
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


def validate_terminal_resume_proposal_overlap(
    material_authority: Mapping[str, Any],
    proposal: Mapping[str, Any],
) -> None:
    intent_material = material_authority.get("intent_material")
    route_material = material_authority.get("route_material_slots")
    execution_material = material_authority.get("execution_material")
    if (
        not isinstance(intent_material, Mapping)
        or not isinstance(route_material, Mapping)
        or not isinstance(execution_material, Mapping)
    ):
        raise EvidenceIntegrityError("material_authority_shape_invalid")
    _validate_repeated_family_axes(intent_material, proposal)
    _validate_repeated_target_axes(intent_material, proposal)
    for axis in (
        "requested_components",
        "requested_dimensions",
        "claim_intents",
        "diagnostic_tags",
    ):
        _validate_repeated_sequence_axis(
            proposal,
            axis,
            route_material.get(axis),
            reason_axis=axis,
        )
    _validate_repeated_baseline_axis(
        proposal,
        "baselines",
        route_material.get("baselines"),
    )
    for alias in ("context_sources", "requested_context_sources"):
        _validate_repeated_sequence_axis(
            proposal,
            alias,
            route_material.get("context_sources"),
            reason_axis="context_sources",
        )
    if "scope" in proposal:
        _validate_repeated_scope(
            proposal.get("scope"),
            route_material.get("scope"),
        )
    if "time_window" in proposal and canonical_value(
        proposal.get("time_window")
    ) != canonical_value(intent_material.get("time_window")):
        raise EvidenceIntegrityError(
            "terminal_resume_proposal_time_window_mismatch"
        )
    for alias in ("target_semantic", "target_window"):
        if alias in proposal:
            _validate_repeated_target_semantic(
                proposal.get(alias), execution_material
            )
    if "fixed_window_bounds" in proposal:
        if _canonical_fixed_window_bounds(
            proposal.get("fixed_window_bounds")
        ) != execution_material.get("fixed_window_bounds"):
            raise EvidenceIntegrityError(
                "terminal_resume_proposal_fixed_window_bounds_mismatch"
            )
    for axis in (
        "filters",
        "grain",
        "dataset_requirements",
        "metric_dataset_overrides",
        "dimension_dataset_overrides",
    ):
        if axis not in proposal:
            continue
        candidate = _canonical_execution_proposal_axis(
            axis, proposal.get(axis)
        )
        if canonical_value(candidate) != canonical_value(
            execution_material.get(axis)
        ):
            raise EvidenceIntegrityError(
                f"terminal_resume_proposal_{axis}_mismatch"
            )


def validate_terminal_clarification_choice_overlap(
    material_authority: Mapping[str, Any],
    choice: Mapping[str, Any],
) -> None:
    intent_material = material_authority.get("intent_material")
    route_material = material_authority.get("route_material_slots")
    execution_material = material_authority.get("execution_material")
    if (
        not isinstance(intent_material, Mapping)
        or not isinstance(route_material, Mapping)
        or not isinstance(execution_material, Mapping)
    ):
        raise EvidenceIntegrityError("material_authority_shape_invalid")
    _validate_repeated_family_axes(intent_material, choice)
    _validate_repeated_target_axes(intent_material, choice)
    for axis in (
        "requested_components",
        "requested_dimensions",
        "claim_intents",
        "diagnostic_tags",
    ):
        _validate_repeated_sequence_axis(
            choice,
            axis,
            route_material.get(axis),
            reason_axis=axis,
        )
    _validate_repeated_baseline_axis(
        choice,
        "baselines",
        route_material.get("baselines"),
    )
    if "baseline_candidates" in choice:
        _validate_repeated_baseline_axis(
            choice,
            "baseline_candidates",
            intent_material.get("baselines"),
        )
    for alias in ("context_sources", "requested_context_sources"):
        _validate_repeated_sequence_axis(
            choice,
            alias,
            route_material.get("context_sources"),
            reason_axis="context_sources",
        )
    if "scope" in choice:
        _validate_repeated_scope(
            choice.get("scope"), route_material.get("scope")
        )
    if "time_window" in choice and canonical_value(
        choice.get("time_window")
    ) != canonical_value(intent_material.get("time_window")):
        raise EvidenceIntegrityError(
            "terminal_resume_proposal_time_window_mismatch"
        )
    for alias in ("target_semantic", "target_window"):
        if alias in choice:
            _validate_repeated_target_semantic(
                choice.get(alias), execution_material
            )
    for axis in (
        "filters",
        "grain",
        "dataset_requirements",
        "metric_dataset_overrides",
        "dimension_dataset_overrides",
        "fixed_window_bounds",
    ):
        if axis not in choice:
            continue
        candidate = (
            _canonical_fixed_window_bounds(choice.get(axis))
            if axis == "fixed_window_bounds"
            else _canonical_execution_proposal_axis(axis, choice.get(axis))
        )
        if canonical_value(candidate) != canonical_value(
            execution_material.get(axis)
        ):
            raise EvidenceIntegrityError(
                f"terminal_resume_proposal_{axis}_mismatch"
            )


def bind_terminal_resume_proposal_material(
    material_authority: Mapping[str, Any],
    proposal: Mapping[str, Any],
) -> dict[str, Any]:
    validate_terminal_resume_proposal_overlap(material_authority, proposal)
    intent_material = material_authority["intent_material"]
    route_material = material_authority["route_material_slots"]
    execution_material = material_authority["execution_material"]
    bound = dict(proposal)
    bound.update(
        {
            "question_families": list(intent_material["question_families"]),
            "target_metrics": list(route_material["target_metrics"]),
            "requested_components": list(
                route_material["requested_components"]
            ),
            "requested_dimensions": list(
                route_material["requested_dimensions"]
            ),
            "baselines": list(route_material["baselines"]),
            "context_sources": list(route_material["context_sources"]),
            "requested_context_sources": list(
                route_material["context_sources"]
            ),
            "claim_intents": list(route_material["claim_intents"]),
            "diagnostic_tags": list(route_material["diagnostic_tags"]),
            "scope": (
                canonical_value(route_material["scope"])
                if route_material["scope"] not in (None, "", {}, [])
                else "full_sample"
            ),
            "time_window": canonical_value(intent_material["time_window"]),
            "target_semantic": execution_material["target_semantic"],
            "fixed_window_bounds": canonical_value(
                execution_material["fixed_window_bounds"]
            ),
            "filters": canonical_value(execution_material["filters"]),
            "grain": execution_material["grain"],
            "dataset_requirements": list(
                execution_material["dataset_requirements"]
            ),
            "metric_dataset_overrides": canonical_value(
                execution_material["metric_dataset_overrides"]
            ),
            "dimension_dataset_overrides": canonical_value(
                execution_material["dimension_dataset_overrides"]
            ),
        }
    )
    return bound


def validate_terminal_runtime_context_overlap(
    material_authority: Mapping[str, Any],
    *,
    analysis_context: Mapping[str, Any],
    permission_scope: str,
    accepted_graph: Iterable[str],
    accepted_choice: Mapping[str, Any],
    run_mode: str,
    runtime_contract_version: str,
    runtime_registry_digest: str,
) -> dict[str, Any]:
    execution_material = material_authority.get("execution_material")
    if not isinstance(execution_material, Mapping):
        raise EvidenceIntegrityError(
            "material_authority_execution_material_missing"
        )
    if str(permission_scope or "") != str(
        execution_material.get("permission_scope") or ""
    ):
        raise EvidenceIntegrityError(
            "terminal_resume_runtime_permission_scope_mismatch"
        )
    if _run_mode_class(run_mode) != execution_material.get("run_mode_class"):
        raise EvidenceIntegrityError(
            "terminal_resume_runtime_run_mode_class_mismatch"
        )
    if str(runtime_contract_version or "") != str(
        execution_material.get("runtime_contract_version") or ""
    ):
        raise EvidenceIntegrityError(
            "terminal_resume_runtime_contract_version_mismatch"
        )
    if str(runtime_registry_digest or "") != str(
        execution_material.get("runtime_registry_digest") or ""
    ):
        raise EvidenceIntegrityError(
            "terminal_resume_runtime_registry_digest_mismatch"
        )
    _validate_terminal_accepted_graph(
        execution_material,
        accepted_graph=accepted_graph,
        accepted_choice=accepted_choice,
    )
    context = dict(analysis_context)
    if "as_of" in context and not _same_as_of(
        context.get("as_of"), execution_material["as_of"]
    ):
        raise EvidenceIntegrityError(
            "terminal_resume_runtime_as_of_mismatch"
        )
    signed_bounds = execution_material["fixed_window_bounds"]
    for context_key, (window_id, index) in (
        _ANALYSIS_CONTEXT_WINDOW_FIELDS.items()
    ):
        if context_key not in context:
            continue
        expected = signed_bounds.get(window_id)
        if (
            not isinstance(context.get(context_key), str)
            or expected is None
            or context[context_key] != expected[index]
        ):
            raise EvidenceIntegrityError(
                "terminal_resume_runtime_fixed_window_bounds_mismatch:"
                + window_id
            )
    return dict(execution_material)


def validate_terminal_compile_overlap(
    material_authority: Mapping[str, Any],
    *,
    analysis_contract: AnalysisContract | Mapping[str, Any],
    query_contracts: Iterable[Mapping[str, Any] | Any],
    accepted_graph: Iterable[str],
    accepted_choice: Mapping[str, Any],
) -> None:
    execution_material = material_authority.get("execution_material")
    if not isinstance(execution_material, Mapping):
        raise EvidenceIntegrityError(
            "material_authority_execution_material_missing"
        )
    current_runtime = _contract_runtime_projection(
        _typed_analysis_contract(analysis_contract)
    )
    for axis in (
        "target_semantic",
        "as_of",
        "business_timezone",
        "permission_scope",
        "fixed_window_bounds",
    ):
        if canonical_value(current_runtime[axis]) != canonical_value(
            execution_material[axis]
        ):
            raise EvidenceIntegrityError(
                f"terminal_resume_compile_{axis}_mismatch"
            )
    current_graph = _exact_string_values(
        accepted_graph,
        reason="terminal_resume_runtime_accepted_graph_mismatch",
    )
    _validate_terminal_accepted_graph(
        execution_material,
        accepted_graph=current_graph,
        accepted_choice=accepted_choice,
    )
    current_capabilities = set(current_graph)
    expected_queries = {
        str(record["contract_signature"]): tuple(
            record["dataset_snapshot_refs"]
        )
        for record in execution_material["source_query_contracts"]
        if current_capabilities.intersection(
            record["owner_capability_ids"]
        )
    }
    current_queries = _current_query_contract_projection(query_contracts)
    if current_queries != expected_queries:
        raise EvidenceIntegrityError(
            "terminal_resume_compile_query_contract_projection_mismatch"
        )


def _validate_terminal_accepted_graph(
    execution_material: Mapping[str, Any],
    *,
    accepted_graph: Iterable[str],
    accepted_choice: Mapping[str, Any],
) -> None:
    source = tuple(execution_material.get("accepted_graph") or ())
    current = _exact_string_values(
        accepted_graph,
        reason="terminal_resume_runtime_accepted_graph_mismatch",
    )
    affected = _exact_string_values(
        accepted_choice.get("affected_capabilities"),
        reason="terminal_resume_runtime_accepted_graph_mismatch",
    )
    source_set = set(source)
    current_set = set(current)
    required = source_set - set(affected)
    if not required.issubset(current_set) or not current_set.issubset(
        source_set
    ):
        raise EvidenceIntegrityError(
            "terminal_resume_runtime_accepted_graph_mismatch"
        )


def _validate_repeated_family_axes(
    intent_material: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> None:
    signed = tuple(intent_material.get("question_families") or ())
    primary = str(intent_material.get("primary_question_family") or "")
    reason = "terminal_resume_proposal_question_families_mismatch"
    if "question_families" in candidate and _proposal_axis_values(
        candidate.get("question_families")
    ) != signed:
        raise EvidenceIntegrityError(reason)
    for alias in ("question_family", "primary_question_family"):
        if alias in candidate and candidate.get(alias) != primary:
            raise EvidenceIntegrityError(reason)
    if "secondary_question_families" in candidate and _proposal_axis_values(
        candidate.get("secondary_question_families")
    ) != signed[1:]:
        raise EvidenceIntegrityError(reason)


def _validate_repeated_target_axes(
    intent_material: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> None:
    signed = tuple(intent_material.get("target_metrics") or ())
    primary = str(intent_material.get("primary_target_metric") or "")
    reason = "terminal_resume_proposal_target_metrics_mismatch"
    if "target_metrics" in candidate and _proposal_axis_values(
        candidate.get("target_metrics")
    ) != signed:
        raise EvidenceIntegrityError(reason)
    if "target_metric" in candidate and candidate.get("target_metric") != primary:
        raise EvidenceIntegrityError(reason)


def _validate_repeated_sequence_axis(
    candidate: Mapping[str, Any],
    key: str,
    expected: Any,
    *,
    reason_axis: str,
) -> None:
    if key not in candidate:
        return
    if _proposal_axis_values(candidate.get(key)) != tuple(expected or ()):
        raise EvidenceIntegrityError(
            f"terminal_resume_proposal_{reason_axis}_mismatch"
        )


def _validate_repeated_baseline_axis(
    candidate: Mapping[str, Any],
    key: str,
    expected: Any,
) -> None:
    if key not in candidate:
        return
    try:
        actual = _canonical_baseline_sequence(candidate.get(key))
    except BaselineSemanticError as exc:
        raise EvidenceIntegrityError(
            "terminal_resume_proposal_baselines_mismatch"
        ) from exc
    if actual != tuple(expected or ()):
        raise EvidenceIntegrityError(
            "terminal_resume_proposal_baselines_mismatch"
        )


def _validate_repeated_scope(value: Any, expected: Any) -> None:
    if value in (None, "", {}, []):
        raise EvidenceIntegrityError(
            "terminal_resume_proposal_scope_mismatch"
        )
    try:
        matches = _material_scope(value) == _material_scope(expected)
    except EvidenceIntegrityError as exc:
        raise EvidenceIntegrityError(
            "terminal_resume_proposal_scope_mismatch"
        ) from exc
    if not matches:
        raise EvidenceIntegrityError(
            "terminal_resume_proposal_scope_mismatch"
        )


def _validate_repeated_target_semantic(
    value: Any,
    execution_material: Mapping[str, Any],
) -> None:
    try:
        actual = _resolve_target_semantic(
            value,
            as_of=execution_material["as_of"],
            timezone_name=execution_material["business_timezone"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise EvidenceIntegrityError(
            "terminal_resume_proposal_time_window_mismatch"
        ) from exc
    if actual != execution_material["target_semantic"]:
        raise EvidenceIntegrityError(
            "terminal_resume_proposal_time_window_mismatch"
        )


def _proposal_axis_values(value: Any) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        values = (value,)
    elif isinstance(value, Iterable) and not isinstance(value, Mapping):
        values = value
    else:
        values = ()
    return tuple(
        dict.fromkeys(str(item).strip() for item in values if str(item).strip())
    )


def _execution_material_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _EXECUTION_MATERIAL_KEYS:
        raise EvidenceIntegrityError(
            "material_authority_execution_material_shape_invalid"
        )
    if str(value.get("schema_version") or "") != "1":
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
    permission_scope = _required_execution_token(
        value.get("permission_scope"), "permission_scope"
    )
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
        "schema_version": "1",
        "target_semantic": target_semantic,
        "as_of": as_of,
        "business_timezone": timezone_name,
        "permission_scope": permission_scope,
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
                reason="terminal_resume_proposal_dataset_requirements_mismatch",
            )
        )
    if axis in {
        "metric_dataset_overrides",
        "dimension_dataset_overrides",
    }:
        return _canonical_overrides(
            value,
            reason=f"terminal_resume_proposal_{axis}_mismatch",
        )
    raise EvidenceIntegrityError(
        f"terminal_resume_proposal_{axis}_mismatch"
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


def _canonical_fixed_window_bounds(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, Mapping) or not value:
        raise EvidenceIntegrityError(
            "execution_material_fixed_window_bounds_invalid"
        )
    unknown = set(value) - set(_FIXED_WINDOW_IDS)
    if unknown:
        raise EvidenceIntegrityError(
            "execution_material_fixed_window_bounds_invalid"
        )
    output: dict[str, list[str]] = {}
    for window_id in _FIXED_WINDOW_IDS:
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
    if mode == "fixture":
        return "fixture"
    raise EvidenceIntegrityError("execution_material_run_mode_class_invalid")


def _canonical_run_mode_class(value: Any) -> str:
    if value not in {"authoritative", "fixture"}:
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
) -> tuple[str, ...]:
    metric_ids: list[str] = []
    for contract_ref in analysis_contract.target_metric_refs:
        matches = tuple(
            dict.fromkeys(
                binding.metric_id
                for binding in analysis_contract.metric_bindings
                if binding.contract_ref == contract_ref
            )
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
    return tuple(metric_ids)


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
    if not primary_target or primary_target != target_metrics[0]:
        raise EvidenceIntegrityError("material_authority_target_metrics_invalid")
    return {
        "primary_question_family": primary_family,
        "question_families": families,
        "primary_target_metric": primary_target,
        "target_metrics": target_metrics,
        "requested_components": _string_sequence(
            original.get("requested_components"),
            reason="material_authority_requested_components_invalid",
        ),
        "requested_dimensions": _string_sequence(
            original.get("requested_dimensions"),
            reason="material_authority_requested_dimensions_invalid",
        ),
        "baselines": _intent_baseline_projection(
            original,
            route_baselines=route_material["baselines"],
        ),
        "context_sources": _string_sequence(
            original.get("context_sources"),
            reason="material_authority_context_sources_invalid",
        ),
        "claim_intents": _string_sequence(
            original.get("claim_intents"),
            reason="material_authority_claim_intents_invalid",
        ),
        "scope": _canonical_scope(original.get("scope")),
        "time_window": _canonical_time_window(original.get("time_window")),
    }


def _route_material_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    unknown = set(value) - _ROUTE_MATERIAL_KEYS
    if unknown:
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
    for axis in (
        "requested_components",
        "requested_dimensions",
        "baselines",
        "context_sources",
        "claim_intents",
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


def build_clarification_outcome(
    *,
    source_run_id: str,
    thread_id: str,
    topic_id: str,
    choice: Mapping[str, Any],
) -> dict[str, Any]:
    body = {
        "source_run_id": _required(source_run_id, "source_run_id"),
        "thread_id": _required(thread_id, "thread_id"),
        "topic_id": _required(topic_id, "topic_id"),
        "choice": canonical_value(dict(choice)),
    }
    digest = stable_contract_signature(body)
    payload = {
        "outcome_ref": f"clarification-outcome:{digest}",
        **body,
    }
    payload["outcome_signature"] = stable_contract_signature(payload)
    return payload


def validate_clarification_resume_authority(
    *,
    source_run_id: str,
    thread_id: str,
    topic_id: str,
    choice: Mapping[str, Any],
    outcome_ref: str,
    analysis_contract: Mapping[str, Any],
    stored_contract_signature: str,
    analysis_run_id: str,
    run_status: str,
    run_thread_id: str,
    run_topic_id: str,
    clarification_outcome: Mapping[str, Any],
    outcome_run_id: str,
    outcome_thread_id: str,
    outcome_topic_id: str,
    material_authority: Mapping[str, Any],
) -> dict[str, Any]:
    if analysis_run_id != source_run_id:
        raise EvidenceIntegrityError("clarification_resume_source_run_mismatch")
    if run_status != "waiting_for_clarification":
        raise EvidenceIntegrityError("clarification_resume_source_run_stale")
    owners = (
        run_thread_id,
        run_topic_id,
        outcome_thread_id,
        outcome_topic_id,
    )
    if owners != (thread_id, topic_id, thread_id, topic_id):
        raise EvidenceIntegrityError("clarification_resume_owner_mismatch")
    if outcome_run_id != source_run_id:
        raise EvidenceIntegrityError("clarification_resume_outcome_run_mismatch")
    if not isinstance(material_authority, Mapping):
        raise EvidenceIntegrityError("material_authority_missing")
    validated_material_authority = validate_material_authority(
        material_authority,
        source_run_id=source_run_id,
        thread_id=thread_id,
        topic_id=topic_id,
        require_execution_material=True,
    )
    contract_payload = dict(analysis_contract)
    embedded_signature = str(contract_payload.pop("contract_signature", "") or "")
    try:
        typed_contract = analysis_contract_from_dict(contract_payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise EvidenceIntegrityError("clarification_resume_contract_payload_invalid") from exc
    expected_signature = analysis_contract_signature(typed_contract)
    if (
        not stored_contract_signature
        or expected_signature != stored_contract_signature
        or (embedded_signature and embedded_signature != stored_contract_signature)
    ):
        raise EvidenceIntegrityError("clarification_resume_contract_signature_invalid")
    if typed_contract.analysis_contract_id != f"analysis:{source_run_id}:1":
        raise EvidenceIntegrityError("clarification_resume_contract_run_mismatch")
    validate_material_authority_contract_overlap(
        validated_material_authority,
        typed_contract,
    )

    outcome = dict(clarification_outcome)
    signature = str(outcome.pop("outcome_signature", "") or "")
    if not signature or stable_contract_signature(outcome) != signature:
        raise EvidenceIntegrityError("clarification_resume_outcome_signature_invalid")
    if str(outcome.get("outcome_ref") or "") != outcome_ref:
        raise EvidenceIntegrityError("clarification_resume_outcome_ref_mismatch")
    expected_outcome_ref = "clarification-outcome:" + stable_contract_signature(
        {
            "source_run_id": source_run_id,
            "thread_id": thread_id,
            "topic_id": topic_id,
            "choice": canonical_value(dict(choice)),
        }
    )
    if outcome_ref != expected_outcome_ref:
        raise EvidenceIntegrityError("clarification_resume_outcome_ref_invalid")
    if (
        str(outcome.get("source_run_id") or "") != source_run_id
        or str(outcome.get("thread_id") or "") != thread_id
        or str(outcome.get("topic_id") or "") != topic_id
    ):
        raise EvidenceIntegrityError("clarification_resume_outcome_owner_mismatch")
    if canonical_value(outcome.get("choice")) != canonical_value(dict(choice)):
        raise EvidenceIntegrityError("clarification_resume_choice_mismatch")
    outcome["outcome_signature"] = signature
    return {
        "source_run_id": source_run_id,
        "thread_id": thread_id,
        "topic_id": topic_id,
        "analysis_contract": typed_contract.to_dict(),
        "analysis_contract_signature": stored_contract_signature,
        "material_authority": validated_material_authority,
        "clarification_outcome": outcome,
    }


def _required(value: str, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise EvidenceIntegrityError(f"clarification_outcome_{field}_missing")
    return normalized
