from __future__ import annotations

from typing import Any

from bi_agent.capabilities.data_quality_check import data_quality_check
from bi_agent.capabilities.pattern_scan import scan_pattern
from bi_agent.runtime.capability_models import (
    CapabilityEvidenceEnvelope,
    CapabilityRequest,
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


def execute_capability(request: CapabilityRequest) -> CapabilityEvidenceEnvelope:
    if request.capability_id in PATTERN_COMPARE_CAPABILITIES:
        return _execute_pattern_compare(request)
    if request.capability_id == "data_quality_profile":
        return _execute_data_quality_profile(request)
    raise KeyError(f"unsupported capability_id: {request.capability_id}")


def _execute_pattern_compare(
    request: CapabilityRequest,
) -> CapabilityEvidenceEnvelope:
    params: dict[str, Any] = dict(request.params)
    rows = params.pop("rows")
    result_refs = tuple(params.pop("result_refs", ()))
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
        sql_hashes=result_refs,
        evidence_type=result.evidence_type,
        strength=result.strength,
        wording_limit=result.wording_limit,
        limitations=tuple(result.limitations),
        disabled_degraded_blocked_path_refs=(),
        verifier_handoff={
            "requires_baseline_label": str(request.baseline.get("label", "")),
            "requires_target_label": str(request.target.get("label", "")),
            "requires_evidence_ref": result.evidence_ref,
        },
        admin_audit_ref=f"capability:{request.run_id}:{request.capability_id}",
    )


def _execute_data_quality_profile(
    request: CapabilityRequest,
) -> CapabilityEvidenceEnvelope:
    params: dict[str, Any] = dict(request.params)
    rows = params.pop("rows")
    result_refs = tuple(params.pop("result_refs", ()))
    result = data_quality_check(
        rows,
        required_fields=tuple(params.pop("required_fields", ())),
        result_refs=result_refs,
    )
    payload = dict(result.typed_payload)
    row_count = payload.get("row_count")
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
        sql_hashes=result_refs,
        evidence_type=result.evidence_type,
        strength=result.strength,
        wording_limit=result.wording_limit,
        limitations=tuple(result.limitations),
        disabled_degraded_blocked_path_refs=(),
        verifier_handoff={
            "requires_evidence_ref": f"{request.capability_id}:{request.run_id}",
        },
        admin_audit_ref=f"capability:{request.run_id}:{request.capability_id}",
    )
