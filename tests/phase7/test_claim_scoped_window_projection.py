from __future__ import annotations

import pytest

from bi_agent.capabilities.driver_decomposition import driver_decomposition
from bi_agent.capabilities.pattern_scan import scan_pattern
from bi_agent.runtime.langgraph_workflow import _comparison_rows_and_params


def _daily_row(window_id: str, amount: float, observation_key: str) -> dict:
    paid_users = amount / 10.0
    return {
        "window_id": window_id,
        "window_role": "target" if window_id == "target_day" else "baseline",
        "observation_key": observation_key,
        "period": "comparison",
        "group": "target" if window_id == "target_day" else "baseline",
        "amount": amount,
        "paid_amount": amount,
        "paid_users": paid_users,
        "paid_orders": paid_users * 2.0,
        "paid_frequency": 2.0,
        "avg_order_amount": 5.0,
        "first_paid_users": paid_users / 2.0,
    }


def _mixed_window_rows() -> tuple[dict, ...]:
    return (
        _daily_row("target_day", 120.0, "2026-06-01"),
        _daily_row("previous_day", 80.0, "2026-05-31"),
        _daily_row("rolling_7_day_baseline", 70.0, "2026-05-25"),
        _daily_row("rolling_7_day_baseline", 80.0, "2026-05-26"),
        _daily_row("rolling_7_day_baseline", 90.0, "2026-05-27"),
        _daily_row("rolling_7_day_baseline", 100.0, "2026-05-28"),
        _daily_row("rolling_7_day_baseline", 110.0, "2026-05-29"),
        _daily_row("rolling_7_day_baseline", 120.0, "2026-05-30"),
        _daily_row("rolling_7_day_baseline", 130.0, "2026-05-31"),
    )


def _state(rows: tuple[dict, ...]) -> dict:
    return {
        "request": {
            "run_mode": "fixture",
            "runtime_rows_by_intent": {
                "component_driver_scan": rows,
                "daily_metric_baselines": rows,
            },
            "compiler_runtime_plan": {
                "baselines": (
                    "previous_day",
                    "rolling_7_day_baseline",
                ),
                "capability_inputs": {
                    "driver_decomposition": {
                        "preferred_query_intents": ("component_driver_scan",),
                    },
                    "rolling_window_compare": {
                        "preferred_query_intents": ("daily_metric_baselines",),
                    },
                },
                "capability_params": {
                    "compare_periods": {"baselines": ("previous_day",)},
                    "rolling_window_compare": {
                        "baseline": "rolling_7_day_baseline",
                    },
                },
            },
        },
        "intent": {
            "pattern_family": "custom_baseline",
            "pattern_params": {
                "period_key": "period",
                "group_key": "group",
                "target_group": "target",
                "baseline_group": "baseline",
            },
        },
        "analysis_route": {
            "analysis_requirements": {
                "baselines": ["previous_day"],
                "claim_intents": [
                    "comparative_change",
                    "formula_component_contribution",
                    "baseline_stability",
                ],
            },
            "claim_intent_resolution": {
                "schema_version": "claim_intent_resolution.v1",
                "required_claim_intents": [
                    "comparative_change",
                    "formula_component_contribution",
                ],
                "auxiliary_claim_intents": ["baseline_stability"],
                "auto_routed_claim_intents": {
                    "baseline_stability": {
                        "capability_id": "rolling_window_compare",
                        "evidence_status": "queryable",
                        "publication_status": "evidence_required",
                        "auxiliary_baselines": ["rolling_7_day_baseline"],
                    }
                },
                "degraded_claim_intents": {},
                "primary_baselines": ["previous_day"],
                "auxiliary_baselines": ["rolling_7_day_baseline"],
            },
        },
    }


def _driver_amount_delta(rows: tuple[dict, ...]) -> float | None:
    state = _state(rows)
    projected_rows, params = _comparison_rows_and_params(
        state,
        "driver_decomposition",
        params=state["intent"]["pattern_params"],
        dimension_keys=(),
        period_key="period",
    )
    evidence = driver_decomposition(projected_rows, **params)
    decompositions = evidence.typed_payload["decompositions"]
    return decompositions[0]["amount_delta"] if decompositions else None


def test_primary_driver_projection_uses_previous_day_independent_of_row_order():
    rows = _mixed_window_rows()

    deltas = (
        _driver_amount_delta(rows),
        _driver_amount_delta(tuple(reversed(rows))),
    )

    assert deltas == pytest.approx((40.0, 40.0))


def test_auxiliary_rolling_projection_uses_only_the_seven_day_aggregate():
    rows = _mixed_window_rows()
    state = _state(rows)

    projected_rows, params = _comparison_rows_and_params(
        state,
        "rolling_window_compare",
        params=state["intent"]["pattern_params"],
        dimension_keys=(),
        period_key="period",
    )
    evidence = scan_pattern(
        projected_rows,
        pattern_family="custom_baseline",
        min_periods=1,
        **params,
    )

    assert evidence.comparable_periods == 1
    assert evidence.median_uplift == pytest.approx(0.20)


def test_missing_primary_window_degrades_only_driver_instead_of_borrowing_auxiliary():
    rows = tuple(
        row for row in _mixed_window_rows() if row["window_id"] != "previous_day"
    )
    state = _state(rows)

    driver_rows, driver_params = _comparison_rows_and_params(
        state,
        "driver_decomposition",
        params=state["intent"]["pattern_params"],
        dimension_keys=(),
        period_key="period",
    )
    driver_evidence = driver_decomposition(driver_rows, **driver_params)
    rolling_rows, rolling_params = _comparison_rows_and_params(
        state,
        "rolling_window_compare",
        params=state["intent"]["pattern_params"],
        dimension_keys=(),
        period_key="period",
    )
    rolling_evidence = scan_pattern(
        rolling_rows,
        pattern_family="custom_baseline",
        min_periods=1,
        **rolling_params,
    )

    assert driver_evidence.typed_payload["decompositions"] == ()
    assert driver_evidence.limitations == ("driver_components_missing",)
    assert rolling_evidence.comparable_periods == 1
    assert rolling_evidence.median_uplift == pytest.approx(0.20)


def test_rolling_factor_projection_derives_ratios_from_aggregated_primitives():
    rows = list(_mixed_window_rows())
    rolling = [
        row for row in rows if row["window_id"] == "rolling_7_day_baseline"
    ]
    for index, row in enumerate(rolling, start=1):
        row["paid_users"] = float(index)
        row["paid_orders"] = float(index * index)
        row["paid_frequency"] = float(index)
        row["paid_amount"] = float(index * index * (index + 1))
        row["amount"] = row["paid_amount"]
        row["avg_order_amount"] = float(index + 1)
    state = _state(tuple(rows))

    projected_rows, _ = _comparison_rows_and_params(
        state,
        "rolling_window_compare",
        params=state["intent"]["pattern_params"],
        dimension_keys=(),
        period_key="period",
    )
    baseline = next(row for row in projected_rows if row["group"] == "baseline")

    mean_users = sum(range(1, 8)) / 7
    mean_orders = sum(index * index for index in range(1, 8)) / 7
    mean_amount = sum(
        index * index * (index + 1) for index in range(1, 8)
    ) / 7
    assert baseline["paid_frequency"] == pytest.approx(
        mean_orders / mean_users
    )
    assert baseline["avg_order_amount"] == pytest.approx(
        mean_amount / mean_orders
    )
