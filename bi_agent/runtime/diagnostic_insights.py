"""Evidence-bounded diagnostic insight construction.

This module turns already verified metric movement and accounting contribution
evidence into a machine-readable diagnostic portfolio.  It deliberately does
not write business prose or invent mechanisms.  Routes that have not run stay
candidate evidence, while arithmetic derived from verified inputs is labelled
as derived evidence.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


_VERIFIED_MECHANISM_STATES = frozenset(
    {
        "verified",
        "causal_supported",
        "mechanism_verified",
        "resolved",
        "not_applicable",
    }
)
_CANDIDATE_MECHANISM_STATES = frozenset(
    {
        "candidate",
        "candidate_mechanism",
        "plausible",
        "plausible_mechanism",
    }
)
_UNUSABLE_EVIDENCE_STATES = frozenset(
    {"blocked", "failed", "invalid", "missing", "unavailable"}
)
_UNUSABLE_EVIDENCE_TYPES = frozenset(
    {"insufficient", "insufficient_evidence", "blocked"}
)
_METRIC_BUSINESS_LABELS = {
    "paid_amount": "付费金额",
    "paid_users": "付费人数",
    "paid_orders": "付费订单数",
    "paid_frequency": "付费频次",
    "avg_order_amount": "单笔付费金额",
    "player_bet_amount": "玩家投注金额",
    "player_bet_count": "玩家投注次数",
    "player_avg_bet_amount": "玩家平均单次投注金额",
    "gameplay_users": "玩法参与人次",
    "gameplay_rounds": "玩法局数",
    "gameplay_profit": "玩法利润",
    "service_fee_rake": "服务费与抽水",
}


def build_diagnostic_insight_portfolio(
    *,
    question: Mapping[str, Any],
    evidence: Sequence[Mapping[str, Any]],
    factor_states: Sequence[Mapping[str, Any]] = (),
    available_routes: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build a diagnostic portfolio from evidence already accepted by runtime.

    The return shape is intentionally JSON-compatible so the workflow can pass
    it to later planning, verification, persistence, and writing nodes without
    translating Python-specific objects.
    """

    usable_evidence = tuple(item for item in evidence if _evidence_is_usable(item))
    movement = _metric_movement(question, usable_evidence)
    decomposition = _formula_decomposition(usable_evidence)
    factors = _normalized_factor_states(factor_states)
    if not factors:
        factors = diagnostic_factor_states_from_evidence(usable_evidence)

    insights: list[dict[str, Any]] = []
    if movement is not None:
        insights.append(_movement_insight(movement))

    contributions = decomposition.get("contributions", []) if decomposition else []
    contributions = _contributions_with_business_labels(contributions, factors)
    if decomposition is not None:
        decomposition = {**decomposition, "contributions": contributions}
    dominant = None
    if movement is not None and decomposition is not None and contributions:
        driver_insights, dominant = _driver_insights(
            contributions=contributions,
            observed_change=movement["change"],
            source_evidence_refs=decomposition["source_evidence_refs"],
            source_result_refs=decomposition.get("source_result_refs", ()),
        )
        insights.extend(driver_insights)

    mechanism = None
    if dominant is not None:
        mechanism = _mechanism_depth_insight(dominant, factors)
        insights.append(mechanism)

    counterfactuals = _counterfactuals(
        movement=movement,
        decomposition=decomposition,
    )
    growth_quality_signals = _growth_quality_signals(
        movement=movement,
        contributions=contributions,
        dominant=dominant,
        factor_states=factors,
    )
    dimension_findings = _dimension_findings(usable_evidence)
    cross_source_findings = _cross_source_findings(usable_evidence)

    eligible_routes = _eligible_next_routes(
        available_routes,
        dominant_factor=dominant,
        factor_states=factors,
    )
    sufficiency = _diagnostic_sufficiency(
        movement=movement,
        decomposition=decomposition,
        dominant=dominant,
        mechanism=mechanism,
        eligible_routes=eligible_routes,
    )

    return {
        "insights": insights,
        "counterfactuals": counterfactuals,
        "growth_quality_signals": growth_quality_signals,
        "dimension_findings": dimension_findings,
        "cross_source_findings": cross_source_findings,
        "next_best_candidate": (
            eligible_routes[0] if sufficiency["status"] == "continue" else None
        ),
        "diagnostic_sufficiency": sufficiency,
    }


def _contributions_with_business_labels(
    contributions: Sequence[Mapping[str, Any]],
    factor_states: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    labeled: list[dict[str, Any]] = []
    for contribution in contributions:
        item = dict(contribution)
        factor = _matching_factor_state(item, factor_states)
        if factor and str(factor.get("factor") or "").strip():
            item["factor"] = str(factor["factor"])
        labeled.append(item)
    return labeled


def _evidence_is_usable(item: Mapping[str, Any]) -> bool:
    if item.get("claim_input_ready") is False:
        return False
    if str(item.get("status") or "").lower() in _UNUSABLE_EVIDENCE_STATES:
        return False
    if str(item.get("evidence_state") or "").lower() in _UNUSABLE_EVIDENCE_STATES:
        return False
    if str(item.get("evidence_type") or "").lower() in _UNUSABLE_EVIDENCE_TYPES:
        return False
    return isinstance(item.get("typed_payload"), Mapping)


def _metric_movement(
    question: Mapping[str, Any],
    evidence: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    for item in evidence:
        if not _evidence_is_verified(item) or not _is_metric_comparison(item):
            continue
        payload = item.get("typed_payload") or {}
        target_value = _number(payload.get("target_value"))
        baseline_value = _number(payload.get("baseline_value"))
        if target_value is None or baseline_value is None:
            continue
        change = target_value - baseline_value
        return {
            "metric_id": str(
                payload.get("metric")
                or payload.get("metric_id")
                or question.get("metric_id")
                or question.get("target_metric")
                or ""
            ),
            "target_window_id": str(
                payload.get("target_window_id")
                or question.get("target_window_id")
                or ""
            ),
            "baseline_window_id": str(
                payload.get("baseline_window_id")
                or question.get("baseline_window_id")
                or ""
            ),
            "target_value": target_value,
            "baseline_value": baseline_value,
            "change": change,
            "change_rate": _ratio(change, baseline_value),
            "direction": _direction(change),
            **_source_provenance(item),
        }
    return None


def _is_metric_comparison(item: Mapping[str, Any]) -> bool:
    role = str(item.get("evidence_role") or "").lower()
    capability = str(item.get("capability_id") or item.get("capability") or "").lower()
    claim_type = str(item.get("claim_type") or "").lower()
    return (
        role in {"metric_movement", "metric_comparison", "period_comparison"}
        or capability in {"compare_periods", "metric_comparison"}
        or claim_type in {"metric_movement", "period_comparison"}
    )


def _movement_insight(movement: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "insight_type": "metric_movement",
        "evidence_state": "verified",
        "metric_id": movement["metric_id"],
        "target_window_id": movement["target_window_id"],
        "baseline_window_id": movement["baseline_window_id"],
        "target_value": movement["target_value"],
        "baseline_value": movement["baseline_value"],
        "change": movement["change"],
        "change_rate": movement["change_rate"],
        "direction": movement["direction"],
        "source_evidence_refs": list(movement["source_evidence_refs"]),
        **(
            {"source_result_refs": list(movement["source_result_refs"])}
            if movement.get("source_result_refs")
            else {}
        ),
    }


def _formula_decomposition(
    evidence: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    for item in evidence:
        if not _evidence_is_verified(item):
            continue
        payload = item.get("typed_payload") or {}
        decompositions = payload.get("decompositions")
        if not isinstance(decompositions, Sequence) or isinstance(
            decompositions, (str, bytes, bytearray)
        ):
            continue
        for raw in decompositions:
            if not isinstance(raw, Mapping):
                continue
            raw_contributions = (
                raw.get("core_factor_contributions")
                or raw.get("factor_contributions")
                or raw.get("contributions")
                or ()
            )
            contributions = _normalized_contributions(raw_contributions)
            if not contributions:
                continue
            status = str(
                raw.get("core_reconciliation_status")
                or raw.get("reconciliation_status")
                or ""
            ).lower()
            metric_delta = _first_number(
                raw,
                "metric_delta",
                "amount_delta",
                "total_change",
                "delta",
            )
            if metric_delta is None:
                target_value = _first_number(raw, "target_value", "target_amount")
                baseline_value = _first_number(
                    raw, "baseline_value", "baseline_amount"
                )
                if target_value is not None and baseline_value is not None:
                    metric_delta = target_value - baseline_value
            return {
                "reconciliation_status": status,
                "metric_delta": metric_delta,
                "contributions": contributions,
                **_source_provenance(item),
            }
    return None


def _normalized_contributions(raw_values: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_values, Sequence) or isinstance(
        raw_values, (str, bytes, bytearray)
    ):
        return []
    result: list[dict[str, Any]] = []
    for item in raw_values:
        if not isinstance(item, Mapping):
            continue
        if item.get("observed") is False or str(item.get("status") or "") in {
            "assumed_neutral",
            "unavailable",
        }:
            continue
        factor_id = _factor_id(item)
        contribution = _number(item.get("contribution"))
        if not factor_id or contribution is None:
            continue
        result.append(
            {
                "factor_id": factor_id,
                "factor": str(
                    item.get("factor")
                    or item.get("business_name")
                    or item.get("label")
                    or factor_id
                ),
                "baseline_value": _first_number(
                    item, "baseline_value", "baseline"
                ),
                "target_value": _first_number(item, "target_value", "target"),
                "change": _first_number(item, "delta", "change"),
                "change_rate": _first_number(
                    item, "delta_ratio", "change_rate", "changeRate"
                ),
                "contribution": contribution,
                "contribution_share": _first_number(
                    item, "contribution_share", "contributionShare"
                ),
            }
        )
    return sorted(result, key=lambda value: abs(value["contribution"]), reverse=True)


def _driver_insights(
    *,
    contributions: Sequence[Mapping[str, Any]],
    observed_change: float,
    source_evidence_refs: Sequence[str],
    source_result_refs: Sequence[str],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    movement_sign = _sign(observed_change)
    supporting = [
        item
        for item in contributions
        if movement_sign and _sign(item["contribution"]) == movement_sign
    ]
    dominant = max(
        supporting or list(contributions),
        key=lambda value: abs(value["contribution"]),
        default=None,
    )
    result: list[dict[str, Any]] = []
    for item in contributions:
        contribution_sign = _sign(item["contribution"])
        if item is dominant:
            insight_type = "dominant_driver"
        elif movement_sign and contribution_sign == -movement_sign:
            insight_type = "offsetting_driver"
        elif movement_sign and contribution_sign == movement_sign:
            insight_type = "supporting_driver"
        else:
            insight_type = "neutral_driver"
        share = item.get("contribution_share")
        derived_fields: list[str] = []
        if share is None and observed_change:
            share = item["contribution"] / observed_change
            derived_fields.append("contribution_share")
        result.append(
            {
                **item,
                "insight_type": insight_type,
                "evidence_state": "verified",
                "contribution_share": share,
                "derived_fields": derived_fields,
                "source_evidence_refs": list(source_evidence_refs),
                **(
                    {"source_result_refs": list(source_result_refs)}
                    if source_result_refs
                    else {}
                ),
            }
        )
    enriched_dominant = next(
        (
            dict(item)
            for item in result
            if item.get("insight_type") == "dominant_driver"
        ),
        None,
    )
    return result, enriched_dominant


def _mechanism_depth_insight(
    dominant: Mapping[str, Any],
    factor_states: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    factor = _matching_factor_state(dominant, factor_states)
    raw_status = str((factor or {}).get("mechanism_status") or "unresolved").lower()
    if raw_status in _VERIFIED_MECHANISM_STATES:
        status = "verified"
        evidence_state = "verified"
    elif raw_status in _CANDIDATE_MECHANISM_STATES:
        status = "candidate"
        evidence_state = "candidate"
    else:
        status = "unresolved"
        evidence_state = "unresolved"
    refs = _string_list((factor or {}).get("mechanism_evidence_refs"))
    result = {
        "insight_type": "mechanism_depth",
        "factor_id": dominant["factor_id"],
        "factor": dominant["factor"],
        "status": status,
        "evidence_state": evidence_state,
        "source_evidence_refs": refs,
    }
    result_refs = _string_list((factor or {}).get("source_result_refs"))
    if result_refs:
        result["source_result_refs"] = result_refs
    return result


def _counterfactuals(
    *,
    movement: Mapping[str, Any] | None,
    decomposition: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    if movement is None or not _decomposition_matches_movement(
        decomposition, movement
    ):
        return []
    source_refs = _dedupe_strings(
        [
            *movement["source_evidence_refs"],
            *decomposition["source_evidence_refs"],
        ]
    )
    source_result_refs = _dedupe_strings(
        [
            *_string_list(movement.get("source_result_refs")),
            *_string_list(decomposition.get("source_result_refs")),
        ]
    )
    result = []
    for item in decomposition["contributions"]:
        change_without = movement["change"] - item["contribution"]
        result.append(
            {
                "counterfactual_type": "accounting_component_removal",
                "evidence_state": "derived",
                "removed_factor_id": item["factor_id"],
                "removed_factor": item["factor"],
                "observed_change": movement["change"],
                "removed_contribution": item["contribution"],
                "change_without_factor": change_without,
                "direction_without_factor": _direction(change_without),
                "derivation": "observed_change_minus_contribution",
                "source_evidence_refs": source_refs,
                **(
                    {"source_result_refs": source_result_refs}
                    if source_result_refs
                    else {}
                ),
            }
        )
    return result


def _growth_quality_signals(
    *,
    movement: Mapping[str, Any] | None,
    contributions: Sequence[Mapping[str, Any]],
    dominant: Mapping[str, Any] | None,
    factor_states: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if movement is None:
        return []
    result: list[dict[str, Any]] = []
    absolute_total = sum(abs(item["contribution"]) for item in contributions)
    if dominant is not None and absolute_total:
        result.append(
            {
                "signal_type": "driver_concentration",
                "evidence_state": "derived",
                "dominant_factor_id": dominant["factor_id"],
                "dominant_absolute_share": (
                    abs(dominant["contribution"]) / absolute_total
                ),
                "derivation": "absolute_dominant_contribution_over_absolute_total",
                "source_evidence_refs": _dedupe_strings(
                    [
                        *_string_list(movement.get("source_evidence_refs")),
                        *(
                            _string_list(dominant.get("source_evidence_refs"))
                            if isinstance(dominant, Mapping)
                            else []
                        ),
                    ]
                ),
                **(
                    {
                        "source_result_refs": _dedupe_strings(
                            [
                                *_string_list(movement.get("source_result_refs")),
                                *(
                                    _string_list(dominant.get("source_result_refs"))
                                    if isinstance(dominant, Mapping)
                                    else []
                                ),
                            ]
                        )
                    }
                    if _dedupe_strings(
                        [
                            *_string_list(movement.get("source_result_refs")),
                            *(
                                _string_list(dominant.get("source_result_refs"))
                                if isinstance(dominant, Mapping)
                                else []
                            ),
                        ]
                    )
                    else {}
                ),
            }
        )
        movement_sign = _sign(movement["change"])
        offset_total = sum(
            abs(item["contribution"])
            for item in contributions
            if movement_sign
            and _sign(item["contribution"]) == -movement_sign
        )
        result.append(
            {
                "signal_type": "offsetting_pressure",
                "evidence_state": "derived",
                "absolute_offset_share": offset_total / absolute_total,
                "derivation": "absolute_offsetting_contribution_over_absolute_total",
                "source_evidence_refs": list(
                    _dedupe_strings(
                        [
                            *_string_list(movement.get("source_evidence_refs")),
                            *_string_list(dominant.get("source_evidence_refs")),
                        ]
                    )
                ),
                **(
                    {
                        "source_result_refs": _dedupe_strings(
                            [
                                *_string_list(movement.get("source_result_refs")),
                                *_string_list(dominant.get("source_result_refs")),
                            ]
                        )
                    }
                    if (
                        movement.get("source_result_refs")
                        or dominant.get("source_result_refs")
                    )
                    else {}
                ),
            }
        )

    for factor in factor_states:
        change = factor.get("change")
        if change is None or factor.get("observed") is False:
            continue
        role = str(factor.get("diagnostic_role") or "observed_factor").lower()
        contribution = factor.get("contribution")
        result.append(
            {
                "signal_type": f"{role}_movement",
                "evidence_state": factor["evidence_state"],
                "factor_id": factor["factor_id"],
                "factor": factor["factor"],
                "baseline_value": factor.get("baseline_value"),
                "target_value": factor.get("target_value"),
                "change": change,
                "change_rate": factor.get("change_rate"),
                "alignment_with_metric": _alignment(
                    movement["change"], change
                ),
                "contribution_status": (
                    "quantified" if contribution is not None else "unquantified"
                ),
                "source_evidence_refs": list(factor["source_evidence_refs"]),
                **(
                    {"source_result_refs": list(factor["source_result_refs"])}
                    if factor.get("source_result_refs")
                    else {}
                ),
            }
        )
    return result


def _normalized_factor_states(
    factor_states: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    result = []
    for item in factor_states:
        if not isinstance(item, Mapping):
            continue
        factor_id = _factor_id(item)
        if not factor_id:
            continue
        result.append(
            {
                "factor_id": factor_id,
                "factor": str(
                    item.get("factor")
                    or item.get("business_name")
                    or item.get("label")
                    or factor_id
                ),
                "state": str(item.get("state") or ""),
                "evidence_state": _normalized_evidence_state(
                    item.get("evidence_state"), default="verified"
                ),
                "observed": item.get("observed", True),
                "baseline_value": _first_number(
                    item, "baseline", "baseline_value"
                ),
                "target_value": _first_number(item, "target", "target_value"),
                "change": _first_number(item, "change", "delta"),
                "change_rate": _first_number(
                    item, "changeRate", "change_rate", "delta_ratio"
                ),
                "contribution": _number(item.get("contribution")),
                "diagnostic_role": str(
                    item.get("diagnostic_role")
                    or item.get("business_role")
                    or "observed_factor"
                ),
                "mechanism_status": str(
                    item.get("mechanism_status")
                    or item.get("mechanism_state")
                    or "unresolved"
                ),
                "mechanism_evidence_refs": _string_list(
                    item.get("mechanism_evidence_refs")
                ),
                "source_evidence_refs": _dedupe_strings(
                    [
                        *_string_list(item.get("source_evidence_refs")),
                        *_string_list(item.get("source_evidence_ref")),
                    ]
                ),
                **(
                    {
                        "source_result_refs": _dedupe_strings(
                            [
                                *_string_list(item.get("source_result_refs")),
                                *_string_list(item.get("source_result_ref")),
                                *_string_list(item.get("result_refs")),
                            ]
                        )
                    }
                    if _dedupe_strings(
                        [
                            *_string_list(item.get("source_result_refs")),
                            *_string_list(item.get("source_result_ref")),
                            *_string_list(item.get("result_refs")),
                        ]
                    )
                    else {}
                ),
            }
        )
    return result


def diagnostic_factor_states_from_evidence(
    evidence: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Rebuild factor observations from accepted decomposition evidence."""

    raw_states: list[dict[str, Any]] = []
    for item in evidence:
        if not isinstance(item, Mapping) or not _evidence_is_usable(item):
            continue
        payload = item.get("typed_payload") or {}
        if not isinstance(payload, Mapping):
            continue
        for decomposition in payload.get("decompositions") or ():
            if not isinstance(decomposition, Mapping):
                continue
            contributions = {
                str(value.get("component_id") or ""): value
                for value in decomposition.get("core_factor_contributions") or ()
                if isinstance(value, Mapping)
            }
            for change in decomposition.get("component_changes") or ():
                if not isinstance(change, Mapping):
                    continue
                factor_id = _factor_id(change)
                if not factor_id:
                    continue
                contribution = contributions.get(factor_id)
                raw_states.append(
                    {
                        "factor_id": factor_id,
                        "factor": str(
                            change.get("business_name")
                            or change.get("label")
                            or factor_id
                        ),
                        "observed": change.get("observed") is not False,
                        "baseline_value": change.get("baseline_value"),
                        "target_value": change.get("target_value"),
                        "change": change.get("delta"),
                        "change_rate": change.get("delta_ratio"),
                        "contribution": (
                            contribution.get("contribution")
                            if isinstance(contribution, Mapping)
                            else None
                        ),
                        "diagnostic_role": _factor_diagnostic_role(factor_id),
                        "mechanism_status": "unresolved",
                        "source_evidence_refs": [
                            str(item.get("evidence_ref") or "")
                        ],
                        **(
                            {"source_result_refs": _source_result_refs(item)}
                            if _source_result_refs(item)
                            else {}
                        ),
                    }
                )
    return _normalized_factor_states(raw_states)


def _factor_diagnostic_role(factor_id: str) -> str:
    normalized = factor_id.lower()
    if "user" in normalized or "customer" in normalized or "payer" in normalized:
        return "breadth"
    if "frequency" in normalized or normalized in {"orders", "transactions"}:
        return "frequency"
    if "amount" in normalized or "value" in normalized or "price" in normalized:
        return "structure"
    if "success" in normalized or "conversion" in normalized or "rate" in normalized:
        return "conversion"
    return "observed_factor"


def _dimension_findings(
    evidence: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for item in evidence:
        payload = item.get("typed_payload") or {}
        raw_findings = payload.get("dimension_findings")
        if not isinstance(raw_findings, Sequence) or isinstance(
            raw_findings, (str, bytes, bytearray)
        ):
            continue
        evidence_refs = _source_evidence_refs(item)
        result_refs = _source_result_refs(item)
        source_state = _explicit_evidence_state(item) or "verified"
        for raw in raw_findings:
            if not isinstance(raw, Mapping):
                continue
            evidence_state = str(
                raw.get("evidence_state") or source_state
            ).lower()
            if evidence_state not in {
                "verified",
                "derived",
                "candidate",
                "unresolved",
            }:
                evidence_state = "unresolved"
            finding = dict(raw)
            finding["evidence_state"] = evidence_state
            finding["source_evidence_refs"] = _dedupe_strings(
                [
                    *evidence_refs,
                    *_string_list(raw.get("source_evidence_refs")),
                ]
            )
            raw_result_refs = _dedupe_strings(
                [
                    *result_refs,
                    *_string_list(raw.get("source_result_refs")),
                    *_string_list(raw.get("source_result_ref")),
                    *_string_list(raw.get("result_refs")),
                ]
            )
            if raw_result_refs:
                finding["source_result_refs"] = raw_result_refs
            findings.append(finding)
    return findings


def _cross_source_findings(
    evidence: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for item in evidence:
        capability_id = str(
            item.get("capability_id") or item.get("capability") or ""
        )
        payload = item.get("typed_payload") or {}
        if not isinstance(payload, Mapping):
            continue
        if capability_id == "cross_source_association":
            findings.extend(_temporal_association_findings(item, payload))
        elif capability_id == "cross_source_panel_association":
            findings.extend(_panel_association_findings(item, payload))
    return findings


def _temporal_association_findings(
    item: Mapping[str, Any], payload: Mapping[str, Any]
) -> list[dict[str, Any]]:
    by_outcome = payload.get("associations_by_outcome") or {}
    if not isinstance(by_outcome, Mapping):
        return []
    findings: list[dict[str, Any]] = []
    for outcome_id, raw_bundle in by_outcome.items():
        if not isinstance(raw_bundle, Mapping):
            continue
        if raw_bundle.get("evidence_type") != "statistical_association":
            continue
        association = raw_bundle.get("association") or {}
        if not isinstance(association, Mapping):
            continue
        supported = association.get("supported_associations") or ()
        if not isinstance(supported, Sequence) or isinstance(
            supported, (str, bytes, bytearray)
        ):
            continue
        best_by_candidate: dict[str, Mapping[str, Any]] = {}
        for raw in supported:
            if not isinstance(raw, Mapping):
                continue
            candidate_id = str(raw.get("candidate_key") or "")
            if candidate_id and candidate_id not in best_by_candidate:
                best_by_candidate[candidate_id] = raw
        for candidate_id, best in best_by_candidate.items():
            rolling = best.get("rolling") or {}
            outcome_metric = _metric_business_label(outcome_id)
            candidate_metric = _metric_business_label(candidate_id)
            coefficient = _number(best.get("coefficient"))
            q_value = _number(best.get("q_value"))
            lag = best.get("lag")
            rolling_stable = bool(
                isinstance(rolling, Mapping) and rolling.get("stable")
            )
            findings.append(
                {
                    "finding_type": "cross_source_temporal_association",
                    "evidence_state": "derived",
                    "analysis_role": "auxiliary",
                    "outcome_metric_id": str(outcome_id),
                    "outcome_metric": outcome_metric,
                    "candidate_metric_id": candidate_id,
                    "candidate_metric": candidate_metric,
                    "transform": str(best.get("transform") or ""),
                    "lag": lag,
                    "lag_semantics": association.get("lag_semantics"),
                    "method": str(best.get("method") or ""),
                    "coefficient": coefficient,
                    "q_value": q_value,
                    "sample_size": best.get("sample_size"),
                    "rolling_stable": rolling_stable,
                    "rolling_same_direction_ratio": (
                        _number(rolling.get("same_direction_ratio"))
                        if isinstance(rolling, Mapping)
                        else None
                    ),
                    "wording_limit": str(
                        raw_bundle.get("wording_limit") or "candidate_association"
                    ),
                    "causal_claim_allowed": False,
                    "contribution_claim_allowed": False,
                    "statement": _temporal_association_statement(
                        outcome_metric=outcome_metric,
                        candidate_metric=candidate_metric,
                        transform=str(best.get("transform") or ""),
                        lag=lag,
                        method=str(best.get("method") or ""),
                        coefficient=coefficient,
                        q_value=q_value,
                        rolling_stable=rolling_stable,
                    ),
                    **_source_provenance(item),
                }
            )
    return findings


def _panel_association_findings(
    item: Mapping[str, Any], payload: Mapping[str, Any]
) -> list[dict[str, Any]]:
    by_hypothesis = payload.get("associations_by_hypothesis") or {}
    if not isinstance(by_hypothesis, Mapping):
        return []
    findings: list[dict[str, Any]] = []
    for hypothesis_id, raw_bundle in by_hypothesis.items():
        if not isinstance(raw_bundle, Mapping):
            continue
        if raw_bundle.get("evidence_type") != "statistical_association":
            continue
        hypothesis = raw_bundle.get("hypothesis") or {}
        association = raw_bundle.get("association") or {}
        if not isinstance(hypothesis, Mapping) or not isinstance(
            association, Mapping
        ):
            continue
        outcome_id = str(hypothesis.get("outcome_metric") or "")
        candidate_id = str(hypothesis.get("candidate_metric") or "")
        if not outcome_id or not candidate_id:
            continue
        aggregate = association.get("aggregate_association") or {}
        stability = association.get("within_panel_direction_stability") or {}
        inner_mapping = association.get("mapping") or {}
        outcome_metric = _metric_business_label(outcome_id)
        candidate_metric = _metric_business_label(candidate_id)
        residual_pearson = _number(aggregate.get("residual_pearson"))
        residual_spearman = _number(aggregate.get("residual_spearman"))
        same_direction_ratio = _number(stability.get("same_direction_ratio"))
        stable_across_channels = bool(stability.get("stable"))
        mapping_coverage = _number(inner_mapping.get("coverage"))
        findings.append(
            {
                "finding_type": "cross_source_channel_panel_sensitivity",
                "evidence_state": "derived",
                "analysis_role": "auxiliary",
                "hypothesis_id": str(hypothesis_id),
                "outcome_metric_id": outcome_id,
                "outcome_metric": outcome_metric,
                "candidate_metric_id": candidate_id,
                "candidate_metric": candidate_metric,
                "transform": str(hypothesis.get("transform") or ""),
                "lag": hypothesis.get("lag"),
                "residual_pearson": residual_pearson,
                "residual_spearman": residual_spearman,
                "same_direction_ratio": same_direction_ratio,
                "stable_across_channels": stable_across_channels,
                "mapping_status": str(
                    inner_mapping.get("authority_status") or ""
                ),
                "mapping_authority_established": bool(
                    inner_mapping.get("authority_established")
                ),
                "mapping_coverage": mapping_coverage,
                "wording_limit": "sensitivity_only",
                "specific_channel_claim_allowed": False,
                "causal_claim_allowed": False,
                "contribution_claim_allowed": False,
                "statement": _panel_association_statement(
                    outcome_metric=outcome_metric,
                    candidate_metric=candidate_metric,
                    residual_pearson=residual_pearson,
                    residual_spearman=residual_spearman,
                    same_direction_ratio=same_direction_ratio,
                    stable_across_channels=stable_across_channels,
                    mapping_coverage=mapping_coverage,
                ),
                **_source_provenance(item),
            }
        )
    return findings


def _metric_business_label(value: Any) -> str:
    metric_id = str(value or "")
    return _METRIC_BUSINESS_LABELS.get(metric_id, metric_id)


def cross_source_auxiliary_claim_text(payload: Mapping[str, Any]) -> str:
    """Render the bounded, number-free claim admitted by stable evidence."""

    outcome_id = str(payload.get("primary_outcome") or "paid_amount")
    by_outcome = payload.get("associations_by_outcome") or {}
    raw_bundle = (
        by_outcome.get(outcome_id)
        if isinstance(by_outcome, Mapping)
        else None
    )
    raw_bundle = raw_bundle if isinstance(raw_bundle, Mapping) else {}
    association = raw_bundle.get("association") or {}
    association = association if isinstance(association, Mapping) else {}
    best = association.get("best_association") or {}
    best = best if isinstance(best, Mapping) else {}
    candidate_id = str(best.get("candidate_key") or "")
    outcome_label = _metric_business_label(outcome_id)
    candidate_label = _metric_business_label(candidate_id) if candidate_id else "玩法经营指标"
    transform_label = {
        "level": "原始水平",
        "difference": "日变化",
        "log_difference": "相对日变化",
        "signed_log_difference": "相对日变化",
    }.get(str(best.get("transform") or ""), "变化")
    lag = best.get("lag")
    if isinstance(lag, int) and not isinstance(lag, bool):
        if lag > 0:
            relation = f"{candidate_label}领先{outcome_label}"
        elif lag < 0:
            relation = f"{candidate_label}滞后{outcome_label}"
        else:
            relation = f"{candidate_label}与{outcome_label}同日"
    else:
        relation = f"{candidate_label}与{outcome_label}"
    coefficient = _number(best.get("coefficient"))
    direction = "正向" if coefficient is None or coefficient >= 0 else "负向"
    rolling = best.get("rolling") or {}
    stable = isinstance(rolling, Mapping) and rolling.get("stable") is True
    stability = "稳定" if stable else ""
    return (
        f"{relation}的{transform_label}呈{stability}{direction}统计关联；"
        "该结果作为辅助诊断，不能解释贡献金额或因果关系。"
    )


def _temporal_association_statement(
    *,
    outcome_metric: str,
    candidate_metric: str,
    transform: str,
    lag: Any,
    method: str,
    coefficient: float | None,
    q_value: float | None,
    rolling_stable: bool,
) -> str:
    lag_value = int(lag) if isinstance(lag, int) and not isinstance(lag, bool) else 0
    if lag_value > 0:
        time_relation = f"{candidate_metric}领先{outcome_metric}{lag_value}天"
    elif lag_value < 0:
        time_relation = f"{candidate_metric}滞后{outcome_metric}{abs(lag_value)}天"
    else:
        time_relation = f"{candidate_metric}与{outcome_metric}同日"
    direction = (
        "正向" if coefficient is not None and coefficient >= 0 else "负向"
    )
    method_label = {"pearson": "线性", "spearman": "排序"}.get(
        method, "统计"
    )
    transform_label = {
        "level": "原始水平",
        "difference": "日变化",
        "log_difference": "相对日变化",
        "signed_log_difference": "对称相对日变化",
    }.get(transform, "变化")
    coefficient_text = _business_decimal(coefficient, digits=3)
    q_value_text = _business_decimal(q_value, digits=4)
    stability_text = (
        "滚动窗口中的关联方向较稳定"
        if rolling_stable
        else "滚动窗口中的关联方向尚未稳定"
    )
    return (
        f"{time_relation}的{transform_label}呈{direction}关联"
        f"（{method_label}系数{coefficient_text}，校正后q值{q_value_text}）；"
        f"{stability_text}。该结果只提供玩法关联背景，"
        "不能解释贡献金额或因果关系。"
    )


def _panel_association_statement(
    *,
    outcome_metric: str,
    candidate_metric: str,
    residual_pearson: float | None,
    residual_spearman: float | None,
    same_direction_ratio: float | None,
    stable_across_channels: bool,
    mapping_coverage: float | None,
) -> str:
    stability_text = (
        "多个渠道内的方向较一致"
        if stable_across_channels
        else "多个渠道内的方向一致性尚不足"
    )
    return (
        "按机械生成、尚未确认为正式口径的渠道映射做敏感性检查后，"
        f"{candidate_metric}与{outcome_metric}在控制日期共同波动和渠道长期差异后，"
        f"残差线性相关系数为{_business_decimal(residual_pearson, digits=3)}，"
        f"残差排序相关系数为{_business_decimal(residual_spearman, digits=3)}；"
        f"{candidate_metric}与{outcome_metric}的渠道方向一致比例为"
        f"{_business_percent(same_direction_ratio)}，"
        f"{stability_text}，映射覆盖率为{_business_percent(mapping_coverage)}。"
        "该结果只用于渠道内部共变敏感性判断，"
        "不能发布具体渠道、贡献或因果结论。"
    )


def _business_decimal(value: float | None, *, digits: int) -> str:
    if value is None:
        return "未知"
    return f"{value:.{digits}f}"


def _business_percent(value: float | None) -> str:
    if value is None:
        return "未知"
    return f"{value * 100:.1f}%"


def _evidence_is_verified(item: Mapping[str, Any]) -> bool:
    return _explicit_evidence_state(item) not in {"candidate", "unresolved"}


def _explicit_evidence_state(item: Mapping[str, Any]) -> str:
    return str(item.get("evidence_state") or "").strip().lower()


def _normalized_evidence_state(value: Any, *, default: str) -> str:
    state = str(value or default).strip().lower()
    if state in {"verified", "derived", "candidate", "unresolved"}:
        return state
    return "unresolved"


def _eligible_next_routes(
    available_routes: Sequence[Mapping[str, Any]],
    *,
    dominant_factor: Mapping[str, Any] | None,
    factor_states: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if dominant_factor is None:
        return []
    dominant_keys = {
        str(dominant_factor.get("factor_id") or ""),
        str(dominant_factor.get("factor") or ""),
    }
    factor_state = _matching_factor_state(dominant_factor, factor_states)
    if factor_state:
        dominant_keys.update(
            {
                str(factor_state.get("factor_id") or ""),
                str(factor_state.get("factor") or ""),
            }
        )
    dominant_keys.discard("")

    candidates: list[dict[str, Any]] = []
    for raw in available_routes:
        if not isinstance(raw, Mapping) or not _route_is_executable(raw):
            continue
        parents = _route_parent_keys(raw)
        if not dominant_keys.intersection(parents):
            continue
        candidate = dict(raw)
        candidate["evidence_state"] = "candidate"
        candidate["information_gain"] = _score(
            raw, "information_gain", "expected_information_gain", "informationGain"
        )
        candidate["materiality"] = _score(
            raw, "materiality", "expected_materiality"
        )
        candidate["actionability"] = _score(
            raw, "actionability", "expected_actionability"
        )
        candidate["priority_basis"] = "information_gain_then_business_value"
        candidates.append(candidate)
    return sorted(
        candidates,
        key=lambda item: (
            -item["information_gain"],
            -item["materiality"],
            -item["actionability"],
            str(item.get("route_id") or ""),
        ),
    )


def _route_is_executable(route: Mapping[str, Any]) -> bool:
    if route.get("executable") is True:
        return True
    status = str(
        route.get("execution_status") or route.get("status") or ""
    ).lower()
    return status in {"executable", "ready"}


def _route_parent_keys(route: Mapping[str, Any]) -> set[str]:
    keys = {
        str(route.get("parent_factor_id") or ""),
        str(route.get("source_factor_id") or ""),
        str(route.get("target_factor_id") or ""),
        str(route.get("drilldown_of") or ""),
    }
    applies_to = route.get("applies_to")
    if isinstance(applies_to, Sequence) and not isinstance(
        applies_to, (str, bytes, bytearray)
    ):
        keys.update(str(value) for value in applies_to)
    elif applies_to:
        keys.add(str(applies_to))
    keys.discard("")
    return keys


def _diagnostic_sufficiency(
    *,
    movement: Mapping[str, Any] | None,
    decomposition: Mapping[str, Any] | None,
    dominant: Mapping[str, Any] | None,
    mechanism: Mapping[str, Any] | None,
    eligible_routes: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    hard_reasons: list[str] = []
    if movement is None:
        hard_reasons.append("verified_metric_movement_missing")
    if decomposition is None:
        hard_reasons.append("formula_decomposition_missing")
    elif not _decomposition_is_reconciled(decomposition):
        hard_reasons.append("formula_decomposition_unreconciled")
    elif movement is not None and not _decomposition_matches_movement(
        decomposition, movement
    ):
        hard_reasons.append("formula_metric_movement_mismatch")
    if dominant is None and decomposition is not None:
        hard_reasons.append("dominant_driver_missing")
    if hard_reasons:
        return {"status": "bounded", "reasons": hard_reasons, "next_routes": []}

    mechanism_status = str((mechanism or {}).get("status") or "unresolved")
    if mechanism_status == "verified":
        return {
            "status": "sufficient",
            "reasons": ["dominant_driver_mechanism_verified"],
            "next_routes": [],
        }

    mechanism_reason = (
        "dominant_driver_mechanism_candidate_only"
        if mechanism_status == "candidate"
        else "dominant_driver_mechanism_unresolved"
    )
    if eligible_routes:
        return {
            "status": "continue",
            "reasons": [
                mechanism_reason,
                "executable_diagnostic_route_available",
            ],
            "next_routes": list(eligible_routes),
        }
    return {
        "status": "bounded",
        "reasons": [
            mechanism_reason,
            "no_executable_route_for_unresolved_dominant_driver",
        ],
        "next_routes": [],
    }


def _decomposition_is_reconciled(
    decomposition: Mapping[str, Any] | None,
) -> bool:
    return bool(
        decomposition
        and str(decomposition.get("reconciliation_status") or "").lower()
        == "reconciled"
    )


def _decomposition_matches_movement(
    decomposition: Mapping[str, Any] | None,
    movement: Mapping[str, Any],
) -> bool:
    if not _decomposition_is_reconciled(decomposition):
        return False
    observed_change = _number(movement.get("change"))
    if observed_change is None:
        return False
    metric_delta = _number(decomposition.get("metric_delta"))
    contribution_total = sum(
        item["contribution"] for item in decomposition.get("contributions") or ()
    )
    return (
        metric_delta is not None
        and _numbers_close(metric_delta, observed_change)
        and _numbers_close(contribution_total, observed_change)
    )


def _matching_factor_state(
    factor: Mapping[str, Any],
    factor_states: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    factor_keys = {
        str(factor.get("factor_id") or ""),
        str(factor.get("factor") or ""),
    }
    factor_keys.discard("")
    for state in factor_states:
        state_keys = {
            str(state.get("factor_id") or ""),
            str(state.get("factor") or ""),
        }
        state_keys.discard("")
        if factor_keys.intersection(state_keys):
            return state
    return None


def _factor_id(item: Mapping[str, Any]) -> str:
    return str(
        item.get("factor_id")
        or item.get("component_id")
        or item.get("factor")
        or item.get("business_name")
        or ""
    ).strip()


def _source_evidence_refs(item: Mapping[str, Any]) -> list[str]:
    return _dedupe_strings(
        [
            *_string_list(item.get("source_evidence_refs")),
            *_string_list(item.get("source_evidence_ref")),
            *_string_list(item.get("evidence_ref")),
        ]
    )


def _source_result_refs(item: Mapping[str, Any]) -> list[str]:
    return _dedupe_strings(
        [
            *_string_list(item.get("source_result_refs")),
            *_string_list(item.get("source_result_ref")),
            *_string_list(item.get("result_refs")),
        ]
    )


def _source_provenance(item: Mapping[str, Any]) -> dict[str, list[str]]:
    evidence_refs = _source_evidence_refs(item)
    result_refs = _source_result_refs(item)
    return {
        "source_evidence_refs": evidence_refs,
        **({"source_result_refs": result_refs} if result_refs else {}),
    }


def _string_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [str(item) for item in value if str(item)]
    return [str(value)]


def _dedupe_strings(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _first_number(item: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _number(item.get(key))
        if value is not None:
            return value
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _score(item: Mapping[str, Any], *keys: str) -> float:
    value = _first_number(item, *keys)
    return value if value is not None else 0.0


def _numbers_close(left: float, right: float) -> bool:
    tolerance = max(1e-9, max(abs(left), abs(right)) * 1e-9)
    return abs(left - right) <= tolerance


def _ratio(change: float, baseline: float) -> float | None:
    if not baseline:
        return None
    return change / abs(baseline)


def _sign(value: float) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def _direction(value: float) -> str:
    return {1: "increase", -1: "decrease", 0: "unchanged"}[_sign(value)]


def _alignment(metric_change: float, factor_change: float) -> str:
    metric_sign = _sign(metric_change)
    factor_sign = _sign(factor_change)
    if not metric_sign or not factor_sign:
        return "neutral"
    return "supports" if metric_sign == factor_sign else "opposes"
