from copy import deepcopy

import pytest

from bi_agent.capabilities.candidate_dimension_screen import (
    candidate_dimension_screen,
)
from bi_agent.runtime.contracts import load_contract
from bi_agent.runtime.factor_coverage import (
    FactorCoverageContractError,
    FactorCoverageOutcome,
    FactorCoveragePlan,
    FactorCoveragePlanItem,
    FactorCoverageResult,
    InvestigationBranch,
    InvestigationSynthesis,
    narrative_factor_coverage_context,
    synthesize_factor_coverage,
)
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)


P4_FACTOR_DOMAINS = (
    "payment_order_metric_chain",
    "user_acquisition_and_first_payment",
    "amount_tier_and_user_value",
    "payment_channel_and_method",
    "marketing_channel_and_growth_ops",
    "gameplay_and_betting",
    "calendar_time_and_payday",
    "product_operation_events",
    "external_context_events",
    "data_quality_and_evidence",
)


def _registry_payload() -> dict:
    return load_contract(CANONICAL_RUNTIME_BINDINGS_PATH)


def test_p4_registry_exposes_reviewed_factor_domain_coverage() -> None:
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)

    assert registry.factor_domain_ids == P4_FACTOR_DOMAINS
    compiled = registry.compile_goal_factor_coverage(
        goal_bindings=[{"goal_id": "explain_change", "role": "primary"}],
        target_metric="paid_amount",
    )

    assert tuple(item["factor_domain_id"] for item in compiled) == P4_FACTOR_DOMAINS
    by_id = {item["factor_domain_id"]: item for item in compiled}
    assert by_id["user_acquisition_and_first_payment"]["axis_refs"] == [
        "acquisition_funnel"
    ]
    assert by_id["amount_tier_and_user_value"]["dimension_refs"] == [
        "amount_bucket"
    ]
    assert by_id["product_operation_events"]["dataset_refs"] == [
        "internal_operation_event"
    ]
    assert by_id["external_context_events"]["dataset_refs"] == [
        "external_event"
    ]


def test_explain_change_contract_schedules_breadth_axes_and_active_funnel() -> None:
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    goal_plan = registry.compile_goal_analysis_plan(
        goal_bindings=[{"goal_id": "explain_change", "role": "primary"}],
        target_metric="paid_amount",
        explicit_focus={
            "component_ids": [],
            "dimension_ids": [],
            "context_source_ids": [],
        },
    )

    assert goal_plan["schema_version"] == "analysis_goal_plan.v3"
    assert goal_plan["factor_domain_refs"] == list(P4_FACTOR_DOMAINS)
    by_axis = {item["axis_id"]: item for item in goal_plan["analysis_axes"]}
    assert by_axis["acquisition_funnel"]["metric_refs"] == [
        "new_users",
        "registrations",
        "registration_rate_new_base",
        "first_paid_users",
        "first_pay_rate_new_base",
        "same_day_new_paid_users",
        "same_day_new_paid_rate",
    ]
    assert by_axis["external_context_screen"]["context_source_refs"] == [
        "external_event"
    ]
    assert by_axis["internal_operation_context"]["context_source_refs"] == [
        "internal_operation_event"
    ]
    assert by_axis["external_context_screen"]["capability_refs"] == [
        "event_evidence",
        "event_window_compare",
    ]
    assert by_axis["internal_operation_context"]["capability_refs"] == [
        "internal_operation_event_evidence",
        "internal_operation_event_window_compare",
    ]
    assert "amount_bucket" in by_axis["dimension_localization"]["dimension_refs"]


def test_analysis_capability_execution_identity_belongs_to_one_axis() -> None:
    payload = _registry_payload()
    ownership = {
        capability_id: axis_id
        for axis_id, axis in payload["analysis_axis_catalog"].items()
        for capability_id in axis["capability_refs"]
    }

    assert sum(
        len(axis["capability_refs"])
        for axis in payload["analysis_axis_catalog"].values()
    ) == len(ownership)
    assert payload["capability_inputs"]["event_evidence"][
        "allowed_context_datasets"
    ] == ["external_event"]
    assert payload["capability_inputs"]["internal_operation_event_evidence"][
        "allowed_context_datasets"
    ] == ["internal_operation_event"]


def test_registry_rejects_capability_reused_across_analysis_axes() -> None:
    payload = _registry_payload()
    payload["analysis_axis_catalog"]["internal_operation_context"][
        "capability_refs"
    ][0] = "event_evidence"

    with pytest.raises(
        ValueError,
        match="runtime_analysis_capability_axis_duplicated:event_evidence",
    ):
        RuntimeContractRegistry(payload)


def test_current_stage_coverage_accepts_dashboard_and_external_events_through_june_2() -> None:
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)

    for dataset_id in (
        "market_dashboard",
        "market_dashboard_channel",
        "external_event",
    ):
        coverage = registry.dataset(dataset_id)["current_stage_coverage"]
        assert coverage["status"] == "accepted_complete_through"
        assert coverage["complete_through"] == "2026-06-02"
        assert coverage["later_window_policy"] == "window_data_unavailable"


def test_factor_domain_contract_rejects_unreviewed_runtime_refs() -> None:
    payload = _registry_payload()
    invalid = deepcopy(payload)
    invalid["factor_domains"]["external_context_events"]["dataset_refs"] = [
        "open_web_search"
    ]

    with pytest.raises(
        ValueError,
        match="runtime_factor_domain_reference_unknown:external_context_events:dataset_refs",
    ):
        RuntimeContractRegistry(invalid)


def test_amount_bucket_screen_reconciles_value_user_order_and_mix_structure() -> None:
    evidence = candidate_dimension_screen(
        rows_by_dimension={
            "amount_bucket": (
                {
                    "amount_bucket": "0-999",
                    "window_role": "baseline",
                    "paid_amount": 100,
                    "paid_orders": 10,
                    "paid_users": 5,
                    "sample_size": 10,
                },
                {
                    "amount_bucket": "0-999",
                    "window_role": "target",
                    "paid_amount": 120,
                    "paid_orders": 12,
                    "paid_users": 6,
                    "sample_size": 12,
                },
                {
                    "amount_bucket": "1000-4999",
                    "window_role": "baseline",
                    "paid_amount": 100,
                    "paid_orders": 5,
                    "paid_users": 2,
                    "sample_size": 5,
                },
                {
                    "amount_bucket": "1000-4999",
                    "window_role": "target",
                    "paid_amount": 180,
                    "paid_orders": 6,
                    "paid_users": 3,
                    "sample_size": 6,
                },
            )
        },
        overall_by_group={"baseline": 200, "target": 300},
        complete_dimensions=("amount_bucket",),
        dimension_labels={"amount_bucket": "充值档位"},
        dimension_metadata={
            "amount_bucket": {
                "business_name": "充值档位",
                "hierarchy_id": "amount_bucket",
                "hierarchy_level": "amount_bucket",
            }
        },
        group_key="window_role",
        amount_key="paid_amount",
        order_key="paid_orders",
        user_key="paid_users",
        min_sample_size=1,
    )

    profile = evidence.typed_payload["dimension_profiles"][0]
    members = {
        item["value"]: item
        for item in (*profile["top_lifts"], *profile["top_drags"])
    }
    assert profile["dimension"] == "amount_bucket"
    assert profile["reconciliation_status"] == "passed"
    assert sum(item["baseline_amount"] for item in members.values()) == 200
    assert sum(item["target_amount"] for item in members.values()) == 300
    assert members["0-999"]["baseline_paid_frequency"] == 2
    assert members["1000-4999"]["target_avg_order_amount"] == 30
    assert members["1000-4999"]["baseline_amount_share"] == 0.5
    assert members["1000-4999"]["target_amount_share"] == 0.6
    assert members["1000-4999"]["excess_delta"] == 30
    assert evidence.typed_payload["interpretation_contract"][
        "cross_dimension_contribution_ranking"
    ] == (
        "forbidden"
    )


def test_factor_coverage_records_are_content_addressed_and_strict() -> None:
    item = FactorCoveragePlanItem.create(
        factor_domain_id="external_context_events",
        business_name="外部环境与事件",
        role="required",
        axis_refs=("external_context_screen",),
        capability_refs=("event_evidence",),
        dataset_refs=("external_event",),
        dimension_refs=(),
        reconciliation_group="paid_amount_external_context",
        task_refs=("capability-task-external",),
        source_refs=("contracts/events/events.yaml#events",),
    )
    plan = FactorCoveragePlan.create(
        run_attempt_id="run-p4",
        intent_revision_id="intent-p4",
        plan_revision_id="plan-p4",
        authority_context_ref="authority-p4",
        runtime_contract_version="19",
        runtime_contract_digest="a" * 64,
        target_metric_ref="paid_amount",
        coverage_items=(item,),
    )
    outcome = FactorCoverageOutcome.create(
        coverage_plan_ref=plan.coverage_plan_ref,
        coverage_item_ref=item.coverage_item_ref,
        factor_domain_id=item.factor_domain_id,
        status="screened_no_signal",
        task_refs=item.task_refs,
        outcome_refs=("capability-outcome-external",),
        evidence_refs=("evidence-external",),
        limitation_refs=(),
        result_refs=("result-external",),
        retryability="never",
        summary_code="no_material_signal",
    )

    assert FactorCoveragePlan.from_dict(plan.to_dict()) == plan
    assert FactorCoverageOutcome.from_dict(outcome.to_dict()) == outcome
    result = FactorCoverageResult.create(
        plan=plan,
        execution_result_ref="authoritative-execution-result:p4",
        outcomes=(outcome,),
    )
    assert FactorCoverageResult.from_dict(result.to_dict(), plan=plan) == result
    branch = InvestigationBranch.create(
        item=item,
        snapshot_refs=("snapshot:p4",),
        release_refs=("release:p4",),
        stop_policy={
            "policy": "existing_capability_scheduler_budget",
            "publication_authority": "none",
            "thread_head_authority": "none",
            "query_access": "reviewed_capability_only",
        },
    )
    assert InvestigationBranch.from_dict(branch.to_dict(), item=item) == branch
    synthesis = synthesize_factor_coverage(
        plan=plan,
        coverage_result=result,
    )
    assert (
        InvestigationSynthesis.from_dict(
            synthesis.to_dict(),
            plan=plan,
            coverage_result=result,
        )
        == synthesis
    )
    assert "外部环境与事件" in narrative_factor_coverage_context(
        plan=plan,
        coverage_result=result,
        synthesis=synthesis,
    )
    tampered = outcome.to_dict()
    tampered["status"] = "analyzed"
    with pytest.raises(FactorCoverageContractError):
        FactorCoverageOutcome.from_dict(tampered)
