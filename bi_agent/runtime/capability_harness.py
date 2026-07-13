from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from bi_agent.capabilities.data_quality_check import data_quality_check
from bi_agent.capabilities.pattern_scan import scan_pattern
from bi_agent.runtime.exploration_budget import should_ask_before_more_exploration
from bi_agent.runtime.capability_models import (
    CapabilityEvidenceEnvelope,
    CapabilityRequest,
)
from bi_agent.runtime.capability_execution import (
    BoundCapabilityInput,
    validate_bound_capability_input,
)
from bi_agent.runtime.authoritative_query_chain import validate_authoritative_query_chain
from bi_agent.runtime.evidence_authority import legacy_fixture_enabled
from bi_agent.runtime.window_metric_evidence import (
    WindowMetricAggregate,
    WindowMetricComparison,
    WindowMetricEvidenceError,
    WindowMetricObservation,
    aggregate_window_metric_comparison,
)

PATTERN_COMPARE_CAPABILITIES = frozenset(
    {
        "compare_period_phases",
        "compare_periods",
        "rolling_window_compare",
        "weekday_calendar_compare",
        "event_window_compare",
    }
)
WINDOW_METRIC_COMPARE_CAPABILITIES = frozenset({"market_health_compare"})


def execute_capability(request: CapabilityRequest) -> CapabilityEvidenceEnvelope:
    if _bound_input(request) is None and not _legacy_fixture_allowed(request):
        return _blocked_envelope(request, "missing_bound_capability_input")
    input_limitation = _bound_input_limitation(request)
    if input_limitation:
        return _blocked_envelope(request, input_limitation)
    budget_limitation = _budget_limitation(request)
    if budget_limitation:
        return _blocked_envelope(request, budget_limitation)
    if request.capability_id in PATTERN_COMPARE_CAPABILITIES:
        return _execute_pattern_compare(request)
    if request.capability_id in WINDOW_METRIC_COMPARE_CAPABILITIES:
        return _execute_window_metric_compare(request)
    if request.capability_id == "data_quality_profile":
        return _execute_data_quality_profile(request)
    if request.capability_id in {"event_evidence", "gameplay_activity_context"}:
        return _execute_context_capability(request)
    raise KeyError(f"unsupported capability_id: {request.capability_id}")


def _execute_pattern_compare(
    request: CapabilityRequest,
) -> CapabilityEvidenceEnvelope:
    params: dict[str, Any] = dict(request.params)
    rows = _capability_rows(request, params.pop("rows", ()))
    legacy_refs = params.pop("result_refs", ())
    result_refs = _result_refs(request, legacy_refs)
    sql_hashes = _sql_hashes(request, legacy_refs)
    pattern_family = params.pop("pattern_family", "intra_period")
    result = scan_pattern(
        rows,
        pattern_family=pattern_family,
        materiality_floor=params.pop("materiality_floor", 0.03),
        result_refs=result_refs,
        evidence_ref=f"{request.capability_id}:{request.run_id}",
        **params,
    )
    payload = dict(result.typed_payload)
    evidence_type, strength, wording_limit, limitations = _evidence_boundary(
        request,
        evidence_type=result.evidence_type,
        strength=result.strength,
        wording_limit=result.wording_limit,
        limitations=tuple(result.limitations),
    )
    return CapabilityEvidenceEnvelope(
        evidence_ref=result.evidence_ref,
        capability_id=request.capability_id,
        question_family=request.question_family,
        target_claim=request.target_claim,
        claim_type=request.claim_type,
        metric=request.metric,
        scope=request.scope,
        grain=request.grain,
        baseline_label=str(request.baseline.get("label", "")),
        target_label=str(request.target.get("label", "")),
        time_window=request.time_window,
        numeric_facts={
            "median_uplift": payload.get("median_uplift"),
            "direction_ratio": payload.get("direction_ratio"),
            "direction_consistency_ratio": payload.get("direction_consistency_ratio"),
            "materiality_hit_ratio": payload.get("materiality_hit_ratio"),
            "comparable_periods": payload.get("comparable_periods"),
        },
        typed_payload=payload,
        result_refs=result_refs,
        sql_hashes=sql_hashes,
        evidence_type=evidence_type,
        strength=strength,
        wording_limit=wording_limit,
        limitations=limitations,
        disabled_degraded_blocked_path_refs=(),
        verifier_handoff={
            "requires_baseline_label": str(request.baseline.get("label", "")),
            "requires_target_label": str(request.target.get("label", "")),
            "requires_evidence_ref": result.evidence_ref,
        },
        admin_audit_ref=f"capability:{request.run_id}:{request.capability_id}",
        **_bound_provenance(request),
    )


def _execute_data_quality_profile(
    request: CapabilityRequest,
) -> CapabilityEvidenceEnvelope:
    params: dict[str, Any] = dict(request.params)
    rows = _capability_rows(request, params.pop("rows", ()))
    legacy_refs = params.pop("result_refs", ())
    result_refs = _result_refs(request, legacy_refs)
    sql_hashes = _sql_hashes(request, legacy_refs)
    result = data_quality_check(
        rows,
        required_fields=tuple(params.pop("required_fields", ())),
        result_refs=result_refs,
    )
    payload = dict(result.typed_payload)
    row_count = payload.get("row_count")
    evidence_type, strength, wording_limit, limitations = _evidence_boundary(
        request,
        evidence_type=result.evidence_type,
        strength=result.strength,
        wording_limit=result.wording_limit,
        limitations=tuple(result.limitations),
    )
    return CapabilityEvidenceEnvelope(
        evidence_ref=f"{request.capability_id}:{request.run_id}",
        capability_id=request.capability_id,
        question_family=request.question_family,
        target_claim=request.target_claim,
        claim_type=request.claim_type,
        metric=request.metric,
        scope=request.scope,
        grain=request.grain,
        baseline_label=str(request.baseline.get("label", "")),
        target_label=str(request.target.get("label", "")),
        time_window=request.time_window,
        numeric_facts={"row_count": row_count},
        typed_payload=payload,
        result_refs=result_refs,
        sql_hashes=sql_hashes,
        evidence_type=evidence_type,
        strength=strength,
        wording_limit=wording_limit,
        limitations=limitations,
        disabled_degraded_blocked_path_refs=(),
        verifier_handoff={
            "requires_evidence_ref": f"{request.capability_id}:{request.run_id}",
        },
        admin_audit_ref=f"capability:{request.run_id}:{request.capability_id}",
        **_bound_provenance(request),
    )


def _execute_window_metric_compare(
    request: CapabilityRequest,
) -> CapabilityEvidenceEnvelope:
    limitations: tuple[str, ...] = ()
    comparison: WindowMetricComparison | None = None
    if _legacy_fixture_allowed(request):
        comparison = _legacy_window_metric_comparison(request)
        if comparison is None:
            limitations = ("window_pair_cardinality_invalid",)
    else:
        comparison = _authoritative_window_metric_comparison(request)
    payload = comparison.to_payload() if comparison is not None else {
        "metric": request.metric,
        "target_window_id": "",
        "baseline_window_id": "",
        "target_value": None,
        "baseline_value": None,
        "absolute_change": None,
        "relative_change": None,
        "comparisons": (),
    }
    evidence_type, strength, wording_limit, limitations = _evidence_boundary(
        request,
        evidence_type="statistical_association" if not limitations else "insufficient",
        strength="directional" if not limitations else "low",
        wording_limit="quantified" if not limitations else "insufficient",
        limitations=limitations,
    )
    result_refs = _result_refs(request, ())
    numeric_facts = {
        key: payload[key]
        for key in (
            "target_value",
            "baseline_value",
            "absolute_change",
            "relative_change",
        )
    }
    return CapabilityEvidenceEnvelope(
        evidence_ref=f"{request.capability_id}:{request.run_id}",
        capability_id=request.capability_id,
        question_family=request.question_family,
        target_claim=request.target_claim,
        claim_type=request.claim_type,
        metric=request.metric,
        scope=request.scope,
        grain=request.grain,
        baseline_label=str(request.baseline.get("label", "")),
        target_label=str(request.target.get("label", "")),
        time_window=request.time_window,
        numeric_facts=numeric_facts,
        typed_payload=payload,
        result_refs=result_refs,
        sql_hashes=_sql_hashes(request, ()),
        evidence_type=evidence_type,
        strength=strength,
        wording_limit=wording_limit,
        limitations=limitations,
        disabled_degraded_blocked_path_refs=(),
        verifier_handoff={
            "requires_evidence_ref": f"{request.capability_id}:{request.run_id}",
            "requires_bound_result_refs": result_refs,
        },
        admin_audit_ref=f"capability:{request.run_id}:{request.capability_id}",
        **_bound_provenance(request),
    )


def _authoritative_window_metric_comparison(
    request: CapabilityRequest,
) -> WindowMetricComparison:
    bound = _bound_input(request)
    if (
        bound is None
        or not bound.binding_manifest_ref
        or request.evidence_resolver is None
        or request.rows_loader is None
        or request.runtime_registry is None
    ):
        raise WindowMetricEvidenceError("window_metric_authority_missing")
    binding = request.evidence_resolver.resolve_capability_binding(
        bound.binding_manifest_ref
    )
    chain = validate_authoritative_query_chain(
        binding,
        resolver=request.evidence_resolver,
        rows_loader=request.rows_loader,
        runtime_registry=request.runtime_registry,
        release_resolver=request.release_resolver,
    )
    if len(chain.primary_results) != 1:
        raise WindowMetricEvidenceError("window_metric_query_cardinality_invalid")
    result = chain.primary_results[0]
    contract = chain.query_records[result.result_ref].contract
    return aggregate_window_metric_comparison(
        contract,
        result.rows,
        metric_id=request.metric,
    )


def _legacy_window_metric_comparison(
    request: CapabilityRequest,
) -> WindowMetricComparison | None:
    rows = tuple(dict(row) for row in _capability_rows(request, ()))
    targets = tuple(row for row in rows if row.get("window_role") == "target")
    baselines = tuple(row for row in rows if row.get("window_role") == "baseline")
    if len(targets) != 1 or len(baselines) != 1:
        return None
    # Legacy fixture mode is explicitly non-authoritative; retain its narrow two-row path.
    target_value = _legacy_decimal(targets[0].get(request.metric))
    baseline_value = _legacy_decimal(baselines[0].get(request.metric))
    if target_value is None or baseline_value is None:
        return None
    aggregates = tuple(
        WindowMetricAggregate(
            window_id=str(row.get("window_id") or ""),
            role=str(row.get("window_role") or ""),
            aggregation="daily_total",
            required_complete_days=1,
            value=value,
            observations=(
                WindowMetricObservation(
                    str(row.get("observation_key") or ""), value
                ),
            ),
        )
        for row, value in zip((*targets, *baselines), (target_value, baseline_value))
    )
    return WindowMetricComparison(request.metric, aggregates[0], aggregates[1], ())


def _legacy_decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _execute_context_capability(
    request: CapabilityRequest,
) -> CapabilityEvidenceEnvelope:
    rows = tuple(dict(row) for row in _capability_rows(request, ()))
    result_refs = _result_refs(request, ())
    sql_hashes = _sql_hashes(request, ())
    if request.capability_id == "event_evidence":
        context_rows = tuple(
            row
            for row in rows
            if not str(row.get("event_id") or "").startswith("__no_event__:")
        )
        payload = {
            "events": context_rows,
            "event_count": len(context_rows),
            "zero_event_windows": tuple(
                str(row.get("window_id") or "")
                for row in rows
                if str(row.get("event_id") or "").startswith("__no_event__:")
            ),
            "claim_boundary": (
                "Window overlap is reviewed context for a candidate mechanism; "
                "it does not establish causal impact."
            ),
        }
        evidence_type = "candidate_mechanism" if context_rows else "insufficient"
        strength = "medium" if context_rows else "insufficient"
        wording_limit = "candidate" if context_rows else "insufficient"
        limitations = () if context_rows else ("no_event_matches",)
        numeric_facts = {"event_count": len(context_rows)}
    else:
        total = Decimal(0)
        numeric_count = 0
        for row in rows:
            value = row.get("player_bet_amount")
            if value is None:
                continue
            try:
                total += Decimal(str(value))
            except (InvalidOperation, ValueError):
                continue
            numeric_count += 1
        payload = {
            "activity_rows": rows,
            "activity_metric": "player_bet_amount",
            "claim_boundary": (
                "Gameplay activity is operational context and cannot be relabeled "
                "as payment or revenue."
            ),
        }
        evidence_type = "observed" if rows else "insufficient"
        strength = "observed" if rows else "low"
        wording_limit = "contextual" if rows else "insufficient"
        limitations = () if rows else ("no_gameplay_activity_rows",)
        numeric_facts = {
            "activity_row_count": len(rows),
            "player_bet_amount_total": total if numeric_count else None,
        }
    evidence_type, strength, wording_limit, limitations = _evidence_boundary(
        request,
        evidence_type=evidence_type,
        strength=strength,
        wording_limit=wording_limit,
        limitations=limitations,
    )
    return CapabilityEvidenceEnvelope(
        evidence_ref=f"{request.capability_id}:{request.run_id}",
        capability_id=request.capability_id,
        question_family=request.question_family,
        target_claim=request.target_claim,
        claim_type=request.claim_type,
        metric=request.metric,
        scope=request.scope,
        grain=request.grain,
        baseline_label=str(request.baseline.get("label", "")),
        target_label=str(request.target.get("label", "")),
        time_window=request.time_window,
        numeric_facts=numeric_facts,
        typed_payload=payload,
        result_refs=result_refs,
        sql_hashes=sql_hashes,
        evidence_type=evidence_type,
        strength=strength,
        wording_limit=wording_limit,
        limitations=limitations,
        disabled_degraded_blocked_path_refs=(),
        verifier_handoff={
            "requires_evidence_ref": f"{request.capability_id}:{request.run_id}",
            "requires_bound_result_refs": result_refs,
        },
        admin_audit_ref=f"capability:{request.run_id}:{request.capability_id}",
        **_bound_provenance(request),
    )
def _budget_limitation(request: CapabilityRequest) -> str:
    params = request.params
    if params.get("timeout_exceeded"):
        return "capability_timeout"
    if should_ask_before_more_exploration(request.budget_state):
        return "capability_budget_exhausted"
    bound = _bound_input(request)
    rows = (
        tuple(
            row
            for slot_rows in bound.rows_by_slot.values()
            for row in slot_rows
        )
        if bound is not None
        else params.get("rows", ())
    )
    row_budget = _positive_int(params.get("row_budget", 5000), 5000)
    if isinstance(rows, (list, tuple)) and len(rows) > row_budget:
        return "row_budget_exceeded"
    result_refs = (
        (*bound.result_refs, *bound.validation_result_refs)
        if bound is not None
        else tuple(params.get("result_refs", ()))
    )
    result_ref_budget = _positive_int(params.get("result_ref_budget", 100), 100)
    if len(result_refs) > result_ref_budget:
        return "result_ref_budget_exceeded"
    return ""


def _blocked_envelope(request: CapabilityRequest, limitation: str) -> CapabilityEvidenceEnvelope:
    result_refs = _result_refs(request, request.params.get("result_refs", ()))
    rows = request.params.get("rows", ())
    row_count = len(rows) if isinstance(rows, (list, tuple)) else None
    return CapabilityEvidenceEnvelope(
        evidence_ref=f"{request.capability_id}:{request.run_id}:blocked",
        capability_id=request.capability_id,
        question_family=request.question_family,
        target_claim=request.target_claim,
        claim_type=request.claim_type,
        metric=request.metric,
        scope=request.scope,
        grain=request.grain,
        baseline_label=str(request.baseline.get("label", "")),
        target_label=str(request.target.get("label", "")),
        time_window=request.time_window,
        numeric_facts={},
        typed_payload={
            "status": "blocked",
            "limitation": limitation,
            "row_count": row_count,
            "used_capability_calls": request.budget_state.used_capability_calls,
            "hard_limit": request.budget_state.hard_limit,
        },
        result_refs=result_refs,
        sql_hashes=_sql_hashes(request, request.params.get("result_refs", ())),
        evidence_type="insufficient",
        strength="low",
        wording_limit="blocked",
        limitations=(limitation,),
        disabled_degraded_blocked_path_refs=(limitation,),
        verifier_handoff={"blocked_by": limitation},
        admin_audit_ref=f"capability:{request.run_id}:{request.capability_id}",
        **_bound_provenance(request),
    )


def _bound_input(request: CapabilityRequest) -> BoundCapabilityInput | None:
    value = request.bound_input
    return value if isinstance(value, BoundCapabilityInput) else None


def _bound_input_limitation(request: CapabilityRequest) -> str:
    bound = _bound_input(request)
    if bound is None:
        return ""
    validation_reason = validate_bound_capability_input(
        bound,
        request.evidence_resolver,
        allow_fixture=_legacy_fixture_allowed(request),
    )
    if validation_reason:
        return validation_reason
    if bound.capability_id != request.capability_id:
        return "bound_capability_mismatch"
    if bound.status == "blocked":
        return "bound_capability_input_blocked"
    if request.claim_type not in bound.supported_claim_types:
        return "unsupported_claim_type"
    return ""


def _capability_rows(
    request: CapabilityRequest,
    unbound_rows: Any,
) -> tuple[Mapping[str, Any], ...] | Any:
    bound = _bound_input(request)
    if bound is None:
        return unbound_rows
    requested_slot = str(request.params.get("input_slot_id") or "")
    if requested_slot:
        return bound.rows_by_slot.get(requested_slot, ())
    if len(bound.rows_by_slot) == 1:
        return next(iter(bound.rows_by_slot.values()))
    requested_slots = request.params.get("input_slot_ids")
    if isinstance(requested_slots, (list, tuple)):
        return tuple(
            row
            for slot_id in requested_slots
            for row in bound.rows_by_slot.get(str(slot_id), ())
        )
    return ()


def _result_refs(request: CapabilityRequest, legacy_refs: Any) -> tuple[str, ...]:
    bound = _bound_input(request)
    if bound is not None:
        return _dedupe((*bound.result_refs, *bound.validation_result_refs))
    if _legacy_fixture_allowed(request):
        return tuple(str(ref) for ref in legacy_refs if ref)
    return ()


def _sql_hashes(request: CapabilityRequest, legacy_refs: Any) -> tuple[str, ...]:
    if (
        _bound_input(request) is None and _legacy_fixture_allowed(request)
    ):
        return tuple(str(ref) for ref in legacy_refs if ref)
    return ()


def _bound_provenance(request: CapabilityRequest) -> dict[str, Any]:
    bound = _bound_input(request)
    if bound is None or validate_bound_capability_input(
        bound,
        request.evidence_resolver,
        allow_fixture=_legacy_fixture_allowed(request),
    ):
        return {
            "analysis_contract_ref": "",
            "capability_contract_ref": "",
            "query_contract_refs": (),
            "query_execution_record_refs": (),
            "query_execution_record_digests": (),
            "rows_metadata_record_refs": (),
            "rows_metadata_record_digests": (),
            "completeness_report_refs": (),
            "completeness_record_refs": (),
            "completeness_record_digests": (),
            "source_snapshot_refs": (),
            "supported_evidence_types": (),
            "supported_claim_types": (),
            "maximum_claim_strength": "",
            "maximum_claim_strength_rank": -1,
            "claim_strength_taxonomy_version": "",
            "input_status": "fixture" if _legacy_fixture_allowed(request) else "blocked",
            "input_completeness_statuses": (),
            "binding_manifest_ref": "",
            "binding_manifest_digest": "",
        }
    return {
        "analysis_contract_ref": bound.analysis_contract_ref,
        "capability_contract_ref": bound.capability_contract_ref,
        "query_contract_refs": _dedupe(
            (*bound.query_contract_refs, *bound.validation_query_contract_refs)
        ),
        "query_execution_record_refs": _dedupe(
            (
                *bound.query_execution_record_refs,
                *bound.validation_query_execution_record_refs,
            )
        ),
        "query_execution_record_digests": _dedupe(
            (
                *bound.query_execution_record_digests,
                *bound.validation_query_execution_record_digests,
            )
        ),
        "rows_metadata_record_refs": _dedupe(
            (
                *bound.rows_metadata_record_refs,
                *bound.validation_rows_metadata_record_refs,
            )
        ),
        "rows_metadata_record_digests": _dedupe(
            (
                *bound.rows_metadata_record_digests,
                *bound.validation_rows_metadata_record_digests,
            )
        ),
        "completeness_report_refs": _dedupe(
            (
                *bound.completeness_report_refs,
                *bound.validation_completeness_report_refs,
            )
        ),
        "completeness_record_refs": _dedupe(
            (
                *bound.completeness_record_refs,
                *bound.validation_completeness_record_refs,
            )
        ),
        "completeness_record_digests": _dedupe(
            (
                *bound.completeness_record_digests,
                *bound.validation_completeness_record_digests,
            )
        ),
        "source_snapshot_refs": _dedupe(
            (*bound.source_snapshot_refs, *bound.validation_source_snapshot_refs)
        ),
        "supported_claim_types": bound.supported_claim_types,
        "supported_evidence_types": bound.supported_evidence_types,
        "maximum_claim_strength": bound.maximum_claim_strength,
        "maximum_claim_strength_rank": bound.maximum_claim_strength_rank,
        "claim_strength_taxonomy_version": bound.claim_strength_taxonomy_version,
        "input_status": bound.status,
        "input_completeness_statuses": bound.input_completeness_statuses,
        "binding_manifest_ref": bound.binding_manifest_ref,
        "binding_manifest_digest": getattr(bound, "binding_manifest_digest", ""),
    }


def _evidence_boundary(
    request: CapabilityRequest,
    *,
    evidence_type: str,
    strength: str,
    wording_limit: str,
    limitations: tuple[str, ...],
) -> tuple[str, str, str, tuple[str, ...]]:
    bound = _bound_input(request)
    if bound is None or not bound.binding_manifest_ref:
        if _legacy_fixture_allowed(request):
            return (
                evidence_type,
                strength,
                wording_limit,
                _dedupe((*limitations, "legacy_fixture_non_authoritative")),
            )
        return evidence_type, strength, wording_limit, limitations
    if evidence_type not in bound.supported_evidence_types:
        return (
            "insufficient",
            "low",
            "blocked",
            _dedupe((*limitations, "unsupported_evidence_type")),
        )
    if bound.status == "degraded":
        return (
            evidence_type,
            "low",
            "contextual",
            _dedupe((*limitations, *bound.reasons)),
        )
    return evidence_type, strength, wording_limit, limitations


def _dedupe(values: Any) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in values if value))


def _legacy_fixture_allowed(request: CapabilityRequest) -> bool:
    return bool(
        request.fixture_input_mode == "legacy_unbound_fixture"
        and legacy_fixture_enabled(request.run_mode)
    )


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default
