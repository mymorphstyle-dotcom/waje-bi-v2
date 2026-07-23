from __future__ import annotations

import pytest

from bi_agent.capabilities.payment_outcome_compare import (
    PaymentOutcomeCompareError,
    payment_outcome_compare,
)


def _rows(successful: int = 60, not_paid: int = 40, rate: float = 0.6):
    return {
        "payment_method": (
            {
                "payment_method": "OPAY",
                "window_id": "baseline",
                "window_role": "baseline",
                "terminal_payment_orders": 100,
                "successful_payment_orders": successful,
                "not_paid_payment_orders": not_paid,
                "payment_success_rate": rate,
            },
            {
                "payment_method": "OPAY",
                "window_id": "target",
                "window_role": "target",
                "terminal_payment_orders": 120,
                "successful_payment_orders": 78,
                "not_paid_payment_orders": 42,
                "payment_success_rate": 0.65,
            },
        )
    }


def _run(rows=None):
    return payment_outcome_compare(
        rows or _rows(),
        group_key="window_role",
        window_id_key="window_id",
        target_group="target",
        baseline_group="baseline",
        terminal_orders_key="terminal_payment_orders",
        successful_orders_key="successful_payment_orders",
        not_paid_orders_key="not_paid_payment_orders",
        success_rate_key="payment_success_rate",
        dimension_labels={
            "payment_method": "支付方式",
            "channel": "渠道",
        },
        result_refs=("result:payment-outcome",),
    )


def test_payment_outcome_compare_keeps_final_status_boundary() -> None:
    evidence = _run()

    assert evidence.evidence_type == "observed_comparison"
    assert evidence.typed_payload["claim_ceiling"] == "directional"
    assert evidence.typed_payload["failure_reason_claim_allowed"] is False
    assert evidence.typed_payload["causal_claim_allowed"] is False
    profile = evidence.typed_payload["profiles"][0]
    assert profile["dimension_id"] == "payment_method"
    assert profile["observations"][1]["success_rate"] == "0.65"
    assert evidence.typed_payload["window_totals"] == (
        {
            "window_id": "baseline",
            "window_role": "baseline",
            "terminal_orders": 100,
            "successful_orders": 60,
            "not_paid_as_of_snapshot_orders": 40,
            "success_rate": "0.6",
        },
        {
            "window_id": "target",
            "window_role": "target",
            "terminal_orders": 120,
            "successful_orders": 78,
            "not_paid_as_of_snapshot_orders": 42,
            "success_rate": "0.65",
        },
    )
    assert evidence.numeric_facts["baseline_terminal_payment_orders"] == 100
    assert evidence.numeric_facts["target_payment_success_rate"] == 0.65
    assert (
        evidence.numeric_facts[
            "dimension_payment_method_representative_member"
        ]
        == "OPAY"
    )
    assert (
        evidence.numeric_facts[
            "dimension_payment_method_baseline_payment_success_rate"
        ]
        == 0.6
    )
    assert (
        evidence.numeric_facts[
            "dimension_payment_method_target_terminal_payment_orders"
        ]
        == 120
    )
    summary = evidence.typed_payload["dimension_summaries"][0]
    assert summary["selection_policy"] == "largest_target_terminal_order_volume"
    assert summary["representative_member"] == "OPAY"
    assert evidence.typed_payload["interpretation_contract"][
        "process_inference_allowed"
    ] is False
    assert "payment_failure_reason_unavailable" in evidence.limitations


def test_payment_outcome_compare_fails_on_component_drift() -> None:
    with pytest.raises(
        PaymentOutcomeCompareError,
        match="payment_outcome_component_reconciliation_failed",
    ):
        _run(_rows(successful=59))


def test_payment_outcome_compare_fails_on_rate_drift() -> None:
    with pytest.raises(
        PaymentOutcomeCompareError,
        match="payment_outcome_rate_reconciliation_failed",
    ):
        _run(_rows(rate=0.7))


def test_payment_outcome_compare_reconciles_totals_across_dimensions() -> None:
    rows = _rows()
    rows["channel"] = tuple(
        {
            **row,
            "channel": "WajeSpecial",
        }
        for row in rows["payment_method"]
    )

    evidence = _run(rows)

    assert len(evidence.typed_payload["profiles"]) == 2
    assert len(evidence.typed_payload["dimension_summaries"]) == 2
    assert evidence.typed_payload["window_totals"][1][
        "terminal_orders"
    ] == 120


def test_payment_outcome_compare_rejects_cross_dimension_total_drift() -> None:
    rows = _rows()
    rows["channel"] = (
        {
            "channel": "WajeSpecial",
            "window_id": "baseline",
            "window_role": "baseline",
            "terminal_payment_orders": 100,
            "successful_payment_orders": 60,
            "not_paid_payment_orders": 40,
            "payment_success_rate": 0.6,
        },
        {
            "channel": "WajeSpecial",
            "window_id": "target",
            "window_role": "target",
            "terminal_payment_orders": 121,
            "successful_payment_orders": 79,
            "not_paid_payment_orders": 42,
            "payment_success_rate": 79 / 121,
        },
    )

    with pytest.raises(
        PaymentOutcomeCompareError,
        match="payment_outcome_dimension_total_reconciliation_failed",
    ):
        _run(rows)
