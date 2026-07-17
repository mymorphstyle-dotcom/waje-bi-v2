from __future__ import annotations

import math
from decimal import Decimal

import pytest

from bi_agent.capabilities.cross_source_panel_association import (
    cross_source_panel_association,
)


def _associated_panel_rows(*, periods: int = 18, panels: int = 5):
    rows = []
    for time_index in range(periods):
        for panel_index in range(panels):
            interaction = ((time_index + 1) * (panel_index + 2) % 13) - 6
            candidate = (
                40.0
                + 7.0 * panel_index
                + 3.0 * time_index
                + interaction
            )
            target = (
                100.0
                - 5.0 * panel_index
                + 8.0 * time_index
                + 1.9 * interaction
                + ((time_index + panel_index) % 3 - 1) * 0.03
            )
            rows.append(
                {
                    "day": f"2026-05-{time_index + 1:02d}",
                    "channel": f"channel-{panel_index}",
                    "paid_amount": target,
                    "bet_amount": candidate,
                }
            )
    return rows


def _hypothesis(*, transform: str = "level", lag: int = 0):
    return {
        "hypothesis_id": f"paid-vs-bet-{transform}-lag{lag}",
        "outcome_key": "paid_amount",
        "candidate_key": "bet_amount",
        "transform": transform,
        "lag": lag,
    }


def test_contracted_mapping_can_publish_aggregate_panel_association():
    result = cross_source_panel_association(
        _associated_panel_rows(),
        time_key="day",
        panel_key="channel",
        hypothesis=_hypothesis(),
        mapping_authority_status="contracted",
        mapping_coverage=1.0,
        min_samples=30,
        min_panels=3,
        min_panel_samples=6,
        result_refs=("result:paid-channel", "result:gameplay-channel"),
    )

    assert result.capability == "cross_source_panel_association"
    assert result.evidence_type == "statistical_association"
    assert result.strength == "medium"
    assert result.wording_limit == "statistical_association"
    assert result.result_refs == (
        "result:paid-channel",
        "result:gameplay-channel",
    )
    assert result.numeric_facts["analysis_sample_size"] == 90
    assert result.numeric_facts["analysis_panel_count"] == 5
    assert result.numeric_facts["pair_coverage"] == 1.0
    assert result.numeric_facts["residual_pearson"] > 0.99
    assert result.numeric_facts["residual_spearman"] > 0.98
    assert result.numeric_facts["same_direction_ratio"] == 1.0

    payload = result.typed_payload
    assert payload["claim_ceiling"] == "statistical_association"
    assert payload["uncertainty_estimate_available"] is False
    assert payload["causal_claim_allowed"] is False
    assert payload["contribution_claim_allowed"] is False
    assert payload["specific_panel_claim_allowed"] is False
    assert payload["coefficient_is_contribution"] is False
    assert payload["two_way_fixed_effects"]["converged"] is True
    assert "panel_estimates" not in payload
    assert "channel-0" not in repr(payload)


def test_mechanical_crosswalk_is_always_sensitivity_only():
    result = cross_source_panel_association(
        _associated_panel_rows(),
        time_key="day",
        panel_key="channel",
        hypothesis=_hypothesis(),
        mapping_authority_status="candidate_mechanical_crosswalk",
        mapping_coverage=1.0,
        min_samples=30,
        min_panels=3,
        min_panel_samples=6,
    )

    assert result.evidence_type == "statistical_association"
    assert result.strength == "low"
    assert result.wording_limit == "sensitivity_only"
    assert result.typed_payload["claim_ceiling"] == "sensitivity_only"
    assert result.typed_payload["mapping"]["authority_established"] is False
    assert any("mapping" in limitation for limitation in result.limitations)


def test_low_mapping_coverage_and_incomplete_pairs_are_reported_and_degraded():
    rows = _associated_panel_rows()
    rows[0]["bet_amount"] = None
    rows[1]["paid_amount"] = float("nan")

    result = cross_source_panel_association(
        rows,
        time_key="day",
        panel_key="channel",
        hypothesis=_hypothesis(),
        mapping_authority_status="contracted",
        mapping_coverage=0.70,
        min_mapping_coverage=0.80,
        min_pair_coverage=0.90,
        min_samples=30,
        min_panels=3,
        min_panel_samples=6,
    )

    assert result.numeric_facts["input_row_count"] == 90
    assert result.numeric_facts["complete_pair_count"] == 88
    assert math.isclose(result.numeric_facts["pair_coverage"], 88 / 90)
    assert result.numeric_facts["mapping_coverage"] == 0.70
    assert result.wording_limit == "sensitivity_only"
    assert result.typed_payload["claim_ceiling"] == "sensitivity_only"
    assert result.typed_payload["coverage"]["pair_coverage_sufficient"] is True
    assert result.typed_payload["mapping"]["coverage_sufficient"] is False


def test_minimum_sample_and_panel_requirements_prevent_association_evidence():
    result = cross_source_panel_association(
        _associated_panel_rows(periods=6, panels=2),
        time_key="day",
        panel_key="channel",
        hypothesis=_hypothesis(),
        mapping_authority_status="contracted",
        mapping_coverage=1.0,
        min_samples=30,
        min_panels=3,
        min_panel_samples=3,
    )

    assert result.evidence_type == "insufficient"
    assert result.strength == "low"
    assert result.wording_limit == "sensitivity_only"
    assert result.typed_payload["claim_ceiling"] == "sensitivity_only"
    assert result.typed_payload["minimum_requirements"]["samples_met"] is False
    assert result.typed_payload["minimum_requirements"]["panels_met"] is False
    assert any("minimum sample" in limitation for limitation in result.limitations)
    assert any("minimum panel" in limitation for limitation in result.limitations)


def test_two_way_fixed_effects_remove_common_time_and_panel_levels():
    rows = []
    for time_index in range(12):
        for panel_index in range(4):
            rows.append(
                {
                    "day": f"t-{time_index}",
                    "channel": f"p-{panel_index}",
                    "paid_amount": 100 + 20 * time_index + 9 * panel_index,
                    "bet_amount": 50 + 8 * time_index + 4 * panel_index,
                }
            )

    result = cross_source_panel_association(
        rows,
        time_key="day",
        panel_key="channel",
        hypothesis=_hypothesis(),
        mapping_authority_status="contracted",
        mapping_coverage=1.0,
        min_samples=30,
        min_panels=3,
        min_panel_samples=6,
    )

    assert result.numeric_facts["residual_pearson"] is None
    assert result.numeric_facts["residual_spearman"] is None
    assert result.evidence_type == "insufficient"
    assert result.wording_limit == "sensitivity_only"
    assert any("residual variance" in limitation for limitation in result.limitations)


def test_duplicate_panel_time_cells_are_rejected_before_statistics():
    rows = _associated_panel_rows()
    rows.append(dict(rows[0]))

    with pytest.raises(ValueError, match="panel-time cells must be unique"):
        cross_source_panel_association(
            rows,
            time_key="day",
            panel_key="channel",
            hypothesis=_hypothesis(),
        )


def test_decimal_metrics_and_an_unbalanced_panel_are_supported():
    rows = _associated_panel_rows()
    rows = [
        {
            **row,
            "paid_amount": Decimal(str(row["paid_amount"])),
            "bet_amount": Decimal(str(row["bet_amount"])),
        }
        for row in rows
        if not (row["channel"] == "channel-4" and row["day"] in {"2026-05-01", "2026-05-02"})
    ]

    result = cross_source_panel_association(
        rows,
        time_key="day",
        panel_key="channel",
        hypothesis=_hypothesis(),
        mapping_authority_status="contracted",
        mapping_coverage=1.0,
        min_samples=30,
        min_panels=3,
        min_panel_samples=6,
    )

    assert result.numeric_facts["analysis_sample_size"] == 88
    assert result.numeric_facts["residual_pearson"] > 0.99
    assert result.typed_payload["two_way_fixed_effects"]["converged"] is True
    assert result.typed_payload["two_way_fixed_effects"]["outcome_iterations"] > 1
    assert result.wording_limit == "statistical_association"


@pytest.mark.parametrize(
    ("parameter", "value"),
    (("mapping_coverage", -0.1), ("mapping_coverage", 1.1)),
)
def test_mapping_coverage_must_be_a_ratio(parameter, value):
    kwargs = {parameter: value}
    with pytest.raises(ValueError, match="mapping_coverage"):
        cross_source_panel_association(
            _associated_panel_rows(),
            time_key="day",
            panel_key="channel",
            hypothesis=_hypothesis(),
            **kwargs,
        )


def test_explicit_hypothesis_is_recorded_with_coverage_semantics():
    result = cross_source_panel_association(
        _associated_panel_rows(),
        time_key="day",
        panel_key="channel",
        hypothesis={
            "hypothesis_id": "paid-vs-bet-level-lag0",
            "outcome_key": "paid_amount",
            "candidate_key": "bet_amount",
            "transform": "level",
            "lag": 0,
        },
        mapping_authority_status="candidate_mechanical_crosswalk",
        mapping_coverage=0.7,
        mapping_coverage_basis={
            "combination": "minimum_of_source_metric_coverage",
            "outcome": {
                "coverage": 0.7,
                "basis": "absolute_metric_mass",
            },
            "candidate": {
                "coverage": 0.9,
                "basis": "absolute_metric_mass",
            },
            "limiting_side": "outcome",
        },
        min_samples=30,
        min_panels=3,
        min_panel_samples=6,
    )

    assert result.typed_payload["hypothesis"] == {
        "hypothesis_id": "paid-vs-bet-level-lag0",
        "outcome_key": "paid_amount",
        "candidate_key": "bet_amount",
        "transform": "level",
        "lag": 0,
    }
    assert result.typed_payload["coverage"]["pair_coverage_basis"] == (
        "complete_transformed_lagged_pairs_over_aligned_opportunities"
    )
    assert result.typed_payload["mapping"]["coverage_basis"]["outcome"] == {
        "coverage": 0.7,
        "basis": "absolute_metric_mass",
    }
    assert result.typed_payload["mapping"]["authority_established"] is False
    assert result.wording_limit == "sensitivity_only"
    assert "owner" not in repr(result.typed_payload).casefold()
    assert "owner" not in repr(result.limitations).casefold()


def test_transform_and_lag_are_applied_within_each_panel_before_fixed_effects():
    periods = 16
    panels = 4
    rows = []
    candidate_levels = [100.0 + panel * 10.0 for panel in range(panels)]
    outcome_levels = [500.0 - panel * 7.0 for panel in range(panels)]
    increments_by_panel = [
        [((time + 2) * (panel + 3) % 11) - 5 + panel * 0.07 for time in range(periods)]
        for panel in range(panels)
    ]
    for time in range(periods):
        for panel in range(panels):
            candidate_levels[panel] += increments_by_panel[panel][time]
            if time > 0:
                outcome_levels[panel] += 2.5 * increments_by_panel[panel][time - 1]
            rows.append(
                {
                    "day": f"t-{time:02d}",
                    "channel": f"p-{panel}",
                    "paid_amount": outcome_levels[panel],
                    "bet_amount": candidate_levels[panel],
                }
            )

    result = cross_source_panel_association(
        rows,
        time_key="day",
        panel_key="channel",
        hypothesis={
            "hypothesis_id": "paid-vs-bet-difference-lag1",
            "outcome_key": "paid_amount",
            "candidate_key": "bet_amount",
            "transform": "difference",
            "lag": 1,
        },
        mapping_authority_status="contracted",
        mapping_coverage=1.0,
        mapping_coverage_basis={
            "combination": "minimum_of_source_metric_coverage",
            "outcome": {"coverage": 1.0, "basis": "absolute_metric_mass"},
            "candidate": {"coverage": 1.0, "basis": "absolute_metric_mass"},
            "limiting_side": "equal",
        },
        min_samples=30,
        min_panels=3,
        min_panel_samples=6,
    )

    assert result.numeric_facts["lag_aligned_opportunity_count"] == panels * (
        periods - 1
    )
    assert result.numeric_facts["complete_pair_count"] == panels * (periods - 2)
    assert result.numeric_facts["residual_pearson"] > 0.999
    assert result.numeric_facts["residual_spearman"] > 0.999
    assert result.typed_payload["transformation"]["scope"] == "within_panel"
    assert result.typed_payload["lag_alignment"]["scope"] == "within_panel"
    assert result.typed_payload["lag_alignment"]["lag"] == 1
