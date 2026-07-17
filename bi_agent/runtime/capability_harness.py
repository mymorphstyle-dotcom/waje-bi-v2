from __future__ import annotations

from math import isfinite
from typing import Any, Mapping

from bi_agent.capabilities.candidate_crosswalk import candidate_crosswalk
from bi_agent.capabilities.cross_source_association import cross_source_association
from bi_agent.capabilities.cross_source_panel_association import (
    cross_source_panel_association,
)
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
from bi_agent.runtime.window_metric_evidence import (
    WindowMetricComparison,
    WindowMetricEvidenceError,
    aggregate_window_metric_comparison,
)

PATTERN_COMPARE_CAPABILITIES = frozenset(
    {
        "compare_period_phases",
        "rolling_window_compare",
        "weekday_calendar_compare",
        "event_window_compare",
    }
)
WINDOW_METRIC_COMPARE_CAPABILITIES = frozenset(
    {"compare_periods", "market_health_compare"}
)
_ASSOCIATION_OUTCOME_SLOT = "association_outcome_timeseries"
_ASSOCIATION_CANDIDATE_SLOT = "association_candidate_timeseries"
_ASSOCIATION_STRUCTURAL_FIELDS = frozenset(
    {
        "window_id",
        "window_role",
        "observation_key",
        "business_date",
        "date",
        "period",
        "group",
        "calendar_week",
        "weekday",
        "month_phase",
        "channel",
        "source_row_count",
    }
)
_ASSOCIATION_OPTION_KEYS = frozenset(
    {
        "methods",
        "transforms",
        "lags",
        "min_samples",
        "rolling_window",
        "rolling_step",
        "min_rolling_windows",
        "stability_direction_ratio",
        "min_abs_correlation",
        "alpha",
        "fdr_method",
    }
)
_PANEL_CANDIDATE_RULES = (
    "unicode_casefold",
    "remove_non_alphanumeric",
    "strip_paid_source_prefix_pa",
)
_PANEL_OPTION_KEYS = frozenset(
    {
        "min_samples",
        "min_panels",
        "min_panel_samples",
        "min_pair_coverage",
        "min_mapping_coverage",
        "min_direction_stability",
        "residual_tolerance",
        "max_iterations",
    }
)
_PANEL_TRANSFORM_ALIASES = {
    "level": "level",
    "difference": "difference",
    "daily_change": "difference",
    "absolute_change": "difference",
    "log_difference": "log_difference",
    "log_change": "log_difference",
    "log_return": "log_difference",
    "signed_log_difference": "signed_log_difference",
    "signed_log_change": "signed_log_difference",
}
_PANEL_RATIO_METRICS = frozenset(
    {
        "paid_frequency",
        "avg_order_amount",
        "payment_success_rate",
        "player_avg_bet_amount",
    }
)
_PANEL_DEFAULT_ROW_BUDGET = 250_000


def execute_capability(request: CapabilityRequest) -> CapabilityEvidenceEnvelope:
    if _bound_input(request) is None:
        return _blocked_envelope(request, "missing_bound_capability_input")
    input_limitation = _bound_input_limitation(request)
    if input_limitation:
        return _blocked_envelope(request, input_limitation)
    budget_limitation = _budget_limitation(request)
    if budget_limitation:
        return _blocked_envelope(request, budget_limitation)
    if request.capability_id == "compare_periods":
        return _execute_window_metric_compare(request)
    if request.capability_id in PATTERN_COMPARE_CAPABILITIES:
        return _execute_pattern_compare(request)
    if request.capability_id in WINDOW_METRIC_COMPARE_CAPABILITIES:
        return _execute_window_metric_compare(request)
    if request.capability_id == "data_quality_profile":
        return _execute_data_quality_profile(request)
    if request.capability_id == "cross_source_association":
        return _execute_cross_source_association(request)
    if request.capability_id == "cross_source_panel_association":
        return _execute_cross_source_panel_association(request)
    if request.capability_id == "event_evidence":
        return _execute_context_capability(request)
    raise KeyError(f"unsupported capability_id: {request.capability_id}")


def _execute_pattern_compare(
    request: CapabilityRequest,
) -> CapabilityEvidenceEnvelope:
    params: dict[str, Any] = dict(request.params)
    params.pop("rows", None)
    params.pop("result_refs", None)
    rows = _capability_rows(request)
    result_refs = _result_refs(request)
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
        sql_hashes=(),
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
    params.pop("rows", None)
    params.pop("result_refs", None)
    rows = _capability_rows(request)
    result_refs = _result_refs(request)
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
        sql_hashes=(),
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
    comparison = _authoritative_window_metric_comparison(request)
    payload = comparison.to_payload()
    evidence_type, strength, wording_limit, limitations = _evidence_boundary(
        request,
        evidence_type="statistical_association" if not limitations else "insufficient",
        strength="directional" if not limitations else "low",
        wording_limit="quantified" if not limitations else "insufficient",
        limitations=limitations,
    )
    result_refs = _result_refs(request)
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
        sql_hashes=(),
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
        primary_baseline_window_id=str(
            request.params.get("primary_baseline_window_id") or ""
        ),
    )


def _execute_context_capability(
    request: CapabilityRequest,
) -> CapabilityEvidenceEnvelope:
    rows = tuple(dict(row) for row in _capability_rows(request))
    result_refs = _result_refs(request)
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
        sql_hashes=(),
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


def _execute_cross_source_association(
    request: CapabilityRequest,
) -> CapabilityEvidenceEnvelope:
    bound = _bound_input(request)
    if bound is None:
        return _association_insufficient_envelope(
            request,
            "missing_bound_capability_input",
        )
    aligned, limitation = _align_cross_source_rows(bound)
    if limitation:
        return _association_insufficient_envelope(
            request,
            limitation,
            typed_payload={"alignment": _alignment_summary(aligned)},
        )

    rows = tuple(aligned["rows"])
    outcome_metrics = tuple(aligned["outcome_metrics"])
    candidate_metrics = tuple(aligned["candidate_metrics"])
    requested_primary = str(
        request.params.get("target_key") or request.metric or ""
    )
    if not requested_primary:
        return _association_insufficient_envelope(
            request,
            "association_primary_outcome_missing",
            typed_payload={"alignment": _alignment_summary(aligned)},
        )

    options = {
        key: value
        for key, value in request.params.items()
        if key in _ASSOCIATION_OPTION_KEYS and value is not None
    }
    result_refs = _result_refs(request)
    associations_by_outcome: dict[str, Mapping[str, Any]] = {}
    raw_results: dict[str, Any] = {}
    for outcome_metric in outcome_metrics:
        candidates = tuple(
            metric for metric in candidate_metrics if metric != outcome_metric
        )
        if not candidates:
            associations_by_outcome[outcome_metric] = {
                "evidence_type": "insufficient",
                "strength": "low",
                "wording_limit": "insufficient",
                "numeric_facts": {},
                "association": {},
                "limitations": ("association_candidate_metrics_missing",),
            }
            continue
        try:
            result = cross_source_association(
                rows,
                target_key=outcome_metric,
                candidate_keys=candidates,
                time_key="observation_key",
                result_refs=result_refs,
                evidence_ref=(
                    f"{request.capability_id}:{request.run_id}:{outcome_metric}"
                ),
                **options,
            )
        except (TypeError, ValueError) as exc:
            associations_by_outcome[outcome_metric] = {
                "evidence_type": "insufficient",
                "strength": "low",
                "wording_limit": "insufficient",
                "numeric_facts": {},
                "association": {},
                "limitations": (
                    f"association_evaluation_invalid:{type(exc).__name__}",
                ),
            }
            continue
        raw_results[outcome_metric] = result
        associations_by_outcome[outcome_metric] = {
            "evidence_ref": result.evidence_ref,
            "evidence_type": result.evidence_type,
            "strength": result.strength,
            "wording_limit": result.wording_limit,
            "numeric_facts": dict(result.numeric_facts),
            "association": dict(result.typed_payload),
            "limitations": tuple(result.limitations),
        }

    primary_result = raw_results.get(requested_primary)
    if primary_result is None:
        return _association_insufficient_envelope(
            request,
            f"association_primary_outcome_unavailable:{requested_primary}",
            typed_payload={
                "alignment": _alignment_summary(aligned),
                "primary_outcome": requested_primary,
                "associations_by_outcome": associations_by_outcome,
            },
        )

    primary_supported = primary_result.evidence_type == "statistical_association"
    if primary_supported:
        evidence_type, strength, wording_limit, limitations = _evidence_boundary(
            request,
            evidence_type=primary_result.evidence_type,
            strength=primary_result.strength,
            wording_limit=primary_result.wording_limit,
            limitations=tuple(primary_result.limitations),
        )
    else:
        evidence_type = "insufficient"
        strength = "low"
        wording_limit = "insufficient"
        limitations = _dedupe(
            (
                *tuple(primary_result.limitations),
                *tuple(bound.reasons if bound.status == "degraded" else ()),
            )
        )

    numeric_facts = {
        **dict(primary_result.numeric_facts),
        "outcome_metric_count": len(outcome_metrics),
        "candidate_metric_count": len(candidate_metrics),
        "aligned_observation_count": aligned["aligned_observation_count"],
    }
    evidence_ref = f"{request.capability_id}:{request.run_id}"
    return CapabilityEvidenceEnvelope(
        evidence_ref=evidence_ref,
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
        typed_payload={
            "status": (
                "supported"
                if evidence_type == "statistical_association"
                and wording_limit != "blocked"
                else "insufficient"
            ),
            "analysis_role": "cross_source_association",
            "primary_outcome": requested_primary,
            "outcome_metrics": outcome_metrics,
            "candidate_metrics": candidate_metrics,
            "alignment": _alignment_summary(aligned),
            "associations_by_outcome": associations_by_outcome,
            "claim_ceiling": "candidate_driver",
            "causal_claim_allowed": False,
            "correlation_coefficient_is_contribution": False,
        },
        result_refs=result_refs,
        sql_hashes=(),
        evidence_type=evidence_type,
        strength=strength,
        wording_limit=wording_limit,
        limitations=limitations,
        disabled_degraded_blocked_path_refs=(),
        verifier_handoff={
            "requires_evidence_ref": evidence_ref,
            "requires_bound_result_refs": result_refs,
            "claim_ceiling": "candidate_driver",
            "causal_claim_allowed": False,
            "correlation_coefficient_is_contribution": False,
        },
        admin_audit_ref=f"capability:{request.run_id}:{request.capability_id}",
        **_bound_provenance(request),
    )


def _execute_cross_source_panel_association(
    request: CapabilityRequest,
) -> CapabilityEvidenceEnvelope:
    bound = _bound_input(request)
    if bound is None:
        return _panel_association_insufficient_envelope(
            request,
            "missing_bound_capability_input",
        )
    outcome_rows = tuple(bound.rows_by_slot.get(_ASSOCIATION_OUTCOME_SLOT, ()))
    candidate_rows = tuple(bound.rows_by_slot.get(_ASSOCIATION_CANDIDATE_SLOT, ()))
    if not outcome_rows:
        return _panel_association_insufficient_envelope(
            request,
            "panel_association_outcome_rows_missing",
        )
    if not candidate_rows:
        return _panel_association_insufficient_envelope(
            request,
            "panel_association_candidate_rows_missing",
        )

    outcome_rows, outcome_metrics, limitation = _dedupe_panel_slot_rows(
        outcome_rows,
        slot_id=_ASSOCIATION_OUTCOME_SLOT,
    )
    if limitation:
        return _panel_association_insufficient_envelope(request, limitation)
    candidate_rows, candidate_metrics, limitation = _dedupe_panel_slot_rows(
        candidate_rows,
        slot_id=_ASSOCIATION_CANDIDATE_SLOT,
    )
    if limitation:
        return _panel_association_insufficient_envelope(request, limitation)
    if not outcome_metrics:
        return _panel_association_insufficient_envelope(
            request,
            "panel_association_outcome_metrics_missing",
        )
    if not candidate_metrics:
        return _panel_association_insufficient_envelope(
            request,
            "panel_association_candidate_metrics_missing",
        )

    hypotheses, limitation = _normalize_panel_hypotheses(
        request.params.get("hypotheses")
    )
    if limitation:
        return _panel_association_insufficient_envelope(request, limitation)

    try:
        crosswalk = candidate_crosswalk(
            outcome_rows,
            candidate_rows,
            time_key="observation_key",
            group_key="channel",
            metric_strategies={
                "left": {
                    metric: _panel_metric_strategy(metric)
                    for metric in outcome_metrics
                },
                "right": {
                    metric: _panel_metric_strategy(metric)
                    for metric in candidate_metrics
                },
            },
            candidate_rules=_PANEL_CANDIDATE_RULES,
            mapped_group_key="mapped_channel",
        )
    except (TypeError, ValueError) as exc:
        return _panel_association_insufficient_envelope(
            request,
            f"panel_crosswalk_invalid:{type(exc).__name__}",
        )

    mapping_summary = dict(crosswalk["mapping_summary"])
    aligned_rows = tuple(crosswalk["aligned_rows"])
    if not aligned_rows:
        return _panel_association_insufficient_envelope(
            request,
            "panel_crosswalk_has_no_aligned_cells",
            typed_payload={
                "mapping": _panel_mapping_summary(crosswalk),
            },
        )

    requested_primary = str(
        request.params.get("target_key") or request.metric or ""
    )
    if not requested_primary:
        return _panel_association_insufficient_envelope(
            request,
            "panel_association_primary_outcome_missing",
        )
    options = {
        key: value
        for key, value in request.params.items()
        if key in _PANEL_OPTION_KEYS and value is not None
    }
    result_refs = _result_refs(request)
    associations_by_hypothesis: dict[str, Mapping[str, Any]] = {}
    hypothesis_ids_by_outcome: dict[str, list[str]] = {}
    statistical_hypothesis_count = 0
    outcome_metric_set = set(outcome_metrics)
    candidate_metric_set = set(candidate_metrics)
    for hypothesis in hypotheses:
        hypothesis_id = hypothesis["hypothesis_id"]
        outcome_metric = hypothesis["outcome_metric"]
        candidate_metric = hypothesis["candidate_metric"]
        hypothesis_ids_by_outcome.setdefault(outcome_metric, []).append(
            hypothesis_id
        )
        if outcome_metric not in outcome_metric_set:
            associations_by_hypothesis[hypothesis_id] = (
                _panel_hypothesis_insufficient_bundle(
                    hypothesis,
                    f"panel_hypothesis_outcome_metric_missing:{outcome_metric}",
                )
            )
            continue
        if candidate_metric not in candidate_metric_set:
            associations_by_hypothesis[hypothesis_id] = (
                _panel_hypothesis_insufficient_bundle(
                    hypothesis,
                    f"panel_hypothesis_candidate_metric_missing:{candidate_metric}",
                )
            )
            continue

        mapping_context = _panel_mapping_coverage_context(
            mapping_summary,
            outcome_metric=outcome_metric,
            candidate_metric=candidate_metric,
        )
        capability_hypothesis = {
            "hypothesis_id": hypothesis_id,
            "outcome_key": f"left_{outcome_metric}",
            "candidate_key": f"right_{candidate_metric}",
            "transform": hypothesis["transform"],
            "lag": hypothesis["lag"],
        }
        try:
            result = cross_source_panel_association(
                aligned_rows,
                time_key="observation_key",
                panel_key="mapped_channel",
                hypothesis=capability_hypothesis,
                mapping_authority_status="candidate_mechanical_crosswalk",
                mapping_coverage=mapping_context["coverage"],
                mapping_coverage_basis=mapping_context["coverage_basis"],
                result_refs=result_refs,
                evidence_ref=(
                    f"{request.capability_id}:{request.run_id}:{hypothesis_id}"
                ),
                **options,
            )
        except (TypeError, ValueError) as exc:
            associations_by_hypothesis[hypothesis_id] = (
                _panel_hypothesis_insufficient_bundle(
                    hypothesis,
                    f"panel_association_evaluation_invalid:{type(exc).__name__}",
                )
            )
            continue
        if result.evidence_type == "statistical_association":
            statistical_hypothesis_count += 1
        associations_by_hypothesis[hypothesis_id] = {
            "hypothesis": hypothesis,
            "evidence_ref": result.evidence_ref,
            "evidence_type": result.evidence_type,
            "strength": result.strength,
            "wording_limit": result.wording_limit,
            "numeric_facts": dict(result.numeric_facts),
            "association": dict(result.typed_payload),
            "limitations": tuple(result.limitations),
        }

    requested_hypothesis_count = len(hypotheses)
    common_limitations = (
        "mechanical_crosswalk_has_no_mapping_authority",
        "panel_association_is_sensitivity_only",
        "specific_channel_claims_are_not_allowed",
        "panel_association_cannot_establish_causality_or_contribution",
    )
    if statistical_hypothesis_count:
        evidence_type, strength, wording_limit, limitations = _evidence_boundary(
            request,
            evidence_type="statistical_association",
            strength="low",
            wording_limit="sensitivity_only",
            limitations=common_limitations,
        )
        if wording_limit != "blocked":
            strength = "low"
            wording_limit = "sensitivity_only"
    else:
        evidence_type = "insufficient"
        strength = "low"
        wording_limit = "sensitivity_only"
        limitations = common_limitations

    overall_summary = {
        "requested_hypothesis_count": requested_hypothesis_count,
        "evaluated_hypothesis_count": len(associations_by_hypothesis),
        "statistical_hypothesis_count": statistical_hypothesis_count,
        "insufficient_hypothesis_count": (
            requested_hypothesis_count - statistical_hypothesis_count
        ),
        "mapped_channel_pair_count": mapping_summary.get("pair_count", 0),
        "aligned_panel_cell_count": mapping_summary.get("aligned_cell_count", 0),
        "complete_aligned_panel_cell_count": mapping_summary.get(
            "complete_aligned_cell_count",
            0,
        ),
    }
    evidence_ref = f"{request.capability_id}:{request.run_id}"
    return CapabilityEvidenceEnvelope(
        evidence_ref=evidence_ref,
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
        numeric_facts=overall_summary,
        typed_payload={
            "status": (
                "supported_sensitivity"
                if statistical_hypothesis_count
                and evidence_type == "statistical_association"
                else "insufficient"
            ),
            "analysis_role": "cross_source_panel_association",
            "primary_outcome": requested_primary,
            "outcome_metrics": outcome_metrics,
            "candidate_metrics": candidate_metrics,
            "overall_summary": overall_summary,
            "coverage": {
                "left_metric_coverage": mapping_summary.get(
                    "left_metric_coverage",
                    {},
                ),
                "right_metric_coverage": mapping_summary.get(
                    "right_metric_coverage",
                    {},
                ),
                "left_metric_coverage_detail": mapping_summary.get(
                    "left_metric_coverage_detail",
                    {},
                ),
                "right_metric_coverage_detail": mapping_summary.get(
                    "right_metric_coverage_detail",
                    {},
                ),
                "aligned_panel_cells": mapping_summary.get(
                    "aligned_cell_count",
                    0,
                ),
                "complete_aligned_panel_cells": mapping_summary.get(
                    "complete_aligned_cell_count",
                    0,
                ),
            },
            "mapping": _panel_mapping_summary(crosswalk),
            "hypotheses": hypotheses,
            "hypothesis_ids_by_outcome": {
                outcome: tuple(hypothesis_ids)
                for outcome, hypothesis_ids in hypothesis_ids_by_outcome.items()
            },
            "associations_by_hypothesis": associations_by_hypothesis,
            "claim_ceiling": "sensitivity_only",
            "causal_claim_allowed": False,
            "contribution_claim_allowed": False,
            "specific_channel_claim_allowed": False,
            "correlation_coefficient_is_contribution": False,
        },
        result_refs=result_refs,
        sql_hashes=(),
        evidence_type=evidence_type,
        strength=strength,
        wording_limit=wording_limit,
        limitations=limitations,
        disabled_degraded_blocked_path_refs=(),
        verifier_handoff={
            "requires_evidence_ref": evidence_ref,
            "requires_bound_result_refs": result_refs,
            "claim_ceiling": "sensitivity_only",
            "causal_claim_allowed": False,
            "contribution_claim_allowed": False,
            "specific_channel_claim_allowed": False,
            "correlation_coefficient_is_contribution": False,
        },
        admin_audit_ref=f"capability:{request.run_id}:{request.capability_id}",
        **_bound_provenance(request),
    )


def _align_cross_source_rows(
    bound: BoundCapabilityInput,
) -> tuple[dict[str, Any], str]:
    outcome_rows = tuple(bound.rows_by_slot.get(_ASSOCIATION_OUTCOME_SLOT, ()))
    candidate_rows = tuple(bound.rows_by_slot.get(_ASSOCIATION_CANDIDATE_SLOT, ()))
    if not outcome_rows:
        return _empty_alignment(), "association_outcome_rows_missing"
    if not candidate_rows:
        return _empty_alignment(), "association_candidate_rows_missing"

    outcome_values, outcome_metrics, limitation = _collapse_association_slot(
        outcome_rows,
        slot_id=_ASSOCIATION_OUTCOME_SLOT,
    )
    if limitation:
        return _empty_alignment(), limitation
    candidate_values, candidate_metrics, limitation = _collapse_association_slot(
        candidate_rows,
        slot_id=_ASSOCIATION_CANDIDATE_SLOT,
    )
    if limitation:
        return _empty_alignment(), limitation
    if not outcome_metrics:
        return _empty_alignment(), "association_outcome_metrics_missing"
    if not candidate_metrics:
        return _empty_alignment(), "association_candidate_metrics_missing"
    collisions = tuple(
        metric for metric in outcome_metrics if metric in set(candidate_metrics)
    )
    if collisions:
        return (
            _empty_alignment(),
            "association_metric_name_collision:" + ",".join(collisions),
        )

    outcome_keys = set(outcome_values)
    candidate_keys = set(candidate_values)
    aligned_keys = tuple(sorted(outcome_keys.intersection(candidate_keys)))
    aligned_rows = tuple(
        {
            "observation_key": observation_key,
            **outcome_values[observation_key],
            **candidate_values[observation_key],
        }
        for observation_key in aligned_keys
    )
    alignment = {
        "rows": aligned_rows,
        "outcome_metrics": outcome_metrics,
        "candidate_metrics": candidate_metrics,
        "outcome_observation_count": len(outcome_keys),
        "candidate_observation_count": len(candidate_keys),
        "aligned_observation_count": len(aligned_keys),
        "outcome_only_observation_count": len(outcome_keys - candidate_keys),
        "candidate_only_observation_count": len(candidate_keys - outcome_keys),
        "duplicate_window_rows_deduplicated": (
            len(outcome_rows)
            + len(candidate_rows)
            - len(outcome_keys)
            - len(candidate_keys)
        ),
    }
    if not aligned_rows:
        return alignment, "association_observation_overlap_missing"
    return alignment, ""


def _collapse_association_slot(
    rows: tuple[Mapping[str, Any], ...],
    *,
    slot_id: str,
) -> tuple[dict[str, dict[str, Any]], tuple[str, ...], str]:
    metric_fields = _association_metric_fields(rows)
    observations: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            return {}, (), f"association_row_type_invalid:{slot_id}:{index}"
        raw_key = row.get("observation_key")
        observation_key = "" if raw_key is None else str(raw_key)
        if not observation_key:
            return {}, (), f"association_observation_key_missing:{slot_id}:{index}"
        values = observations.setdefault(observation_key, {})
        for field in metric_fields:
            if field not in row:
                continue
            value = row.get(field)
            if field in values and not _association_values_equal(values[field], value):
                return (
                    {},
                    (),
                    f"association_duplicate_value_conflict:{slot_id}:{field}",
                )
            values[field] = value
    return observations, metric_fields, ""


def _association_metric_fields(
    rows: tuple[Mapping[str, Any], ...],
) -> tuple[str, ...]:
    ordered_fields = tuple(
        dict.fromkeys(
            str(field)
            for row in rows
            if isinstance(row, Mapping)
            for field in row
            if _association_metric_field_allowed(str(field))
        )
    )
    metrics = []
    for field in ordered_fields:
        values = tuple(
            row.get(field)
            for row in rows
            if isinstance(row, Mapping) and field in row
        )
        non_null = tuple(value for value in values if value is not None)
        if not non_null or all(_association_number(value) is not None for value in non_null):
            metrics.append(field)
    return tuple(metrics)


def _association_metric_field_allowed(field: str) -> bool:
    return bool(
        field
        and field not in _ASSOCIATION_STRUCTURAL_FIELDS
        and not field.startswith("__")
        and not field.endswith("_id")
        and not field.endswith("_ref")
        and not field.endswith("_role")
    )


def _association_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _association_values_equal(left: Any, right: Any) -> bool:
    left_number = _association_number(left)
    right_number = _association_number(right)
    if left_number is not None and right_number is not None:
        return left_number == right_number
    return left is None and right is None


def _dedupe_panel_slot_rows(
    rows: tuple[Mapping[str, Any], ...],
    *,
    slot_id: str,
) -> tuple[tuple[dict[str, Any], ...], tuple[str, ...], str]:
    metric_fields = _association_metric_fields(rows)
    cells: dict[tuple[str, str], dict[str, Any]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            return (), (), f"panel_association_row_type_invalid:{slot_id}:{index}"
        raw_observation = row.get("observation_key")
        observation_key = (
            "" if raw_observation is None else str(raw_observation)
        )
        if not observation_key:
            return (
                (),
                (),
                f"panel_association_observation_key_missing:{slot_id}:{index}",
            )
        if "channel" not in row:
            return (), (), f"panel_association_channel_missing:{slot_id}:{index}"
        raw_channel = row.get("channel")
        channel_token = "" if raw_channel is None else str(raw_channel)
        cell = cells.setdefault(
            (observation_key, channel_token),
            {
                "observation_key": observation_key,
                "channel": raw_channel,
            },
        )
        for field in metric_fields:
            if field not in row:
                continue
            value = row.get(field)
            if field in cell and not _association_values_equal(cell[field], value):
                return (
                    (),
                    (),
                    f"panel_association_duplicate_value_conflict:{slot_id}:{field}",
                )
            cell[field] = value
    return tuple(cells.values()), metric_fields, ""


def _panel_metric_strategy(metric: str) -> str:
    normalized = str(metric).casefold()
    if (
        normalized in _PANEL_RATIO_METRICS
        or normalized.startswith("avg_")
        or normalized.endswith("_rate")
        or normalized.endswith("_ratio")
        or normalized.endswith("_frequency")
    ):
        return "ratio"
    return "sum"


def _normalize_panel_hypotheses(
    value: Any,
) -> tuple[tuple[dict[str, Any], ...], str]:
    if value is None:
        return (), "panel_hypotheses_missing"
    if isinstance(value, (str, bytes, Mapping)):
        return (), "panel_hypotheses_invalid"
    try:
        hypotheses = tuple(value)
    except TypeError:
        return (), "panel_hypotheses_invalid"
    if not hypotheses:
        return (), "panel_hypotheses_missing"

    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    required = {"outcome_metric", "candidate_metric", "transform", "lag"}
    allowed = {*required, "hypothesis_id"}
    for index, hypothesis in enumerate(hypotheses):
        if not isinstance(hypothesis, Mapping):
            return (), f"panel_hypothesis_invalid:{index}"
        if set(hypothesis) - allowed or not required.issubset(hypothesis):
            return (), f"panel_hypothesis_invalid:{index}"
        outcome_metric = str(hypothesis.get("outcome_metric") or "").strip()
        candidate_metric = str(hypothesis.get("candidate_metric") or "").strip()
        if not outcome_metric or not candidate_metric:
            return (), f"panel_hypothesis_invalid:{index}"
        transform_token = str(hypothesis.get("transform") or "").strip().casefold()
        transform = _PANEL_TRANSFORM_ALIASES.get(transform_token)
        if transform is None:
            return (), f"panel_hypothesis_transform_invalid:{index}"
        lag = hypothesis.get("lag")
        if isinstance(lag, bool) or not isinstance(lag, int):
            return (), f"panel_hypothesis_lag_invalid:{index}"
        hypothesis_id = str(hypothesis.get("hypothesis_id") or "").strip()
        if not hypothesis_id:
            hypothesis_id = (
                f"{outcome_metric}:{candidate_metric}:{transform}:lag{lag}"
            )
        if hypothesis_id in seen_ids:
            return (), f"panel_hypothesis_id_duplicate:{hypothesis_id}"
        seen_ids.add(hypothesis_id)
        normalized.append(
            {
                "hypothesis_id": hypothesis_id,
                "outcome_metric": outcome_metric,
                "candidate_metric": candidate_metric,
                "transform": transform,
                "lag": lag,
            }
        )
    return tuple(normalized), ""


def _panel_hypothesis_insufficient_bundle(
    hypothesis: Mapping[str, Any],
    limitation: str,
) -> dict[str, Any]:
    return {
        "hypothesis": dict(hypothesis),
        "evidence_type": "insufficient",
        "strength": "low",
        "wording_limit": "sensitivity_only",
        "numeric_facts": {},
        "association": {},
        "limitations": (limitation,),
    }


def _panel_mapping_coverage_context(
    mapping_summary: Mapping[str, Any],
    *,
    outcome_metric: str,
    candidate_metric: str,
) -> dict[str, Any]:
    left_details = mapping_summary.get("left_metric_coverage_detail") or {}
    right_details = mapping_summary.get("right_metric_coverage_detail") or {}
    left = dict(left_details.get(outcome_metric) or {})
    right = dict(right_details.get(candidate_metric) or {})
    left_coverage = _optional_coverage_ratio(left.get("coverage"))
    right_coverage = _optional_coverage_ratio(right.get("coverage"))
    if left_coverage is None or right_coverage is None:
        coverage = None
        limiting_side = "unknown"
    else:
        coverage = min(left_coverage, right_coverage)
        if left_coverage < right_coverage:
            limiting_side = "outcome"
        elif right_coverage < left_coverage:
            limiting_side = "candidate"
        else:
            limiting_side = "equal"
    return {
        "coverage": coverage,
        "coverage_basis": {
            "combination": "minimum_of_source_metric_coverage",
            "outcome": {
                "coverage": left_coverage,
                "basis": str(left.get("basis") or "unknown"),
            },
            "candidate": {
                "coverage": right_coverage,
                "basis": str(right.get("basis") or "unknown"),
            },
            "limiting_side": limiting_side,
        },
    }


def _optional_coverage_ratio(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(number) or not 0.0 <= number <= 1.0:
        return None
    return number


def _panel_mapping_summary(
    crosswalk: Mapping[str, Any],
) -> dict[str, Any]:
    summary = crosswalk.get("mapping_summary") or {}
    return {
        "authority_status": "candidate_mechanical_crosswalk",
        "authority_established": False,
        "mechanical_status": "candidate_mechanical_crosswalk",
        "candidate_rules": tuple(crosswalk.get("candidate_rules") or ()),
        "pair_count": int(summary.get("pair_count") or 0),
        "left_distinct_group_count": int(
            summary.get("left_distinct_group_count") or 0
        ),
        "right_distinct_group_count": int(
            summary.get("right_distinct_group_count") or 0
        ),
        "unmatched_count": int(summary.get("unmatched_count") or 0),
        "ambiguous_count": int(summary.get("ambiguous_count") or 0),
        "aligned_cell_count": int(summary.get("aligned_cell_count") or 0),
        "complete_aligned_cell_count": int(
            summary.get("complete_aligned_cell_count") or 0
        ),
        "specific_mapping_pairs_included": False,
    }


def _empty_alignment() -> dict[str, Any]:
    return {
        "rows": (),
        "outcome_metrics": (),
        "candidate_metrics": (),
        "outcome_observation_count": 0,
        "candidate_observation_count": 0,
        "aligned_observation_count": 0,
        "outcome_only_observation_count": 0,
        "candidate_only_observation_count": 0,
        "duplicate_window_rows_deduplicated": 0,
    }


def _alignment_summary(alignment: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in alignment.items()
        if key != "rows"
    }


def _association_insufficient_envelope(
    request: CapabilityRequest,
    limitation: str,
    *,
    typed_payload: Mapping[str, Any] | None = None,
) -> CapabilityEvidenceEnvelope:
    result_refs = _result_refs(request)
    evidence_ref = f"{request.capability_id}:{request.run_id}:insufficient"
    payload = {
        "status": "insufficient",
        "analysis_role": "cross_source_association",
        "limitation": limitation,
        "claim_ceiling": "candidate_driver",
        "causal_claim_allowed": False,
        "correlation_coefficient_is_contribution": False,
        **dict(typed_payload or {}),
    }
    return CapabilityEvidenceEnvelope(
        evidence_ref=evidence_ref,
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
        typed_payload=payload,
        result_refs=result_refs,
        sql_hashes=(),
        evidence_type="insufficient",
        strength="low",
        wording_limit="insufficient",
        limitations=(limitation,),
        disabled_degraded_blocked_path_refs=(limitation,),
        verifier_handoff={
            "requires_evidence_ref": evidence_ref,
            "requires_bound_result_refs": result_refs,
            "claim_ceiling": "candidate_driver",
            "causal_claim_allowed": False,
            "correlation_coefficient_is_contribution": False,
        },
        admin_audit_ref=f"capability:{request.run_id}:{request.capability_id}",
        **_bound_provenance(request),
    )


def _panel_association_insufficient_envelope(
    request: CapabilityRequest,
    limitation: str,
    *,
    typed_payload: Mapping[str, Any] | None = None,
) -> CapabilityEvidenceEnvelope:
    result_refs = _result_refs(request)
    evidence_ref = f"{request.capability_id}:{request.run_id}:insufficient"
    payload = {
        "status": "insufficient",
        "analysis_role": "cross_source_panel_association",
        "limitation": limitation,
        "claim_ceiling": "sensitivity_only",
        "causal_claim_allowed": False,
        "contribution_claim_allowed": False,
        "specific_channel_claim_allowed": False,
        "correlation_coefficient_is_contribution": False,
        **dict(typed_payload or {}),
    }
    return CapabilityEvidenceEnvelope(
        evidence_ref=evidence_ref,
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
        typed_payload=payload,
        result_refs=result_refs,
        sql_hashes=(),
        evidence_type="insufficient",
        strength="low",
        wording_limit="sensitivity_only",
        limitations=(limitation,),
        disabled_degraded_blocked_path_refs=(limitation,),
        verifier_handoff={
            "requires_evidence_ref": evidence_ref,
            "requires_bound_result_refs": result_refs,
            "claim_ceiling": "sensitivity_only",
            "causal_claim_allowed": False,
            "contribution_claim_allowed": False,
            "specific_channel_claim_allowed": False,
            "correlation_coefficient_is_contribution": False,
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
    rows = tuple(
        row
        for slot_rows in bound.rows_by_slot.values()
        for row in slot_rows
    )
    default_row_budget = (
        _PANEL_DEFAULT_ROW_BUDGET
        if request.capability_id == "cross_source_panel_association"
        else 5000
    )
    row_budget = _positive_int(
        params.get("row_budget", default_row_budget),
        default_row_budget,
    )
    if len(rows) > row_budget:
        return "row_budget_exceeded"
    result_refs = (*bound.result_refs, *bound.validation_result_refs)
    result_ref_budget = _positive_int(params.get("result_ref_budget", 100), 100)
    if len(result_refs) > result_ref_budget:
        return "result_ref_budget_exceeded"
    return ""


def _validated_bound_rows(request: CapabilityRequest) -> tuple[Mapping[str, Any], ...]:
    bound = _bound_input(request)
    if bound is None or validate_bound_capability_input(
        bound,
        request.evidence_resolver,
    ):
        return ()
    return tuple(
        row
        for slot_rows in bound.rows_by_slot.values()
        for row in slot_rows
    )


def _blocked_envelope(request: CapabilityRequest, limitation: str) -> CapabilityEvidenceEnvelope:
    result_refs = _result_refs(request)
    rows = _validated_bound_rows(request)
    row_count = len(rows) if rows else None
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
        sql_hashes=(),
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
) -> tuple[Mapping[str, Any], ...]:
    bound = _bound_input(request)
    if bound is None:
        return ()
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


def _result_refs(request: CapabilityRequest) -> tuple[str, ...]:
    bound = _bound_input(request)
    if bound is not None:
        return _dedupe((*bound.result_refs, *bound.validation_result_refs))
    return ()


def _bound_provenance(request: CapabilityRequest) -> dict[str, Any]:
    bound = _bound_input(request)
    if bound is None or validate_bound_capability_input(
        bound,
        request.evidence_resolver,
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
            "input_status": "blocked",
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
        return (
            "insufficient",
            "low",
            "blocked",
            _dedupe((*limitations, "capability_binding_record_missing")),
        )
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


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default
