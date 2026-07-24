import pytest

from bi_agent.runtime.langgraph_workflow import (
    _normalize_provider_intent_binding,
    _single_authority_intent_payload,
)
from bi_agent.runtime.llm_prompts import build_prompt
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)
from bi_agent.runtime.temporal_comparison import (
    TemporalComparisonContractError,
    validate_comparison_spec,
)


def test_intent_provider_projection_keeps_business_coverage_without_execution_routing() -> (
    None
):
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)

    payload = _single_authority_intent_payload(
        question="2026年Q2相比Q1付费金额为什么变化？",
        registry=registry,
    )

    goals = {item["goal_id"]: item for item in payload["goal_catalog"]}
    axes = {item["axis_id"]: item for item in payload["analysis_axis_catalog"]}

    assert set(goals) == set(registry.analysis_goal_ids)
    assert set(axes) == set(registry.analysis_axis_ids)
    assert goals["explain_change"]["required_outcomes"] == [
        "direction_and_magnitude",
        "ranked_drivers",
        "quantified_contributions",
        "candidate_explanations",
        "evidence_boundaries",
    ]
    assert {
        item["axis_id"] for item in goals["explain_change"]["analysis_axes"]
    } >= {
        "change_validation",
        "formula_tree",
        "payment_outcome_health",
        "external_context_screen",
        "data_quality",
    }
    assert axes["formula_tree"]["metric_refs"] == [
        "paid_users",
        "paid_orders",
        "first_paid_users",
        "paid_frequency",
        "paid_amount_per_paid_user",
        "avg_order_amount",
    ]
    assert axes["external_context_screen"]["context_source_refs"] == [
        "external_event"
    ]

    assert all(
        "completion_policy" not in goal and "outcome_claim_types" not in goal
        for goal in goals.values()
    )
    assert all(
        {
            "capability_refs",
            "source_refs",
            "reconciliation_group",
            "selection_policy",
        }.isdisjoint(axis)
        for axis in axes.values()
    )


def test_intent_prompt_binds_diagnostic_metrics_as_factors_without_runtime_rewrite() -> (
    None
):
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    payload = _single_authority_intent_payload(
        question="比较目标业务结果，并报告组成、健康度和诊断指标。",
        registry=registry,
    )

    prompt = build_prompt("single_authority_intent", payload)
    instructions = prompt.messages[-1]["content"]

    assert "independently define the user's primary analytical target" in instructions
    assert "listed under target_metric_refs for every selected goal" in instructions
    assert "remains a factor even when the user asks for its value" in instructions
    assert "Never put one metric in both fields" in instructions
    assert "keyword table" in instructions


def test_intent_provider_singleton_enum_arrays_are_normalized_deterministically() -> (
    None
):
    normalized = _normalize_provider_intent_binding(
        {
            "time_spec": {
                "kind": ["date_range"],
                "start": "2026-04-01",
                "end": "2026-06-02",
            },
            "comparison_spec": {
                "kind": ["fixed_window"],
                "baseline_class": ["prior_period"],
                "baseline_start": "2026-01-01",
                "baseline_end": "2026-03-04",
                "aggregation": ["sum_of_complete_days"],
            },
        }
    )

    assert normalized["time_spec"]["kind"] == "date_range"
    assert normalized["comparison_spec"] == {
        "kind": "fixed_window",
        "baseline_class": "prior_period",
        "baseline_start": "2026-01-01",
        "baseline_end": "2026-03-04",
        "aggregation": "sum_of_complete_days",
    }


@pytest.mark.parametrize(
    "comparison_spec",
    (
        {
            "kind": ["fixed_window"],
            "baseline_class": "prior_period",
            "baseline_start": "2026-01-01",
            "baseline_end": "2026-03-04",
            "aggregation": "sum_of_complete_days",
        },
        {
            "kind": "fixed_window",
            "baseline_class": ["prior_period", "custom_control_window"],
            "baseline_start": "2026-01-01",
            "baseline_end": "2026-03-04",
            "aggregation": "sum_of_complete_days",
        },
        {
            "kind": "calendar_partition",
            "baseline_class": "prior_period",
            "period_grain": "year",
            "partition_field": "quarter_of_year",
            "target_members": [["Q2"]],
            "baseline_members": ["Q1"],
            "aggregation": "sum_of_complete_days",
        },
    ),
)
def test_intent_enum_catalog_copies_fail_as_typed_temporal_errors(
    comparison_spec,
) -> None:
    with pytest.raises(
        TemporalComparisonContractError,
        match="temporal_comparison_spec_invalid",
    ):
        validate_comparison_spec(
            comparison_spec,
            time_spec={
                "kind": "date_range",
                "start": "2026-04-01",
                "end": "2026-06-02",
            },
        )
