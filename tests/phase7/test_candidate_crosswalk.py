from __future__ import annotations

import math

import pytest

from bi_agent.capabilities.candidate_crosswalk import candidate_crosswalk


RULES = (
    "unicode_casefold",
    "remove_non_alphanumeric",
    "strip_paid_source_prefix_pa",
)


def _strategies(*, left=None, right=None):
    return {
        "left": left or {"paid_amount": "sum"},
        "right": right or {"bet_amount": "sum"},
    }


def test_mechanical_rules_create_only_unique_one_to_one_pairs():
    result = candidate_crosswalk(
        [
            {"day": "2026-06-01", "channel": "PA_Waje H5", "paid_amount": 10},
            {"day": "2026-06-01", "channel": "PA-Alpha", "paid_amount": 20},
        ],
        [
            {"day": "2026-06-01", "channel": "wajeh5", "bet_amount": 30},
            {"day": "2026-06-01", "channel": "ALPHA", "bet_amount": 40},
        ],
        time_key="day",
        group_key="channel",
        metric_strategies=_strategies(),
        candidate_rules=RULES,
    )

    assert result["mapping_status"] == "candidate_unreviewed"
    assert result["mapping_summary"]["pair_count"] == 2
    assert result["mapping_summary"]["ambiguous_count"] == 0
    assert result["mapping_summary"]["unmatched_count"] == 0
    assert len(result["mapping_pairs"]) == 2
    assert result["aligned_rows"] == (
        {
            "day": "2026-06-01",
            "mapped_group": "alpha",
            "left_group": "PA-Alpha",
            "right_group": "ALPHA",
            "left_present": True,
            "right_present": True,
            "left_paid_amount": 20.0,
            "right_bet_amount": 40.0,
        },
        {
            "day": "2026-06-01",
            "mapped_group": "wajeh5",
            "left_group": "PA_Waje H5",
            "right_group": "wajeh5",
            "left_present": True,
            "right_present": True,
            "left_paid_amount": 10.0,
            "right_bet_amount": 30.0,
        },
    )


def test_default_rules_do_not_guess_a_prefix_mapping():
    result = candidate_crosswalk(
        [{"day": "d1", "channel": "PA-X", "paid_amount": 10}],
        [{"day": "d1", "channel": "x", "bet_amount": 20}],
        time_key="day",
        group_key="channel",
        metric_strategies=_strategies(),
    )

    assert result["candidate_rules"] == ()
    assert result["mapping_summary"]["pair_count"] == 0
    assert result["mapping_summary"]["unmatched_count"] == 2
    assert result["aligned_rows"] == ()


def test_normalization_ambiguity_is_excluded_from_mapping():
    result = candidate_crosswalk(
        [
            {"day": "d1", "channel": "PA-X", "paid_amount": 10},
            {"day": "d1", "channel": "PA_X", "paid_amount": 20},
        ],
        [{"day": "d1", "channel": "x", "bet_amount": 30}],
        time_key="day",
        group_key="channel",
        metric_strategies=_strategies(),
        candidate_rules=RULES,
    )

    summary = result["mapping_summary"]
    assert summary["pair_count"] == 0
    assert summary["ambiguous_count"] == 1
    assert summary["left_ambiguous_group_count"] == 2
    assert summary["left_unmatched_count"] == 2
    assert summary["right_unmatched_count"] == 1


def test_unmatched_and_empty_groups_are_counted_without_entering_panel():
    result = candidate_crosswalk(
        [
            {"day": "d1", "channel": "PA-A", "paid_amount": 10},
            {"day": "d1", "channel": "PA-LEFT", "paid_amount": 5},
            {"day": "d1", "channel": "---", "paid_amount": 2},
        ],
        [
            {"day": "d1", "channel": "a", "bet_amount": 20},
            {"day": "d1", "channel": "right", "bet_amount": 5},
            {"day": "d1", "channel": None, "bet_amount": 1},
        ],
        time_key="day",
        group_key="channel",
        metric_strategies=_strategies(),
        candidate_rules=RULES,
    )

    summary = result["mapping_summary"]
    assert summary["pair_count"] == 1
    assert summary["left_unmatched_count"] == 1
    assert summary["right_unmatched_count"] == 1
    assert summary["left_empty_group_row_count"] == 1
    assert summary["right_empty_group_row_count"] == 1
    assert len(result["aligned_rows"]) == 1


def test_time_alignment_retains_union_and_marks_incomplete_cells():
    result = candidate_crosswalk(
        [
            {"day": "d1", "channel": "PA-A", "paid_amount": 10},
            {"day": "d2", "channel": "PA-A", "paid_amount": 20},
        ],
        [
            {"day": "d2", "channel": "a", "bet_amount": 30},
            {"day": "d3", "channel": "a", "bet_amount": 40},
        ],
        time_key="day",
        group_key="channel",
        metric_strategies=_strategies(),
        candidate_rules=RULES,
    )

    rows = result["aligned_rows"]
    assert [row["day"] for row in rows] == ["d1", "d2", "d3"]
    assert rows[0]["left_paid_amount"] == 10.0
    assert rows[0]["right_bet_amount"] is None
    assert rows[1]["left_present"] is True
    assert rows[1]["right_present"] is True
    assert rows[2]["left_paid_amount"] is None
    assert rows[2]["right_bet_amount"] == 40.0
    assert result["mapping_summary"]["complete_aligned_cell_count"] == 1


def test_metric_coverage_uses_mass_for_additive_metrics_and_cells_for_ratios():
    result = candidate_crosswalk(
        [
            {
                "day": "d1",
                "channel": "PA-A",
                "paid_amount": 90,
                "success_rate": 0.9,
            },
            {
                "day": "d1",
                "channel": "PA-LEFT",
                "paid_amount": 10,
                "success_rate": 0.5,
            },
        ],
        [
            {"day": "d1", "channel": "a", "bet_amount": 80},
            {"day": "d1", "channel": "right", "bet_amount": 20},
        ],
        time_key="day",
        group_key="channel",
        metric_strategies=_strategies(
            left={"paid_amount": "sum", "success_rate": "ratio"}
        ),
        candidate_rules=RULES,
    )

    summary = result["mapping_summary"]
    assert math.isclose(summary["left_metric_coverage"]["paid_amount"], 0.9)
    assert math.isclose(summary["left_metric_coverage"]["success_rate"], 0.5)
    assert math.isclose(summary["right_metric_coverage"]["bet_amount"], 0.8)
    assert (
        summary["left_metric_coverage_detail"]["paid_amount"]["basis"]
        == "absolute_metric_mass"
    )
    assert (
        summary["left_metric_coverage_detail"]["success_rate"]["basis"]
        == "observed_metric_cells"
    )


def test_duplicate_additive_rows_sum_but_ratio_rows_are_rejected():
    left = [
        {"day": "d1", "channel": "PA-A", "paid_amount": 10, "rate": 0.5},
        {"day": "d1", "channel": "PA-A", "paid_amount": 15, "rate": 0.6},
    ]
    right = [{"day": "d1", "channel": "a", "bet_amount": 20}]

    summed = candidate_crosswalk(
        left,
        right,
        time_key="day",
        group_key="channel",
        metric_strategies=_strategies(),
        candidate_rules=RULES,
    )
    assert summed["aligned_rows"][0]["left_paid_amount"] == 25.0

    with pytest.raises(ValueError, match="ratio metrics require one"):
        candidate_crosswalk(
            left,
            right,
            time_key="day",
            group_key="channel",
            metric_strategies=_strategies(left={"paid_amount": "sum", "rate": "ratio"}),
            candidate_rules=RULES,
        )
