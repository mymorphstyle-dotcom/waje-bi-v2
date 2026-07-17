from __future__ import annotations

import pytest

from bi_agent.runtime.diagnostic_insights import (
    build_diagnostic_insight_portfolio,
)
from bi_agent.runtime.final_narrative_binding import (
    build_authority_safe_narrative,
    build_narrative_authority_record,
)


def _verified_evidence() -> list[dict[str, object]]:
    return [
        {
            "evidence_ref": "comparison:ready",
            "capability_id": "compare_periods",
            "claim_input_ready": True,
            "evidence_type": "descriptive_comparison",
            "limitations": [],
            "typed_payload": {
                "metric": "gross_value",
                "target_value": 120.0,
                "baseline_value": 100.0,
                "target_window_id": "window:target",
                "baseline_window_id": "window:baseline",
            },
        },
        {
            "evidence_ref": "decomposition:ready",
            "capability_id": "driver_decomposition",
            "claim_input_ready": True,
            "evidence_type": "accounting_contribution",
            "limitations": [],
            "typed_payload": {
                "metric": "gross_value",
                "decompositions": [
                    {
                        "target_value": 120.0,
                        "baseline_value": 100.0,
                        "metric_delta": 20.0,
                        "core_reconciliation_status": "reconciled",
                        "core_factor_contributions": [
                            {
                                "component_id": "unit_value",
                                "baseline_value": 5.0,
                                "target_value": 6.0,
                                "contribution": 28.0,
                                "contribution_share": 1.4,
                            },
                            {
                                "component_id": "engagement_rate",
                                "baseline_value": 3.0,
                                "target_value": 2.7,
                                "contribution": -10.0,
                                "contribution_share": -0.5,
                            },
                            {
                                "component_id": "active_entities",
                                "baseline_value": 20.0,
                                "target_value": 20.2,
                                "contribution": 2.0,
                                "contribution_share": 0.1,
                            },
                        ],
                    }
                ],
            },
        },
    ]


def _factor_states() -> list[dict[str, object]]:
    return [
        {
            "factor_id": "unit_value",
            "factor": "单位价值",
            "state": "已量化贡献",
            "baseline": 5.0,
            "target": 6.0,
            "change": 1.0,
            "contribution": 28.0,
            "diagnostic_role": "structure",
            "mechanism_status": "unresolved",
        },
        {
            "factor_id": "engagement_rate",
            "factor": "参与频次",
            "state": "已量化贡献",
            "baseline": 3.0,
            "target": 2.7,
            "change": -0.3,
            "contribution": -10.0,
            "diagnostic_role": "breadth",
        },
        {
            "factor_id": "new_entities",
            "factor": "新增主体",
            "state": "已观察变化，贡献尚未量化",
            "baseline": 40.0,
            "target": 32.0,
            "change": -8.0,
            "diagnostic_role": "breadth",
        },
    ]


def _routes() -> list[dict[str, object]]:
    return [
        {
            "route_id": "unit-value-by-submarket",
            "parent_factor_id": "unit_value",
            "route_kind": "dimension_drilldown",
            "dimension_id": "submarket",
            "executable": True,
            "information_gain": 0.91,
            "materiality": 0.85,
            "actionability": 0.8,
        },
        {
            "route_id": "unit-value-by-package",
            "parent_factor_id": "unit_value",
            "route_kind": "factor_drilldown",
            "factor_id": "package_mix",
            "executable": True,
            "information_gain": 0.78,
            "materiality": 0.95,
            "actionability": 0.9,
        },
        {
            "route_id": "unrelated-high-score",
            "parent_factor_id": "active_entities",
            "route_kind": "dimension_drilldown",
            "dimension_id": "acquisition_source",
            "executable": True,
            "information_gain": 0.99,
            "materiality": 0.99,
            "actionability": 0.99,
        },
    ]


def _portfolio(**overrides: object) -> dict[str, object]:
    inputs: dict[str, object] = {
        "question": {
            "metric_id": "gross_value",
            "target_window_id": "window:target",
            "baseline_window_id": "window:baseline",
        },
        "evidence": _verified_evidence(),
        "factor_states": _factor_states(),
        "available_routes": _routes(),
    }
    inputs.update(overrides)
    return build_diagnostic_insight_portfolio(**inputs)


def test_builds_ranked_verified_drivers_and_derived_counterfactuals() -> None:
    portfolio = _portfolio()

    dominant = next(
        item
        for item in portfolio["insights"]
        if item["insight_type"] == "dominant_driver"
    )
    assert dominant["factor_id"] == "unit_value"
    assert dominant["evidence_state"] == "verified"
    assert dominant["contribution"] == pytest.approx(28.0)
    assert dominant["contribution_share"] == pytest.approx(1.4)

    offset = next(
        item
        for item in portfolio["insights"]
        if item["insight_type"] == "offsetting_driver"
    )
    assert offset["factor_id"] == "engagement_rate"
    assert offset["evidence_state"] == "verified"

    without_unit_value = next(
        item
        for item in portfolio["counterfactuals"]
        if item["removed_factor_id"] == "unit_value"
    )
    assert without_unit_value["evidence_state"] == "derived"
    assert without_unit_value["observed_change"] == pytest.approx(20.0)
    assert without_unit_value["change_without_factor"] == pytest.approx(-8.0)
    assert without_unit_value["direction_without_factor"] == "decrease"
    assert without_unit_value["derivation"] == "observed_change_minus_contribution"
    assert without_unit_value["source_evidence_refs"] == [
        "comparison:ready",
        "decomposition:ready",
    ]


def test_exposes_growth_breadth_and_structure_without_claiming_mechanism() -> None:
    portfolio = _portfolio()

    signals = portfolio["growth_quality_signals"]
    concentration = next(
        item for item in signals if item["signal_type"] == "driver_concentration"
    )
    assert concentration["evidence_state"] == "derived"
    assert concentration["dominant_factor_id"] == "unit_value"
    assert concentration["dominant_absolute_share"] == pytest.approx(28 / 40)

    breadth = next(
        item
        for item in signals
        if item.get("factor_id") == "new_entities"
    )
    assert breadth["signal_type"] == "breadth_movement"
    assert breadth["evidence_state"] == "verified"
    assert breadth["alignment_with_metric"] == "opposes"
    assert breadth["contribution_status"] == "unquantified"

    mechanism = next(
        item
        for item in portfolio["insights"]
        if item["insight_type"] == "mechanism_depth"
        and item["factor_id"] == "unit_value"
    )
    assert mechanism["evidence_state"] == "unresolved"
    assert mechanism["status"] == "unresolved"


def test_reconciled_formula_with_executable_dominant_child_route_must_continue() -> None:
    portfolio = _portfolio()

    sufficiency = portfolio["diagnostic_sufficiency"]
    assert sufficiency["status"] == "continue"
    assert sufficiency["reasons"] == [
        "dominant_driver_mechanism_unresolved",
        "executable_diagnostic_route_available",
    ]
    assert [route["route_id"] for route in sufficiency["next_routes"]] == [
        "unit-value-by-submarket",
        "unit-value-by-package",
    ]
    assert sufficiency["next_routes"][0]["evidence_state"] == "candidate"
    assert portfolio["next_best_candidate"]["route_id"] == (
        "unit-value-by-submarket"
    )
    assert portfolio["next_best_candidate"]["evidence_state"] == "candidate"


def test_unresolved_dominant_mechanism_without_executable_route_is_bounded() -> None:
    portfolio = _portfolio(available_routes=[])

    assert portfolio["next_best_candidate"] is None
    assert portfolio["diagnostic_sufficiency"] == {
        "status": "bounded",
        "reasons": [
            "dominant_driver_mechanism_unresolved",
            "no_executable_route_for_unresolved_dominant_driver",
        ],
        "next_routes": [],
    }


def test_verified_dominant_mechanism_can_be_sufficient() -> None:
    factor_states = _factor_states()
    factor_states[0] = {
        **factor_states[0],
        "mechanism_status": "verified",
        "mechanism_evidence_refs": ["mechanism:ready"],
    }
    portfolio = _portfolio(factor_states=factor_states)

    mechanism = next(
        item
        for item in portfolio["insights"]
        if item["insight_type"] == "mechanism_depth"
        and item["factor_id"] == "unit_value"
    )
    assert mechanism["evidence_state"] == "verified"
    assert mechanism["source_evidence_refs"] == ["mechanism:ready"]
    assert portfolio["diagnostic_sufficiency"] == {
        "status": "sufficient",
        "reasons": ["dominant_driver_mechanism_verified"],
        "next_routes": [],
    }


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        (
            lambda evidence: evidence[1]["typed_payload"]["decompositions"][0].update(
                {"core_reconciliation_status": "mismatch"}
            ),
            "formula_decomposition_unreconciled",
        ),
        (
            lambda evidence: evidence[0].update({"claim_input_ready": False}),
            "verified_metric_movement_missing",
        ),
    ],
)
def test_missing_hard_diagnostic_inputs_are_bounded(
    mutation,
    expected_reason: str,
) -> None:
    evidence = _verified_evidence()
    mutation(evidence)

    portfolio = _portfolio(evidence=evidence)

    assert portfolio["diagnostic_sufficiency"]["status"] == "bounded"
    assert expected_reason in portfolio["diagnostic_sufficiency"]["reasons"]
    assert portfolio["diagnostic_sufficiency"]["next_routes"] == []


def test_preserves_verified_dimension_findings_without_promoting_candidates() -> None:
    evidence = _verified_evidence()
    evidence.append(
        {
            "evidence_ref": "dimension:ready",
            "capability_id": "segment_contribution",
            "claim_input_ready": True,
            "evidence_type": "statistical_association",
            "typed_payload": {
                "dimension_findings": [
                    {
                        "dimension_id": "submarket",
                        "member": "segment-alpha",
                        "contribution": 7.5,
                        "information_gain": 0.72,
                    }
                ]
            },
        }
    )
    evidence.append(
        {
            "evidence_ref": "dimension:candidate",
            "capability_id": "segment_contribution",
            "claim_input_ready": True,
            "evidence_state": "candidate",
            "evidence_type": "statistical_association",
            "typed_payload": {
                "dimension_findings": [
                    {
                        "dimension_id": "package",
                        "member": "candidate-segment",
                    }
                ]
            },
        }
    )

    portfolio = _portfolio(evidence=evidence)

    assert portfolio["dimension_findings"] == [
        {
            "dimension_id": "submarket",
            "member": "segment-alpha",
            "contribution": 7.5,
            "information_gain": 0.72,
            "evidence_state": "verified",
            "source_evidence_refs": ["dimension:ready"],
        },
        {
            "dimension_id": "package",
            "member": "candidate-segment",
            "evidence_state": "candidate",
            "source_evidence_refs": ["dimension:candidate"],
        },
    ]
    assert portfolio["next_best_candidate"]["evidence_state"] == "candidate"


def test_projects_temporal_and_channel_panel_associations_as_candidate_evidence() -> None:
    evidence = _verified_evidence()
    evidence.extend(
        [
            {
                "evidence_ref": "association:overall",
                "result_refs": ["result:paid-daily", "result:gameplay-daily"],
                "capability_id": "cross_source_association",
                "claim_input_ready": True,
                "evidence_type": "statistical_association",
                "typed_payload": {
                    "associations_by_outcome": {
                        "paid_amount": {
                            "evidence_type": "statistical_association",
                            "wording_limit": "stable_association",
                            "association": {
                                "lag_semantics": "positive_lag_means_candidate_precedes_target",
                                "supported_associations": [
                                    {
                                        "candidate_key": "player_bet_amount",
                                        "transform": "signed_log_difference",
                                        "method": "spearman",
                                        "lag": 0,
                                        "sample_size": 180,
                                        "coefficient": 0.61,
                                        "q_value": 0.01,
                                        "rolling": {
                                            "stable": True,
                                            "same_direction_ratio": 0.8,
                                        },
                                    }
                                ],
                            },
                        }
                    }
                },
            },
            {
                "evidence_ref": "association:panel",
                "result_refs": ["result:paid-panel", "result:gameplay-panel"],
                "capability_id": "cross_source_panel_association",
                "claim_input_ready": True,
                "evidence_type": "statistical_association",
                "typed_payload": {
                    "mapping": {
                        "authority_status": "candidate_mechanical_crosswalk",
                        "authority_established": False,
                    },
                    "associations_by_hypothesis": {
                        "paid-vs-bet-change-lag0": {
                            "hypothesis": {
                                "hypothesis_id": "paid-vs-bet-change-lag0",
                                "outcome_metric": "paid_amount",
                                "candidate_metric": "player_bet_amount",
                                "transform": "signed_log_difference",
                                "lag": 0,
                            },
                            "evidence_type": "statistical_association",
                            "association": {
                                "aggregate_association": {
                                    "residual_pearson": 0.37,
                                    "residual_spearman": 0.34,
                                },
                                "within_panel_direction_stability": {
                                    "stable": True,
                                    "same_direction_ratio": 0.71,
                                },
                                "mapping": {
                                    "authority_status": (
                                        "candidate_mechanical_crosswalk"
                                    ),
                                    "authority_established": False,
                                    "coverage": 0.97,
                                },
                            },
                        },
                    },
                },
            },
        ]
    )

    portfolio = _portfolio(evidence=evidence)

    overall, panel = portfolio["cross_source_findings"]
    assert overall["finding_type"] == "cross_source_temporal_association"
    assert overall["candidate_metric"] == "玩家投注金额"
    assert overall["evidence_state"] == "derived"
    assert "玩法关联背景" in overall["statement"]
    assert "贡献金额或因果关系" in overall["statement"]
    assert overall["contribution_claim_allowed"] is False
    assert overall["source_evidence_refs"] == ["association:overall"]
    assert overall["source_result_refs"] == [
        "result:paid-daily",
        "result:gameplay-daily",
    ]
    assert panel["finding_type"] == "cross_source_channel_panel_sensitivity"
    assert panel["evidence_state"] == "derived"
    assert panel["stable_across_channels"] is True
    assert panel["specific_channel_claim_allowed"] is False
    assert panel["wording_limit"] == "sensitivity_only"
    assert panel["mapping_status"] == "candidate_mechanical_crosswalk"
    assert panel["mapping_authority_established"] is False
    assert panel["transform"] == "signed_log_difference"
    assert panel["lag"] == 0
    assert panel["source_evidence_refs"] == ["association:panel"]
    assert panel["source_result_refs"] == [
        "result:paid-panel",
        "result:gameplay-panel",
    ]
    assert "渠道内部共变敏感性" in panel["statement"]

    authority = build_narrative_authority_record(
        verified_claims=(),
        evidence=evidence,
        visible_limitations=(),
        accepted_assumptions=(),
        diagnostic_insights=portfolio,
    )
    projection = build_authority_safe_narrative(
        authority,
        required_claim_types=(),
    )

    assert projection["status"] == "bound"
    assert overall["statement"] in projection["narrative"]
    assert panel["statement"] in projection["narrative"]


def test_candidate_comparison_cannot_be_promoted_to_verified_movement() -> None:
    evidence = _verified_evidence()
    evidence[0]["evidence_state"] = "candidate"

    portfolio = _portfolio(evidence=evidence)

    assert all(
        item["insight_type"] != "metric_movement"
        for item in portfolio["insights"]
    )
    assert portfolio["diagnostic_sufficiency"]["status"] == "bounded"
    assert "verified_metric_movement_missing" in portfolio[
        "diagnostic_sufficiency"
    ]["reasons"]


def test_formula_and_metric_movement_must_reconcile_to_each_other() -> None:
    evidence = _verified_evidence()
    evidence[1]["typed_payload"]["decompositions"][0]["metric_delta"] = 21.0

    portfolio = _portfolio(evidence=evidence)

    assert portfolio["counterfactuals"] == []
    assert portfolio["diagnostic_sufficiency"]["status"] == "bounded"
    assert "formula_metric_movement_mismatch" in portfolio[
        "diagnostic_sufficiency"
    ]["reasons"]


def test_candidate_factor_state_remains_candidate_in_quality_signals() -> None:
    factor_states = _factor_states()
    factor_states[2] = {**factor_states[2], "evidence_state": "candidate"}

    portfolio = _portfolio(factor_states=factor_states)

    signal = next(
        item
        for item in portfolio["growth_quality_signals"]
        if item.get("factor_id") == "new_entities"
    )
    assert signal["evidence_state"] == "candidate"
