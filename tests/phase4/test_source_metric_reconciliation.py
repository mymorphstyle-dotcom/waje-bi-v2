from decimal import Decimal

import pytest

from bi_agent.runtime.source_metric_reconciliation import (
    SourceMetricReconciliationError,
    report_from_records,
)


def test_market_dashboard_mismatch_retains_paid_order_authority():
    report = report_from_records(
        (
            {
                "business_date": "2026-06-01",
                "authority_amount": "308240309",
                "comparison_amount": "308240309",
                "authority_users": 37764,
                "comparison_users": 37763,
            },
            {
                "business_date": "2026-06-02",
                "authority_amount": "338323281",
                "comparison_amount": "185469962",
                "authority_users": 39156,
                "comparison_users": 23976,
            },
        ),
        authority_dataset_id="paid_order_success",
        comparison_dataset_id="market_dashboard",
    )

    assert report.overall_status == "mismatch"
    assert report.authority_action == (
        "retain_authority_and_limit_comparison_source_to_context"
    )
    assert report.observations[0].amount_status == "matched"
    assert report.observations[0].user_status == "mismatch"
    assert report.observations[1].amount_difference == Decimal("-152853319")
    assert report.observations[1].comparison_claim_ceiling == "context_only"
    assert report.report_ref.startswith("source-metric-reconciliation:sha256:")


def test_fully_reconciled_source_is_observed():
    report = report_from_records(
        (
            {
                "business_date": "2026-06-01",
                "authority_amount": "10.00",
                "comparison_amount": "10.01",
                "authority_users": 2,
                "comparison_users": 2,
            },
        ),
        authority_dataset_id="authority",
        comparison_dataset_id="comparison",
        amount_tolerance=Decimal("0.01"),
    )
    assert report.overall_status == "matched"
    assert report.observations[0].comparison_claim_ceiling == "observed"


@pytest.mark.parametrize(
    "record",
    (
        {"business_date": "bad", "authority_amount": 1, "comparison_amount": 1, "authority_users": 1, "comparison_users": 1},
        {"business_date": "2026-06-01", "authority_amount": "nan", "comparison_amount": 1, "authority_users": 1, "comparison_users": 1},
        {"business_date": "2026-06-01", "authority_amount": 1, "comparison_amount": 1, "authority_users": "1.5", "comparison_users": 1},
    ),
)
def test_reconciliation_rejects_malformed_source_observations(record):
    with pytest.raises(SourceMetricReconciliationError):
        report_from_records(
            (record,),
            authority_dataset_id="authority",
            comparison_dataset_id="comparison",
        )
