from datetime import date, timedelta
from math import exp, sin
from random import Random

import pytest

from bi_agent.capabilities.cross_source_association import (
    _adjust_p_values,
    cross_source_association,
)


def _lagged_rows(size: int = 240):
    random = Random(7123)
    driver = [random.gauss(0.0, 1.0) + 0.25 * sin(index / 8) for index in range(size)]
    rows = []
    for index in range(size):
        target = (
            2.4 * driver[index - 2] + random.gauss(0.0, 0.22)
            if index >= 2
            else random.gauss(0.0, 1.0)
        )
        rows.append(
            {
                "business_date": date(2025, 1, 1) + timedelta(days=index),
                "paid_amount": target,
                "gameplay_bet_amount": driver[index],
                "unrelated_metric": random.gauss(0.0, 1.0),
            }
        )
    return rows


def test_lag_scan_finds_stable_leading_association_and_caps_claim_strength():
    result = cross_source_association(
        _lagged_rows(),
        target_key="paid_amount",
        candidate_keys=("gameplay_bet_amount", "unrelated_metric"),
        methods=("pearson", "spearman"),
        transforms=("level",),
        lags=(0, 1, 2, 3, 4),
        min_samples=60,
        rolling_window=60,
        rolling_step=30,
        fdr_method="by",
    )

    best = result.typed_payload["best_association"]
    assert result.evidence_type == "statistical_association"
    assert result.strength == "low"
    assert result.wording_limit == "candidate_association"
    assert best["candidate_key"] == "gameplay_bet_amount"
    assert best["lag"] == 2
    assert best["coefficient"] > 0.98
    assert best["q_value"] < 0.05
    assert best["rolling"]["stable"] is True
    assert result.typed_payload["lag_semantics"].startswith(
        "positive_lag_means_candidate_precedes_target"
    )
    assert result.typed_payload["causal_claim_allowed"] is False
    assert result.typed_payload["claim_ceiling"] == "stable_statistical_association"
    assert result.typed_payload["coefficient_is_contribution"] is False
    assert result.typed_payload["confounding_ruled_out"] is False
    assert "observational_association_only" in result.limitations
    assert (
        "stable_result_present_only_in_levels_and_may_reflect_trend"
        in result.limitations
    )


def test_signed_log_difference_keeps_negative_business_metrics_analyzable():
    rows = [
        {
            "business_date": index,
            "target": (-1 if index % 2 else 1) * (index + 10),
            "driver": (-1 if index % 2 else 1) * (2 * index + 20),
        }
        for index in range(120)
    ]

    result = cross_source_association(
        rows,
        target_key="target",
        candidate_keys=("driver",),
        transforms=("signed_log_difference",),
        min_samples=30,
        rolling_window=30,
        rolling_step=15,
    )

    assert result.typed_payload["transforms"] == ("signed_log_difference",)
    assert all(
        estimate["sample_size"] == 119
        for estimate in result.typed_payload["estimates"]
    )


def test_pearson_spearman_difference_and_log_difference_are_all_evaluated():
    random = Random(88)
    rows = []
    log_driver = 4.0
    log_target = 5.0
    for index in range(180):
        movement = 0.01 + 0.006 * sin(index / 5) + random.gauss(0.0, 0.001)
        log_driver += movement
        log_target += 1.8 * movement + random.gauss(0.0, 0.0008)
        rows.append(
            {
                "business_date": f"2025-{1 + index // 28:02d}-{1 + index % 28:02d}",
                "target": exp(log_target),
                "driver": exp(log_driver),
            }
        )

    result = cross_source_association(
        rows,
        target_key="target",
        candidate_keys=("driver",),
        methods=("pearson", "spearman"),
        transforms=("level", "daily_change", "log_change"),
        lags=(0,),
        min_samples=60,
        rolling_window=45,
        rolling_step=20,
        fdr_method="bh",
    )

    estimates = result.typed_payload["estimates"]
    assert result.typed_payload["transforms"] == (
        "level",
        "difference",
        "log_difference",
    )
    assert {(item["method"], item["transform"]) for item in estimates} == {
        (method, transform)
        for method in ("pearson", "spearman")
        for transform in ("level", "difference", "log_difference")
    }
    assert all(item["sample_size"] == 180 for item in estimates if item["transform"] == "level")
    assert all(item["sample_size"] == 179 for item in estimates if item["transform"] != "level")
    log_estimates = [item for item in estimates if item["transform"] == "log_difference"]
    assert all(item["coefficient"] > 0.95 for item in log_estimates)
    assert all(item["supported"] for item in log_estimates)
    assert "bh_assumes_independent_or_positive_dependence" in result.limitations


def test_minimum_sample_and_invalid_log_values_degrade_only_affected_tests():
    rows = [
        {
            "business_date": index,
            "target": float(index + 1),
            "positive": float((index + 1) ** 2),
            "contains_zero": 0.0 if index % 2 else float(index + 1),
        }
        for index in range(36)
    ]

    result = cross_source_association(
        rows,
        target_key="target",
        candidate_keys=("positive", "contains_zero"),
        transforms=("level", "log_difference"),
        methods=("spearman",),
        min_samples=30,
        rolling_window=12,
        fdr_method="by",
    )

    by_key_transform = {
        (item["candidate_key"], item["transform"]): item
        for item in result.typed_payload["estimates"]
    }
    assert by_key_transform[("positive", "level")]["supported"] is True
    assert by_key_transform[("positive", "log_difference")]["status"] == "ok"
    assert by_key_transform[("contains_zero", "level")]["status"] == "ok"
    assert (
        by_key_transform[("contains_zero", "log_difference")]["status"]
        == "insufficient_samples"
    )
    assert "some_hypotheses_below_minimum_samples" in result.limitations


def test_all_short_series_return_explicit_insufficient_evidence():
    result = cross_source_association(
        [
            {"business_date": index, "target": index * 2.0, "candidate": index * 3.0}
            for index in range(12)
        ],
        target_key="target",
        candidate_keys=("candidate",),
        min_samples=20,
        rolling_window=8,
    )

    assert result.evidence_type == "insufficient_evidence"
    assert result.strength == "insufficient"
    assert result.numeric_facts["tested_hypothesis_count"] == 0
    assert result.numeric_facts["supported_association_count"] == 0
    assert result.typed_payload["best_association"] is None
    assert all(
        item["status"] == "insufficient_samples"
        for item in result.typed_payload["estimates"]
    )


def test_by_adjustment_is_at_least_as_conservative_as_bh_under_dependency():
    p_values = (0.001, 0.01, 0.02, 0.20, 0.80)

    bh = _adjust_p_values(p_values, method="bh")
    by = _adjust_p_values(p_values, method="by")

    assert all(by_value >= bh_value for bh_value, by_value in zip(bh, by))
    assert bh == tuple(sorted(bh))
    assert by == tuple(sorted(by))
    assert bh[0] == pytest.approx(0.005)
    assert by[0] > bh[0]


def test_rows_require_unique_time_keys_and_valid_configuration():
    duplicate_rows = [
        {"business_date": "2026-01-01", "target": 1, "candidate": 2},
        {"business_date": "2026-01-01", "target": 2, "candidate": 3},
    ]
    with pytest.raises(ValueError, match="unique"):
        cross_source_association(
            duplicate_rows,
            target_key="target",
            candidate_keys=("candidate",),
        )

    with pytest.raises(ValueError, match="unsupported fdr_method"):
        cross_source_association(
            [{"business_date": 1, "target": 1, "candidate": 2}],
            target_key="target",
            candidate_keys=("candidate",),
            fdr_method="unknown",
        )
