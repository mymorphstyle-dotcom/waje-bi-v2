from __future__ import annotations

import json

import pytest

from bi_agent.capabilities.event_evidence import event_evidence
from bi_agent.capabilities.high_value_user_contribution import (
    high_value_user_contribution,
)
from bi_agent.capabilities.outlier_contribution import outlier_contribution
from bi_agent.capabilities.outlier_scan import outlier_scan


def _high_value(rows):
    return high_value_user_contribution(
        rows,
        threshold_policy={"type": "top_percentile", "value": 0.95},
        high_value_users_aggregation="window_distinct_count",
        group_key="window_role",
        total_amount_key="paid_amount",
        high_value_amount_key="high_value_amount",
        high_value_users_key="high_value_paid_users",
        threshold_key="high_value_threshold",
    )


def test_high_value_share_is_compared_per_window_without_cross_window_sum() -> None:
    evidence = _high_value(
        (
            {
                "window_role": "baseline",
                "paid_amount": 100.0,
                "high_value_amount": 40.0,
                "high_value_paid_users": 2,
                "high_value_threshold": 20.0,
            },
            {
                "window_role": "target",
                "paid_amount": 120.0,
                "high_value_amount": 72.0,
                "high_value_paid_users": 3,
                "high_value_threshold": 24.0,
            },
        )
    )

    payload = evidence.typed_payload
    assert payload["comparison"] == {
        "baseline_share": 0.4,
        "target_share": 0.6,
        "share_delta": pytest.approx(0.2),
    }
    assert [row["high_value_amount_share"] for row in payload["rows"]] == [
        0.4,
        0.6,
    ]
    assert "同一批稳定用户" in payload["claim_boundary"]
    assert "total_amount" not in payload


def test_high_value_daily_mean_allows_fractional_user_observation() -> None:
    evidence = high_value_user_contribution(
        (
            {
                "window_role": "baseline",
                "paid_amount": 100.0,
                "high_value_amount": 40.0,
                "high_value_paid_users": 2.25,
                "high_value_threshold": 20.0,
            },
        ),
        threshold_policy={"type": "top_percentile", "value": 0.95},
        high_value_users_aggregation="mean_per_complete_day",
        group_key="window_role",
        total_amount_key="paid_amount",
        high_value_amount_key="high_value_amount",
        high_value_users_key="high_value_paid_users",
        threshold_key="high_value_threshold",
    )

    assert evidence.typed_payload["rows"][0]["high_value_paid_users"] == 2.25
    assert (
        evidence.typed_payload["high_value_paid_users_measure"]
        == "average_users_per_complete_day"
    )


def test_high_value_duplicate_window_role_is_rejected() -> None:
    row = {
        "window_role": "target",
        "paid_amount": 120.0,
        "high_value_amount": 72.0,
        "high_value_paid_users": 3,
        "high_value_threshold": 24.0,
    }
    with pytest.raises(ValueError, match="high_value_group_duplicated:target"):
        _high_value((row, row))


def test_event_sentinel_does_not_create_candidate_mechanism() -> None:
    evidence = event_evidence(
        (
            {
                "event_id": "__no_event__:target_day",
                "event_count": 0,
                "window_role": "target",
            },
        ),
    )

    assert evidence.evidence_type == "insufficient_evidence"
    assert evidence.typed_payload["events"] == ()
    assert evidence.limitations == ("no_event_matches",)


def test_real_event_survives_alongside_no_event_sentinel() -> None:
    evidence = event_evidence(
        (
            {
                "event_id": "__no_event__:baseline_window",
                "event_count": 0,
                "window_role": "baseline",
            },
            {
                "event_id": "campaign-june",
                "event_count": 1,
                "window_role": "target",
                "event_type": "campaign",
                "payload": '{"business_use":"candidate_context","raw_owner":"secret"}',
            },
        ),
    )

    assert evidence.evidence_type == "candidate_mechanism"
    assert tuple(item["event_id"] for item in evidence.typed_payload["events"]) == (
        "campaign-june",
    )
    assert "payload" not in evidence.typed_payload["events"][0]
    assert len(evidence.typed_payload["events"][0]["source_event_digest"]) == 64
    assert evidence.typed_payload["event_summary"] == (
        {
            "window_role": "target",
            "event_type": "campaign",
            "event_count": 1,
            "business_use": "candidate_context",
        },
    )
    assert "payload" not in evidence.typed_payload["event_summary"][0]
    assert "event_id" not in evidence.typed_payload["event_summary"][0]
    assert evidence.typed_payload["synthesis_contract"] == {
        "schema_version": "public-fact-projection.v1",
        "public_fact_paths": (
            "business_readout",
            "claim_boundary",
            "event_summary",
        ),
    }


def test_dense_event_rows_keep_content_identity_with_bounded_evidence() -> None:
    evidence = event_evidence(
        tuple(
            {
                "event_id": f"external-event-{index}",
                "event_count": 1,
                "window_role": "target",
                "event_type": "reviewed_context",
                "payload": json.dumps(
                    {
                        "business_use": "candidate_context",
                        "description": f"审阅事件 {index}",
                        "raw_source_material": "x" * 4096,
                    },
                    ensure_ascii=False,
                ),
            }
            for index in range(60)
        )
    )

    encoded = json.dumps(
        evidence.typed_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    assert len(encoded) < 64 * 1024
    assert len(evidence.typed_payload["events"]) == 60
    assert all("payload" not in item for item in evidence.typed_payload["events"])
    assert all("source_event_digest" in item for item in evidence.typed_payload["events"])


def test_invalid_no_event_sentinel_is_rejected() -> None:
    with pytest.raises(ValueError, match="event_evidence_sentinel_count_invalid"):
        event_evidence(
            ({"event_id": "__no_event__:target_day", "event_count": 1},),
        )


def test_event_evidence_requires_complete_temporal_identity() -> None:
    with pytest.raises(
        ValueError,
        match="event_evidence_temporal_identity_incomplete",
    ):
        event_evidence(
            ({"event_id": "campaign-june", "event_count": 1},),
            event_ref="campaign-june",
        )

    evidence = event_evidence(
        ({"event_id": "campaign-june", "event_count": 1},),
        event_ref="campaign-june",
        temporal_authority_ref="temporal-authority:campaign-june",
    )
    assert evidence.typed_payload["evidence_contract"] == "event-presence.v1"
    assert evidence.typed_payload["event_ref"] == "campaign-june"
    assert evidence.typed_payload["temporal_authority_ref"] == (
        "temporal-authority:campaign-june"
    )
    assert evidence.typed_payload["causal_interpretation_allowed"] is False
    assert "event_window_policy" not in evidence.typed_payload
    assert "low_risk_default" not in evidence.typed_payload


def test_outlier_scan_evaluates_target_days_against_preceding_reference_days() -> None:
    evidence = outlier_scan(
        (
            {
                "window_role": "baseline",
                "observation_key": "2026-05-01",
                "paid_amount": 1000.0,
            },
            {
                "window_role": "baseline",
                "observation_key": "2026-05-02",
                "paid_amount": 900.0,
            },
            *(
                {
                    "window_role": "reference",
                    "observation_key": f"2026-05-{day:02d}",
                    "paid_amount": 10.0,
                }
                for day in range(3, 10)
            ),
            {
                "window_role": "target",
                "observation_key": "2026-06-01",
                "paid_amount": 10.0,
            },
            {
                "window_role": "target",
                "observation_key": "2026-06-02",
                "paid_amount": 10.0,
            },
            {
                "window_role": "target",
                "observation_key": "2026-06-03",
                "paid_amount": 100.0,
            },
        ),
        value_key="paid_amount",
    )

    payload = evidence.typed_payload
    assert payload["target_period_count"] == 3
    assert payload["reference_period_count"] == 7
    assert payload["excluded_other_group_period_count"] == 2
    assert payload["reference_median"] == 10.0
    assert payload["outliers"] == (
        {
            "period": "2026-06-03",
            "target_amount": 100.0,
            "absolute_deviation": 90.0,
            "mad_multiple": None,
        },
    )
    assert payload["claim_ceiling"] == "anomaly_candidate"
    assert payload["causal_claim_allowed"] is False


def test_outlier_scan_without_target_daily_observations_is_insufficient() -> None:
    evidence = outlier_scan(
        tuple(
            {
                "window_role": "reference",
                "observation_key": f"2026-05-{day:02d}",
                "paid_amount": 100.0,
            }
            for day in range(1, 8)
        ),
        value_key="paid_amount",
    )

    assert evidence.evidence_type == "insufficient_evidence"
    assert evidence.typed_payload["target_period_count"] == 0
    assert evidence.limitations == ("target_daily_values_missing",)


def test_outlier_scan_with_target_but_short_reference_is_insufficient() -> None:
    evidence = outlier_scan(
        (
            {
                "window_role": "reference",
                "observation_key": "2026-05-01",
                "paid_amount": 100.0,
            },
            {
                "window_role": "target",
                "observation_key": "2026-06-01",
                "paid_amount": 150.0,
            },
        ),
        value_key="paid_amount",
    )

    assert evidence.evidence_type == "insufficient_evidence"
    assert evidence.typed_payload["target_period_count"] == 1
    assert evidence.typed_payload["reference_period_count"] == 1
    assert evidence.limitations == ("insufficient_reference_daily_values",)


def test_outlier_contribution_uses_baseline_daily_mean_for_unequal_windows() -> None:
    evidence = outlier_contribution(
        (
            {
                "window_role": "baseline",
                "observation_key": "2026-05-01",
                "paid_amount": 8.0,
            },
            {
                "window_role": "baseline",
                "observation_key": "2026-05-02",
                "paid_amount": 10.0,
            },
            {
                "window_role": "baseline",
                "observation_key": "2026-05-03",
                "paid_amount": 12.0,
            },
            {
                "window_role": "baseline",
                "observation_key": "2026-05-04",
                "paid_amount": 10.0,
            },
            {
                "window_role": "target",
                "observation_key": "2026-06-10",
                "paid_amount": 12.0,
            },
            {
                "window_role": "target",
                "observation_key": "2026-06-11",
                "paid_amount": 9.0,
            },
            {
                "window_role": "target",
                "observation_key": "2026-06-12",
                "paid_amount": 20.0,
            },
        ),
        period_key="observation_key",
        period_grain="day",
        group_key="window_role",
        amount_key="paid_amount",
        top_n=2,
        max_removed_periods=1,
    )

    payload = evidence.typed_payload
    assert evidence.evidence_type == "accounting_contribution"
    assert evidence.wording_limit == "contextual"
    assert payload["sensitivity_method"] == ("target_daily_minus_baseline_daily_mean")
    assert payload["target_period_count"] == 3
    assert payload["baseline_period_count"] == 4
    assert payload["baseline_daily_mean"] == 10.0
    assert payload["target_window_actual"] == 41.0
    assert payload["target_window_expected_at_baseline_daily_mean"] == 30.0
    assert payload["total_delta"] == 11.0
    assert "paired_periods" not in payload
    assert payload["top_positive_periods"] == (
        {
            "period": "2026-06-12",
            "target_amount": 20.0,
            "baseline_daily_mean": 10.0,
            "sensitivity_delta": 10.0,
        },
        {
            "period": "2026-06-10",
            "target_amount": 12.0,
            "baseline_daily_mean": 10.0,
            "sensitivity_delta": 2.0,
        },
    )
    assert payload["remaining_target_window_delta_after_top_positive"] == 1.0
    assert payload["direction_preserved_after_top_positive"] is True
    assert payload["claim_ceiling"] == "sensitivity_only"
    assert payload["causal_claim_allowed"] is False
    assert payload["stable_pattern_claim_allowed"] is False
    assert "不能证明异常是原因" in payload["claim_boundary"]


def test_outlier_contribution_requires_both_daily_window_samples() -> None:
    evidence = outlier_contribution(
        (
            {
                "window_role": "target",
                "observation_key": "2026-06-10",
                "paid_amount": 12.0,
            },
        ),
        period_key="observation_key",
        period_grain="day",
        group_key="window_role",
        amount_key="paid_amount",
    )

    assert evidence.evidence_type == "insufficient_evidence"
    assert evidence.typed_payload["target_period_count"] == 1
    assert evidence.typed_payload["baseline_period_count"] == 0
    assert evidence.limitations == ("missing_baseline_daily_values",)


def test_outlier_contribution_rejects_window_aggregate_grain() -> None:
    with pytest.raises(
        ValueError,
        match="^outlier_contribution_daily_grain_required$",
    ):
        outlier_contribution(
            (
                {"group": "target", "period": "comparison", "amount": 120},
                {"group": "baseline", "period": "comparison", "amount": 100},
            ),
            period_grain="resolved_window",
        )
