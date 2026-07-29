from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
import json
from types import SimpleNamespace
from typing import Sequence

import pytest

from bi_agent.capabilities import make_evidence_envelope
from bi_agent.runtime import capability_task_adapter
from bi_agent.runtime.analysis_contracts import (
    MetricBinding,
    QueryContract,
    ResolvedWindow,
    ResultShape,
    query_contract_signature,
)
from bi_agent.runtime.capability_authority import (
    CAPABILITY_EVIDENCE_OBSERVATION_BYTE_LIMIT,
    CapabilityAdapterOutput,
    CapabilityAttempt,
)
from bi_agent.runtime.capability_task_adapter import (
    CapabilityAdapterRegistration,
    CapabilityTaskAdapterContractError,
    CapabilityTaskAdapterRegistry,
    ExpectedCapabilityGap,
    TaskRuntimeInputs,
    TaskScopedCapabilityInput,
    builtin_capability_adapter_registry,
)
from bi_agent.runtime.evidence_authority import canonical_value
from bi_agent.runtime.plan_authority import (
    AnalysisAxis,
    ClaimObligation,
    EvidenceRequirement,
    PlanRevision,
)
from tests.support.temporal_authority import resolved_test_temporal_authority


CASE_B_CAPABILITY_IDS = (
    "compare_periods",
    "formula_decompose",
    "candidate_dimension_screen",
    "segment_contribution",
    "segment_breakdown",
    "segment_shift_compare",
    "joint_attribution",
    "user_mix_contribution",
    "high_value_user_contribution",
    "metric_timeseries",
    "rolling_window_compare",
    "compare_period_phases",
    "weekday_calendar_compare",
    "event_evidence",
    "event_window_compare",
    "data_quality_profile",
)


def _plan(
    capability_ids: Sequence[str],
    *,
    seed: str = "one",
    event_relative: bool = False,
) -> PlanRevision:
    obligation = ClaimObligation.create(
        claim_kind="formula_component_contribution",
        role="user_required",
        subject={
            "target_metric_ref": "metric:paid_amount",
            "scope": {"scope_type": "full_sample", "filters": []},
            "outcome_refs": (f"outcome:{seed}",),
            "goal_refs": ("explain_change",),
        },
        evidence_requirement=EvidenceRequirement.create(
            operator="any_of",
            evidence_kinds=("derived",),
        ),
        success_policy={
            "policy": "verified_or_explicit_boundary",
            "minimum_claim_strength": "quantified_contribution",
        },
    )
    axis = AnalysisAxis.create(
        axis_id=f"adapter_axis_{seed}",
        role="required",
        axis_kind="formula_tree",
        target_metric_refs=("paid_amount",),
        metric_refs=(
            "paid_amount",
            "paid_users",
            "paid_frequency",
            "avg_order_amount",
        ),
        dimension_refs=("region", "device_brand"),
        context_source_refs=(),
        capability_refs=tuple(dict.fromkeys(capability_ids)),
        reconciliation_group="paid_amount",
        selection_policy="all_contract_backed_members",
        source_refs=("contract:adapter-test",),
        goal_refs=("explain_change",),
        supports_obligation_ids=(obligation.obligation_id,),
    )
    temporal_authority = resolved_test_temporal_authority(
        time_spec={"kind": "date", "target": "2026-06-19"},
        comparison_spec=(
            {
                "kind": "event_relative_window",
                "event_ref": "business-event:campaign-june-2026",
                "target_start": "2026-06-19",
                "target_end": "2026-06-19",
                "baseline_start": "2026-06-18",
                "baseline_end": "2026-06-18",
                "aggregation": "sum_of_complete_days",
            }
            if event_relative
            else {
                "kind": "fixed_window",
                "baseline_class": "prior_period",
                "baseline_start": "2026-06-18",
                "baseline_end": "2026-06-18",
                "aggregation": "sum_of_complete_days",
            }
        ),
        require_physical_baseline=True,
    )
    return PlanRevision.create(
        run_attempt_id=f"adapter-run-{seed}",
        supersedes_plan_revision_id=None,
        intent_revision_id=f"intent-revision-{seed}",
        decision_refs=(f"decision:{seed}",),
        authority_context_ref=f"authority-context:{seed}",
        planner_proposal_ref=f"planner-proposal:{seed}",
        proposal_admission_ref=f"proposal-admission:{seed}",
        temporal_authority=temporal_authority,
        resolved_window_refs=temporal_authority.resolved_window_refs,
        context_window_specs=(),
        claim_obligations=(obligation,),
        analysis_axes=(axis,),
        capability_task_specs=tuple(
            {
                "task_key": f"{capability_id}:{index}",
                "capability_id": capability_id,
                "normalized_input_refs": (
                    f"authority-context:{seed}",
                    *temporal_authority.resolved_window_refs,
                    "metric:paid_amount",
                ),
                "dependency_task_keys": (),
                "obligation_edges": (
                    {"obligation_id": obligation.obligation_id, "required": True},
                ),
                "execution_rank": index + 1,
                "declared_budget_units": 1,
                "governor_inputs": {
                    "expected_information_gain": "obligation_closing",
                    "materiality": "user_required",
                    "actionability": "decision_supporting",
                    "statistical_risk": "contract_bounded",
                },
                "execution_policy": {
                    "degradation_policy": {
                        "missing_required_input": "block_claim",
                        "missing_optional_input": "record_limitation",
                    },
                    "integrity_failure": "fail_closed",
                    "input_states": (),
                },
            }
            for index, capability_id in enumerate(capability_ids)
        ),
        assumption_refs=(),
        budget_policy_ref="budget-policy:adapter-test",
        contract_versions={"runtime": "phase03.v1", "factor": "factor.v1"},
    )


def _bound(
    plan: PlanRevision,
    *,
    task_index: int = 0,
    payload: dict | None = None,
    expected_gap: ExpectedCapabilityGap | None = None,
    maximum_claim_strength: str = "descriptive",
    supported_evidence_types: tuple[str, ...] | None = None,
    supported_claim_types: tuple[str, ...] | None = None,
) -> TaskScopedCapabilityInput:
    task = plan.capability_tasks[task_index]
    return TaskScopedCapabilityInput.create(
        plan_revision_id=plan.plan_revision_id,
        task_id=task.task_id,
        authority_context_ref=plan.authority_context_ref,
        binding_record_ref=f"binding:{task.task_id}",
        data_contract_state="complete",
        maximum_claim_strength=maximum_claim_strength,
        scope_ref="scope:all_players",
        payload=payload or {},
        result_refs=(f"result:{task.task_id}",),
        completeness_report_refs=(f"completeness:{task.task_id}",),
        limitation_refs=(),
        expected_gap=expected_gap,
        services=(
            {
                "bound_capability_input": SimpleNamespace(
                    maximum_claim_strength=maximum_claim_strength,
                    supported_evidence_types=supported_evidence_types,
                    supported_claim_types=(
                        supported_claim_types
                        if supported_claim_types is not None
                        else tuple(
                            dict.fromkeys(
                                obligation.claim_kind
                                for obligation in plan.claim_obligations
                                if obligation.obligation_id
                                in set(task.supports_obligation_ids)
                            )
                        )
                    ),
                )
            }
            if supported_evidence_types is not None
            else {}
        ),
    )


def _runtime(*inputs: TaskScopedCapabilityInput) -> TaskRuntimeInputs:
    return TaskRuntimeInputs.create(inputs)


def _event_metric_contract(plan: PlanRevision) -> QueryContract:
    windows = tuple(
        ResolvedWindow(
            window_id=("target_day" if window.role == "target" else "baseline_window"),
            role=window.role,
            label=window.window_ref,
            start_inclusive=str(window.start),
            end_exclusive=(
                date.fromisoformat(str(window.end)) + timedelta(days=1)
            ).isoformat(),
            timezone="Africa/Lagos",
            aggregation=str(window.aggregation),
            required_complete_days=1,
            source_watermark_requirement=str(window.end),
        )
        for window in (
            plan.temporal_authority.target_window,
            plan.temporal_authority.baseline_window,
        )
        if window is not None
    )
    contract = QueryContract(
        query_contract_id="query:event-window:paid-amount",
        analysis_contract_ref="analysis:event-window",
        query_intent="daily_metric_baselines",
        dataset_snapshot_refs=("snapshot:paid:r1",),
        metric_bindings=(
            MetricBinding(
                metric_id="paid_amount",
                contract_ref="metric:paid-amount@1",
                dataset_id="paid_order_success",
                expression="paid_amount_ngn",
                aggregation="sum",
                required_fields=("paid_amount_ngn",),
                grain=("business_date_lagos",),
            ),
        ),
        dimension_bindings=(),
        window_refs=tuple(window.window_id for window in windows),
        resolved_windows=windows,
        filters=(),
        result_shape=ResultShape(
            required_fields=(
                "window_id",
                "window_role",
                "observation_key",
                "paid_amount",
            ),
            unique_key=("window_id", "observation_key"),
            grain=("window_id", "observation_key"),
            required_window_ids=tuple(window.window_id for window in windows),
        ),
        completeness_assertions=(),
        workload_class="interactive_aggregate",
        contract_signature="",
    )
    return replace(
        contract,
        contract_signature=query_contract_signature(contract),
    )


def test_bind_fails_before_dispatch_when_a_plan_capability_has_no_adapter() -> None:
    plan = _plan(("unknown_capability",))
    registry = builtin_capability_adapter_registry()

    with pytest.raises(
        CapabilityTaskAdapterContractError,
        match="^adapter_missing:unknown_capability$",
    ):
        registry.bind(plan, _runtime(_bound(plan)))


def test_builtin_registry_covers_every_capability_in_the_active_case_b_plan() -> None:
    plan = _plan(CASE_B_CAPABILITY_IDS, seed="case-b")

    builtin_capability_adapter_registry().validate_plan(plan)


def test_joint_attribution_adapter_consumes_each_dynamic_query_at_its_own_grain() -> None:
    plan = _plan(("joint_attribution",), seed="joint-query-groups")
    task = plan.capability_tasks[0]
    analyses = (
        {
            "query_contract_ref": "query:joint:channel-region",
            "dimension_keys": ("channel", "region"),
            "rows": (
                {
                    "channel": "organic",
                    "region": "lagos",
                    "window_role": "target",
                    "paid_amount": 140.0,
                    "paid_orders": 20,
                },
                {
                    "channel": "organic",
                    "region": "lagos",
                    "window_role": "baseline",
                    "paid_amount": 100.0,
                    "paid_orders": 20,
                },
            ),
        },
        {
            "query_contract_ref": "query:joint:payment-device",
            "dimension_keys": ("payment_method", "device_brand"),
            "rows": (
                {
                    "payment_method": "opay",
                    "device_brand": "tecno",
                    "window_role": "target",
                    "paid_amount": 80.0,
                    "paid_orders": 20,
                },
                {
                    "payment_method": "opay",
                    "device_brand": "tecno",
                    "window_role": "baseline",
                    "paid_amount": 100.0,
                    "paid_orders": 20,
                },
            ),
        },
    )
    scoped = _bound(
        plan,
        payload={
            "analyses": analyses,
            "group_key": "window_role",
            "target_group": "target",
            "baseline_group": "baseline",
            "amount_key": "paid_amount",
            "residual": 0.0,
            "fit": 1.0,
            "force_run": True,
            "missing_group_policy": "additive_zero",
        },
        maximum_claim_strength="candidate_driver",
        supported_evidence_types=("accounting_contribution",),
        supported_claim_types=("formula_component_contribution",),
    )
    execute = builtin_capability_adapter_registry().bind(
        plan,
        _runtime(scoped),
    )

    output = execute(task, CapabilityAttempt.create(plan, task))

    assert output.status == "succeeded"
    assert output.output_payload["evidence_type"] == "accounting_contribution"
    typed_payload = output.output_payload["typed_payload"]
    assert typed_payload["analysis_count"] == 2
    assert typed_payload["supported_analysis_count"] == 2
    assert tuple(
        item["dimension_keys"] for item in typed_payload["analyses"]
    ) == (
        ("channel", "region"),
        ("payment_method", "device_brand"),
    )


def test_market_health_adapter_publishes_every_bound_metric_comparison() -> None:
    plan = _plan(("market_health_compare",), seed="market-health")
    task = plan.capability_tasks[0]
    base_contract = _event_metric_contract(plan)
    metric_ids = (
        "active_users",
        "new_users",
        "registrations",
        "aggregate_marketing_cost",
        "profit",
    )
    market_metrics = tuple(
        MetricBinding(
            metric_id=metric_id,
            contract_ref=f"contract:market:{metric_id}",
            dataset_id="market_dashboard",
            expression=f"sum({metric_id})",
            aggregation="sum",
            required_fields=(metric_id,),
            grain=("window_id",),
            claim_types=("comparative_change",),
        )
        for metric_id in metric_ids
    )
    market_contract = replace(
        base_contract,
        metric_bindings=market_metrics,
        result_shape=replace(
            base_contract.result_shape,
            required_fields=(
                "window_id",
                "window_role",
                "observation_key",
                *metric_ids,
            ),
        ),
        contract_signature="",
    )
    market_contract = replace(
        market_contract,
        contract_signature=query_contract_signature(market_contract),
    )
    rows = tuple(
        {
            "window_id": window.window_id,
            "window_role": window.role,
            "observation_key": window.start_inclusive,
            **{
                metric_id: float(
                    (metric_index + 1) * (10 if window.role == "target" else 5)
                )
                for metric_index, metric_id in enumerate(metric_ids)
            },
        }
        for window in market_contract.resolved_windows
    )
    execute = builtin_capability_adapter_registry().bind(
        plan,
        _runtime(
            _bound(
                plan,
                payload={
                    "contract": market_contract,
                    "rows": rows,
                    "metric_ids": metric_ids,
                    "primary_baseline_window_id": next(
                        window.window_id
                        for window in market_contract.resolved_windows
                        if window.role == "baseline"
                    ),
                },
                maximum_claim_strength="directional",
                supported_evidence_types=("observed_comparison",),
                supported_claim_types=("comparative_change",),
            )
        ),
    )

    output = execute(task, CapabilityAttempt.create(plan, task))

    assert output.status == "succeeded"
    typed_payload = output.output_payload["typed_payload"]
    assert typed_payload["metric_ids"] == metric_ids
    assert tuple(
        item["metric"] for item in typed_payload["comparisons"]
    ) == metric_ids
    assert set(output.output_payload["numeric_facts"]) == {
        key
        for metric_id in metric_ids
        for key in (
            f"{metric_id}_target_value",
            f"{metric_id}_baseline_value",
        )
    }


def test_registry_rejects_duplicate_fixed_capability_mappings() -> None:
    def adapter(_plan, _task, _attempt, _runtime_input):
        raise AssertionError("must not run")

    registration = CapabilityAdapterRegistration(
        capability_id="compare_periods",
        adapter=adapter,
    )
    with pytest.raises(
        CapabilityTaskAdapterContractError,
        match="^adapter_duplicated:compare_periods$",
    ):
        CapabilityTaskAdapterRegistry((registration, registration))


def test_bound_adapter_accepts_only_the_exact_task_from_the_active_plan() -> None:
    plan = _plan(("formula_decompose",), seed="active")
    stale = _plan(("formula_decompose",), seed="stale")
    adapter = builtin_capability_adapter_registry().bind(
        plan,
        _runtime(_bound(plan, payload=_formula_payload())),
    )
    stale_task = stale.capability_tasks[0]

    with pytest.raises(
        CapabilityTaskAdapterContractError,
        match="^adapter_task_not_in_active_plan$",
    ):
        adapter(stale_task, CapabilityAttempt.create(stale, stale_task))


def test_typed_expected_gap_returns_unavailable_without_calling_primitive() -> None:
    plan = _plan(("test_capability",))
    calls: list[str] = []

    def adapter(_plan, task, _attempt, _runtime_input):
        calls.append(task.task_id)
        raise AssertionError("expected gaps must settle before primitive execution")

    registry = CapabilityTaskAdapterRegistry(
        (
            CapabilityAdapterRegistration(
                capability_id="test_capability",
                adapter=adapter,
            ),
        )
    )
    gap = ExpectedCapabilityGap.create(
        gap_type="missing_contract",
        limitation_ref="limitation:payment-attempt-contract-missing",
        data_contract_state="missing_contract",
        business_boundary="payment_success_evidence_unavailable",
        retryability="replan_required",
    )
    task = plan.capability_tasks[0]
    execute = registry.bind(plan, _runtime(_bound(plan, expected_gap=gap)))

    output = execute(task, CapabilityAttempt.create(plan, task))

    assert isinstance(output, CapabilityAdapterOutput)
    assert output.status == "unavailable"
    assert output.output_payload["expected_gap"]["gap_type"] == "missing_contract"
    assert output.affected_obligation_ids == task.supports_obligation_ids
    assert output.failure is None
    assert calls == []


def test_adapter_process_and_integrity_exceptions_propagate_unchanged() -> None:
    plan = _plan(("test_capability",))
    sentinel = RuntimeError("query_transport_failed:clickhouse")

    def adapter(_plan, _task, _attempt, _runtime_input):
        raise sentinel

    registry = CapabilityTaskAdapterRegistry(
        (
            CapabilityAdapterRegistration(
                capability_id="test_capability",
                adapter=adapter,
            ),
        )
    )
    task = plan.capability_tasks[0]
    execute = registry.bind(plan, _runtime(_bound(plan)))

    with pytest.raises(RuntimeError) as captured:
        execute(task, CapabilityAttempt.create(plan, task))

    assert captured.value is sentinel


def test_existing_primitive_is_wrapped_into_one_typed_adapter_output() -> None:
    plan = _plan(("data_quality_profile",))
    task = plan.capability_tasks[0]
    execute = builtin_capability_adapter_registry().bind(
        plan,
        _runtime(
            _bound(
                plan,
                payload={
                    "rows": (
                        {"paid_amount": 12.0, "source_row_count": 4},
                        {"paid_amount": 9.0, "source_row_count": 3},
                    ),
                    "required_fields": ("paid_amount",),
                },
            )
        ),
    )

    output = execute(task, CapabilityAttempt.create(plan, task))

    assert output.status == "succeeded"
    typed_payload = output.output_payload["typed_payload"]
    assert "row_count" not in typed_payload
    assert typed_payload["result_group_count"] == 2
    assert typed_payload["result_group_unit"] == "window_aggregate"
    assert typed_payload["source_coverage_count"] == 7
    assert typed_payload["source_coverage_unit"] == "window_scoped_source_record"
    assert len(output.evidence) == 1
    assert output.evidence[0].execution_state == "available"
    assert output.evidence[0].binding_record_ref == f"binding:{task.task_id}"
    assert output.evidence[0].result_refs == (f"result:{task.task_id}",)
    assert output.evidence[0].observation_facts == (
        output.output_payload["typed_payload"],
    )


def test_pattern_adapter_carries_aggregate_statistics_into_claim_evidence() -> None:
    plan = _plan(("rolling_window_compare",), seed="pattern-statistics")
    task = plan.capability_tasks[0]
    payload = {
        "rows": (
            {
                "observed_on": "2026-06-18",
                "window_role": "baseline",
                "value": 100.0,
            },
            {
                "observed_on": "2026-06-19",
                "window_role": "target",
                "value": 125.0,
            },
        ),
        "materiality_floor": 0.0,
        "min_periods": 1,
        "rolling_span_days": 1,
        "rolling_step_days": 1,
        "observation_key": "observed_on",
        "window_role_key": "window_role",
        "target_role": "target",
        "baseline_role": "baseline",
        "value_key": "value",
    }
    execute = builtin_capability_adapter_registry().bind(
        plan,
        _runtime(
            _bound(
                plan,
                payload=payload,
                maximum_claim_strength="directional",
                supported_evidence_types=("statistical_association",),
            )
        ),
    )

    output = execute(task, CapabilityAttempt.create(plan, task))

    assert output.status == "succeeded"
    assert output.output_payload["numeric_facts"] == {
        "materiality_floor": 0.0,
        "direction_ratio": 1.0,
        "direction_consistency_ratio": 1.0,
        "materiality_hit_ratio": 1.0,
        "median_uplift": 0.25,
        "comparable_periods": 1,
        "min_periods": 1,
    }
    evidence_facts = output.evidence[0].observation_facts
    interpretation = next(
        item["interpretation_contract"]
        for item in evidence_facts
        if "interpretation_contract" in item
    )
    assert interpretation["contract_id"] == (
        "pattern-scan-interpretation.v1"
    )
    summary = output.output_payload["typed_payload"]["period_direction_summary"]
    assert summary["dominant_direction"] == "positive"
    assert summary["positive_period_count"] == 1
    assert summary["exception_period_count"] == 0
    assert any(
        item.get("period_direction_summary") == summary
        for item in evidence_facts
    )
    assert {
        item["name"]: item["value"] for item in evidence_facts if "name" in item
    } == (
        output.output_payload["numeric_facts"]
    )


def test_data_quality_adapter_rejects_partial_source_coverage_counts() -> None:
    plan = _plan(("data_quality_profile",))
    task = plan.capability_tasks[0]
    execute = builtin_capability_adapter_registry().bind(
        plan,
        _runtime(
            _bound(
                plan,
                payload={
                    "rows": (
                        {"paid_amount": 12.0, "source_row_count": 4},
                        {"paid_amount": 9.0},
                    ),
                    "required_fields": ("paid_amount",),
                },
            )
        ),
    )

    with pytest.raises(
        ValueError,
        match="data_quality_source_coverage_count_invalid",
    ):
        execute(task, CapabilityAttempt.create(plan, task))


@pytest.mark.parametrize(
    (
        "capability_id",
        "payload",
        "claim_ceiling",
        "evidence_type",
        "evidence_kind",
    ),
    (
        (
            "change_point_scan",
            {
                "rows": tuple(
                    {
                        "observation_key": f"2026-01-{index + 1:02d}",
                        "paid_amount": value,
                    }
                    for index, value in enumerate((10, 10, 10, 10, 20, 20, 20, 20))
                ),
                "time_key": "observation_key",
                "value_key": "paid_amount",
                "min_total_samples": 8,
                "min_segment_samples": 4,
                "min_relative_level_shift": 0.2,
                "min_standardized_level_shift": 2.0,
                "max_candidates": 5,
            },
            "anomaly_candidate",
            "statistical_association",
            "statistical_association",
        ),
        (
            "metric_coverage_profile",
            {
                "rows": (
                    {
                        "result_ref": "result:coverage",
                        "window_id": "target",
                        "observation_key": "2026-01-02",
                        "paid_amount": 10,
                        "source_row_count": 3,
                    },
                ),
                "metric_id": "paid_amount",
                "value_key": "paid_amount",
                "result_ref_key": "result_ref",
                "window_id_key": "window_id",
                "observation_key": "observation_key",
                "source_row_count_key": "source_row_count",
                "coverage_records": (
                    {
                        "result_ref": "result:coverage",
                        "dataset_id": "paid_order_success",
                        "snapshot_refs": ("snapshot:paid:r1",),
                        "completeness_report_ref": "completeness:coverage",
                        "completeness_status": "complete",
                        "analysis_readiness": "ready",
                        "windows": (
                            {
                                "window_id": "target",
                                "required_days": 1,
                                "observed_days": 1,
                            },
                        ),
                    },
                ),
            },
            "trust_boundary",
            "trust_boundary",
            "boundary",
        ),
        (
            "market_channel_context",
            {
                "rows": (
                    {
                        "window_id": "target",
                        "observation_key": "2026-01-02",
                        "channel": "A",
                        "paid_amount": 10,
                    },
                ),
                "metric_id": "paid_amount",
                "value_key": "paid_amount",
                "channel_key": "channel",
                "window_id_key": "window_id",
                "observation_key": "observation_key",
                "required_window_ids": ("target",),
                "required_window_presence": "all",
                "completeness_records": (
                    {
                        "result_ref": "result:channel",
                        "completeness_report_ref": "completeness:channel",
                        "completeness_status": "complete",
                        "analysis_readiness": "ready",
                        "reconciliation_status": "passed",
                    },
                ),
            },
            "trust_boundary",
            "trust_boundary",
            "boundary",
        ),
        (
            "source_reconciliation",
            {
                "sources": (
                    {
                        "source_id": "market_dashboard",
                        "result_ref": "result:overall",
                        "metric_contract_ref": "contract:paid-amount",
                        "reconciliation_tolerance": 0.01,
                        "reconciliation_strategy": "additive_sum",
                        "rows": (
                            {
                                "window_id": "target",
                                "window_role": "target",
                                "observation_key": "2026-01-02",
                                "paid_amount": 10,
                            },
                        ),
                    },
                    {
                        "source_id": "market_dashboard_channel",
                        "result_ref": "result:channel",
                        "metric_contract_ref": "contract:paid-amount",
                        "reconciliation_tolerance": 0.01,
                        "reconciliation_strategy": "additive_sum",
                        "rows": (
                            {
                                "window_id": "target",
                                "window_role": "target",
                                "observation_key": "2026-01-02",
                                "paid_amount": 10,
                            },
                        ),
                    },
                ),
                "join_keys": ("window_id", "observation_key"),
                "value_key": "paid_amount",
                "reconciliation_tolerance": 0.01,
                "reconciliation_strategy": "additive_sum",
                "reconciliation_policy": {
                    "contract_id": "bounded-window-source-reconciliation.v1",
                    "authoritative_source_id": "market_dashboard",
                    "partition_source_id": "market_dashboard_channel",
                    "window_id_key": "window_id",
                    "window_role_key": "window_role",
                    "bounded_window_relative_tolerance": 0.002,
                    "bounded_change_residual_share": 0.01,
                    "hard_observation_relative_limit": 0.01,
                },
            },
            "quantified_contribution",
            "accounting_contribution",
            "derived",
        ),
    ),
)
def test_remaining_phase3_primitives_settle_through_builtin_adapters(
    capability_id: str,
    payload: dict,
    claim_ceiling: str,
    evidence_type: str,
    evidence_kind: str,
) -> None:
    plan = _plan((capability_id,), seed=capability_id)
    task = plan.capability_tasks[0]
    execute = builtin_capability_adapter_registry().bind(
        plan,
        _runtime(
            _bound(
                plan,
                payload=payload,
                maximum_claim_strength=claim_ceiling,
            )
        ),
    )

    output = execute(task, CapabilityAttempt.create(plan, task))

    assert output.status == "succeeded"
    assert output.output_payload["typed_payload"]["claim_ceiling"] == (claim_ceiling)
    assert output.output_payload["evidence_type"] == evidence_type
    assert output.evidence[0].evidence_kind == evidence_kind
    assert output.evidence[0].maximum_claim_strength == claim_ceiling
    assert output.evidence[0].evidence_strength == output.output_payload["strength"]
    assert output.evidence[0].result_refs == (f"result:{task.task_id}",)
    if capability_id == "source_reconciliation":
        observation = next(
            item
            for item in output.evidence[0].observation_facts
            if item.get("projection_kind") == "claim_material_summary"
        )
        assert observation["projection_kind"] == "claim_material_summary"
        assert "observation_reconciliations" not in observation


def test_source_reconciliation_keeps_full_output_behind_bounded_claim_summary() -> None:
    capability_id = "source_reconciliation"
    rows = tuple(
        {
            "window_id": "target",
            "window_role": "target",
            "observation_key": f"2024-01-01:{index:04d}",
            "paid_amount": 100_000 + index,
        }
        for index in range(900)
    )
    payload = {
        "sources": (
            {
                "source_id": "market_dashboard",
                "result_ref": "result:overall",
                "metric_contract_ref": "contract:paid-amount",
                "reconciliation_tolerance": 0.01,
                "reconciliation_strategy": "additive_sum",
                "rows": rows,
            },
            {
                "source_id": "market_dashboard_channel",
                "result_ref": "result:channel",
                "metric_contract_ref": "contract:paid-amount",
                "reconciliation_tolerance": 0.01,
                "reconciliation_strategy": "additive_sum",
                "rows": rows,
            },
        ),
        "join_keys": ("window_id", "observation_key"),
        "value_key": "paid_amount",
        "reconciliation_tolerance": 0.01,
        "reconciliation_strategy": "additive_sum",
        "reconciliation_policy": {
            "contract_id": "bounded-window-source-reconciliation.v1",
            "authoritative_source_id": "market_dashboard",
            "partition_source_id": "market_dashboard_channel",
            "window_id_key": "window_id",
            "window_role_key": "window_role",
            "bounded_window_relative_tolerance": 0.002,
            "bounded_change_residual_share": 0.01,
            "hard_observation_relative_limit": 0.01,
        },
    }
    plan = _plan((capability_id,), seed="bounded-source-reconciliation")
    task = plan.capability_tasks[0]
    execute = builtin_capability_adapter_registry().bind(
        plan,
        _runtime(
            _bound(
                plan,
                payload=payload,
                maximum_claim_strength="quantified_contribution",
            )
        ),
    )

    output = execute(task, CapabilityAttempt.create(plan, task))

    assert output.status == "succeeded"
    assert len(
        output.output_payload["typed_payload"]["observation_reconciliations"]
    ) == 900
    observation = next(
        item
        for item in output.evidence[0].observation_facts
        if item.get("projection_kind") == "claim_material_summary"
    )
    assert observation["observation_count"] == 900
    assert "observation_reconciliations" not in observation
    encoded = json.dumps(
        canonical_value(output.evidence[0].observation_facts),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert len(encoded) <= CAPABILITY_EVIDENCE_OBSERVATION_BYTE_LIMIT


@pytest.mark.parametrize(
    ("capability_id", "payload", "expected_limitation"),
    (
        (
            "change_point_scan",
            {
                "rows": (
                    {
                        "observation_key": "2026-01-01",
                        "paid_amount": 10,
                    },
                ),
                "time_key": "observation_key",
                "value_key": "paid_amount",
                "min_total_samples": 8,
                "min_segment_samples": 4,
                "min_relative_level_shift": 0.2,
                "min_standardized_level_shift": 2.0,
                "max_candidates": 5,
            },
            "insufficient_ordered_samples",
        ),
        (
            "metric_coverage_profile",
            {
                "rows": (),
                "metric_id": "paid_amount",
                "value_key": "paid_amount",
                "result_ref_key": "result_ref",
                "window_id_key": "window_id",
                "observation_key": "observation_key",
                "source_row_count_key": "source_row_count",
                "coverage_records": (),
            },
            "coverage_evidence_absent",
        ),
        (
            "market_channel_context",
            {
                "rows": (),
                "metric_id": "paid_amount",
                "value_key": "paid_amount",
                "channel_key": "channel",
                "window_id_key": "window_id",
                "observation_key": "observation_key",
                "required_window_ids": ("target",),
                "required_window_presence": "all",
                "completeness_records": (),
            },
            "no_channel_context_rows",
        ),
        (
            "source_reconciliation",
            {
                "sources": (
                    {
                        "source_id": "market_dashboard",
                        "result_ref": "result:overall",
                        "metric_contract_ref": "contract:paid-amount",
                        "reconciliation_tolerance": 0.01,
                        "reconciliation_strategy": "additive_sum",
                        "rows": (
                            {
                                "window_id": "target",
                                "window_role": "target",
                                "observation_key": "left",
                                "paid_amount": 10,
                            },
                        ),
                    },
                    {
                        "source_id": "market_dashboard_channel",
                        "result_ref": "result:channel",
                        "metric_contract_ref": "contract:paid-amount",
                        "reconciliation_tolerance": 0.01,
                        "reconciliation_strategy": "additive_sum",
                        "rows": (
                            {
                                "window_id": "target",
                                "window_role": "target",
                                "observation_key": "right",
                                "paid_amount": 10,
                            },
                        ),
                    },
                ),
                "join_keys": ("window_id", "observation_key"),
                "value_key": "paid_amount",
                "reconciliation_tolerance": 0.01,
                "reconciliation_strategy": "additive_sum",
                "reconciliation_policy": {
                    "contract_id": "bounded-window-source-reconciliation.v1",
                    "authoritative_source_id": "market_dashboard",
                    "partition_source_id": "market_dashboard_channel",
                    "window_id_key": "window_id",
                    "window_role_key": "window_role",
                    "bounded_window_relative_tolerance": 0.002,
                    "bounded_change_residual_share": 0.01,
                    "hard_observation_relative_limit": 0.01,
                },
            },
            "no_reconciled_pairs",
        ),
    ),
)
def test_insufficient_phase3_primitive_envelopes_settle_unavailable(
    capability_id: str,
    payload: dict,
    expected_limitation: str,
) -> None:
    plan = _plan((capability_id,), seed=f"{capability_id}-insufficient")
    task = plan.capability_tasks[0]
    execute = builtin_capability_adapter_registry().bind(
        plan,
        _runtime(_bound(plan, payload=payload)),
    )

    output = execute(task, CapabilityAttempt.create(plan, task))

    assert output.status == "unavailable"
    assert output.evidence == ()
    assert output.failure is None
    assert output.retryability == "replan_required"
    assert output.output_payload["evidence_type"] == "insufficient_evidence"
    assert output.output_payload["strength"] == "insufficient"
    assert output.output_payload["numeric_facts"]
    assert output.output_payload["typed_payload"]
    assert expected_limitation in output.limitation_refs


def test_candidate_context_keeps_primitive_claim_strength() -> None:
    plan = _plan(("event_evidence",), seed="candidate-context")
    task = plan.capability_tasks[0]
    execute = builtin_capability_adapter_registry().bind(
        plan,
        _runtime(
            _bound(
                plan,
                payload={
                    "events": ({"event_id": "event:campaign", "event_count": 1},),
                },
                maximum_claim_strength="candidate_mechanism",
                supported_evidence_types=("candidate_mechanism",),
                supported_claim_types=("candidate_mechanism",),
            )
        ),
    )

    output = execute(task, CapabilityAttempt.create(plan, task))

    assert output.status == "succeeded"
    assert output.output_payload["evidence_type"] == "candidate_mechanism"
    assert output.evidence[0].evidence_kind == "observed"
    assert output.evidence[0].evidence_strength == "low"
    assert output.evidence[0].maximum_claim_strength == "candidate_mechanism"
    assert output.evidence[0].supported_claim_kinds == ("candidate_mechanism",)


def test_exploration_ready_insufficient_output_continues_without_claim_support() -> None:
    plan = _plan(("candidate_dimension_screen",), seed="continuation-ready")
    task = plan.capability_tasks[0]
    payload = {
        "rows_by_dimension": {
            "region": (
                {
                    "region": "A",
                    "group": "baseline",
                    "amount": 50,
                    "paid_orders": 10,
                    "paid_users": 10,
                    "n": 20,
                },
                {
                    "region": "A",
                    "group": "target",
                    "amount": 60,
                    "paid_orders": 12,
                    "paid_users": 10,
                    "n": 20,
                },
                {
                    "region": "B",
                    "group": "baseline",
                    "amount": 50,
                    "paid_orders": 10,
                    "paid_users": 10,
                    "n": 20,
                },
                {
                    "region": "B",
                    "group": "target",
                    "amount": 60,
                    "paid_orders": 12,
                    "paid_users": 10,
                    "n": 20,
                },
            ),
        },
        "overall_by_group": {"baseline": 100, "target": 120},
        "complete_dimensions": ("region",),
    }
    execute = builtin_capability_adapter_registry().bind(
        plan,
        _runtime(_bound(plan, payload=payload)),
    )

    output = execute(task, CapabilityAttempt.create(plan, task))

    assert output.status == "succeeded"
    assert output.output_payload["evidence_type"] == "insufficient_evidence"
    assert len(output.evidence) == 1
    assert output.evidence[0].evidence_kind == "boundary"
    assert output.evidence[0].supported_claim_kinds == ()
    assert "public_claim_support_unavailable" in output.limitation_refs


def test_event_evidence_keeps_large_match_set_behind_bounded_claim_summary() -> None:
    plan = _plan(("event_evidence",), seed="bounded-event-evidence")
    task = plan.capability_tasks[0]
    events = tuple(
        {
            "event_id": f"event:campaign:{index:04d}",
            "event_count": 1,
            "source_family": "external_event",
            "window_role": "target",
            "event_type": "campaign",
            "event_start_date": "2024-01-01",
            "event_end_date": "2024-01-02",
            "affected_scope": "Nigeria",
            "authority": "reviewed_source",
            "evidence_level": "context",
            "wording_limit": "context",
            "payload": json.dumps(
                {"description": "campaign context " + ("x" * 200)}
            ),
        }
        for index in range(500)
    )
    execute = builtin_capability_adapter_registry().bind(
        plan,
        _runtime(
            _bound(
                plan,
                payload={"events": events},
                maximum_claim_strength="candidate_mechanism",
            )
        ),
    )

    output = execute(task, CapabilityAttempt.create(plan, task))

    assert output.status == "succeeded"
    assert len(output.output_payload["typed_payload"]["event_summary"]) == 500
    observation = next(
        item
        for item in output.evidence[0].observation_facts
        if item.get("projection_kind") == "claim_material_summary"
    )
    assert observation["event_count"] == 500
    assert observation["displayed_event_count"] == 20
    assert observation["omitted_event_count"] == 480
    encoded = json.dumps(
        canonical_value(output.evidence[0].observation_facts),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert len(encoded) <= CAPABILITY_EVIDENCE_OBSERVATION_BYTE_LIMIT


def test_event_evidence_binds_presence_to_frozen_event_identity() -> None:
    plan = _plan(
        ("event_evidence",),
        seed="event-presence",
        event_relative=True,
    )
    task = plan.capability_tasks[0]
    event_ref = plan.temporal_authority.event_ref
    execute = builtin_capability_adapter_registry().bind(
        plan,
        _runtime(
            _bound(
                plan,
                payload={
                    "events": (
                        {"event_id": "business-event:other", "event_count": 1},
                        {"event_id": event_ref, "event_count": 1},
                    ),
                    "event_ref": event_ref,
                    "temporal_authority_ref": (plan.temporal_authority.authority_ref),
                },
                maximum_claim_strength="candidate_mechanism",
                supported_evidence_types=("candidate_mechanism",),
            )
        ),
    )

    output = execute(task, CapabilityAttempt.create(plan, task))

    assert output.status == "succeeded"
    typed = output.output_payload["typed_payload"]
    assert typed["evidence_contract"] == "event-presence.v1"
    assert typed["event_ref"] == event_ref
    assert typed["temporal_authority_ref"] == (plan.temporal_authority.authority_ref)
    assert tuple(item["event_id"] for item in typed["events"]) == (event_ref,)
    assert typed["causal_interpretation_allowed"] is False


def test_event_window_comparison_uses_business_metric_and_non_causal_identity() -> None:
    plan = _plan(
        ("event_window_compare",),
        seed="event-window-comparison",
        event_relative=True,
    )
    task = plan.capability_tasks[0]
    contract = _event_metric_contract(plan)
    event_ref = plan.temporal_authority.event_ref
    authority_ref = plan.temporal_authority.authority_ref
    execute = builtin_capability_adapter_registry().bind(
        plan,
        _runtime(
            _bound(
                plan,
                payload={
                    "contract": contract,
                    "rows": (
                        {
                            "window_id": "target_day",
                            "window_role": "target",
                            "observation_key": "2026-06-19",
                            "paid_amount": 120,
                        },
                        {
                            "window_id": "baseline_window",
                            "window_role": "baseline",
                            "observation_key": "2026-06-18",
                            "paid_amount": 100,
                        },
                    ),
                    "metric_id": "paid_amount",
                    "primary_baseline_window_id": "baseline_window",
                    "event_ref": event_ref,
                    "temporal_authority_ref": authority_ref,
                },
                maximum_claim_strength="directional",
                supported_evidence_types=("observed_comparison",),
            )
        ),
    )

    output = execute(task, CapabilityAttempt.create(plan, task))

    assert output.status == "succeeded"
    assert output.output_payload["evidence_type"] == "observed_comparison"
    assert output.output_payload["strength"] == "directional"
    assert output.output_payload["typed_payload"] == {
        "evidence_contract": "event-window-metric-comparison.v1",
        "event_ref": event_ref,
        "temporal_authority_ref": authority_ref,
        "metric_comparison": output.output_payload["typed_payload"][
            "metric_comparison"
        ],
        "interpretation_contract": output.output_payload["typed_payload"][
            "metric_comparison"
        ]["interpretation_contract"],
        "claim_material_observations": output.output_payload["typed_payload"][
            "claim_material_observations"
        ],
        "causal_interpretation_allowed": False,
    }
    assert output.evidence[0].evidence_kind == "observed"
    assert output.evidence[0].maximum_claim_strength == "directional"
    assert any(
        fact.get("evidence_contract") == "event-window-metric-comparison.v1"
        for fact in output.evidence[0].observation_facts
    )
    assert any(
        fact.get("projection_kind") == "claim_material_summary"
        for fact in output.evidence[0].observation_facts
    )
    assert output.output_payload["typed_payload"]["interpretation_contract"] == {
        "contract_id": "window-metric-comparison-interpretation.v1",
        "analysis_role": "observed_window_comparison",
        "comparison_subject": "same_metric_across_resolved_windows",
        "target_value_definition": "aggregate_metric_over_target_window",
        "baseline_value_definition": ("aggregate_metric_over_primary_baseline_window"),
        "absolute_change_formula": "target_value - baseline_value",
        "relative_change_formula": "absolute_change / baseline_value",
        "zero_baseline_policy": "relative_change_unavailable",
        "completeness_authority": ("required_complete_days_and_observation_keys"),
        "causal_interpretation": "forbidden",
    }


def test_event_window_comparison_rejects_temporal_identity_drift() -> None:
    plan = _plan(
        ("event_window_compare",),
        seed="event-window-drift",
        event_relative=True,
    )
    task = plan.capability_tasks[0]
    contract = _event_metric_contract(plan)
    execute = builtin_capability_adapter_registry().bind(
        plan,
        _runtime(
            _bound(
                plan,
                payload={
                    "contract": contract,
                    "rows": (
                        {
                            "window_id": "target_day",
                            "window_role": "target",
                            "observation_key": "2026-06-19",
                            "paid_amount": 120,
                        },
                        {
                            "window_id": "baseline_window",
                            "window_role": "baseline",
                            "observation_key": "2026-06-18",
                            "paid_amount": 100,
                        },
                    ),
                    "metric_id": "paid_amount",
                    "primary_baseline_window_id": "baseline_window",
                    "event_ref": "business-event:tampered",
                    "temporal_authority_ref": plan.temporal_authority.authority_ref,
                },
                maximum_claim_strength="directional",
            )
        ),
    )

    with pytest.raises(
        CapabilityTaskAdapterContractError,
        match="task_runtime_event_window_authority_mismatch",
    ):
        execute(task, CapabilityAttempt.create(plan, task))


def test_outlier_scan_publishes_declared_statistical_evidence() -> None:
    plan = _plan(("outlier_scan",), seed="undeclared-evidence")
    task = plan.capability_tasks[0]
    execute = builtin_capability_adapter_registry().bind(
        plan,
        _runtime(
            _bound(
                plan,
                payload={
                    "rows": (
                        *(
                            {
                                "window_role": "reference",
                                "observation_key": f"2026-05-{day:02d}",
                                "amount": 10,
                            }
                            for day in range(25, 32)
                        ),
                        {
                            "window_role": "target",
                            "observation_key": "2026-06-01",
                            "amount": 100,
                        },
                    )
                },
            )
        ),
    )

    output = execute(task, CapabilityAttempt.create(plan, task))

    assert output.status == "succeeded"
    assert output.output_payload["evidence_type"] == "statistical_association"
    assert output.evidence[0].evidence_kind == "statistical_association"


def test_outlier_contribution_publishes_daily_mean_sensitivity() -> None:
    plan = _plan(("outlier_contribution",), seed="outlier-sensitivity")
    task = plan.capability_tasks[0]
    execute = builtin_capability_adapter_registry().bind(
        plan,
        _runtime(
            _bound(
                plan,
                payload={
                    "rows": (
                        {"group": "baseline", "period": "b1", "amount": 8},
                        {"group": "baseline", "period": "b2", "amount": 12},
                        {"group": "target", "period": "t1", "amount": 14},
                        {"group": "target", "period": "t2", "amount": 9},
                    ),
                    "period_grain": "day",
                },
                maximum_claim_strength="candidate_driver",
            )
        ),
    )

    output = execute(task, CapabilityAttempt.create(plan, task))

    assert output.status == "succeeded"
    assert output.output_payload["evidence_type"] == "accounting_contribution"
    assert output.output_payload["typed_payload"]["baseline_daily_mean"] == 10
    assert output.output_payload["typed_payload"]["total_delta"] == 3
    assert output.output_payload["typed_payload"]["causal_claim_allowed"] is False
    assert output.evidence[0].evidence_kind == "derived"


def test_undeclared_primitive_evidence_type_exposes_contract_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def undeclared_outlier(*, result_refs, **_kwargs):
        return make_evidence_envelope(
            "outlier_scan",
            evidence_type="contextual_evidence",
            strength="low",
            wording_limit="contextual",
            result_refs=result_refs,
        )

    monkeypatch.setattr(
        capability_task_adapter,
        "outlier_scan",
        undeclared_outlier,
    )
    plan = _plan(("outlier_scan",), seed="undeclared-evidence")
    task = plan.capability_tasks[0]
    execute = builtin_capability_adapter_registry().bind(
        plan,
        _runtime(
            _bound(
                plan,
                payload={"rows": ({"amount": 10},)},
            )
        ),
    )

    with pytest.raises(
        CapabilityTaskAdapterContractError,
        match="^primitive_evidence_type_unsupported:contextual_evidence$",
    ):
        execute(task, CapabilityAttempt.create(plan, task))


def test_recognized_primitive_evidence_type_must_be_declared_by_binding() -> None:
    plan = _plan(("event_evidence",), seed="undeclared-recognized-evidence")
    task = plan.capability_tasks[0]
    execute = builtin_capability_adapter_registry().bind(
        plan,
        _runtime(
            _bound(
                plan,
                payload={
                    "events": ({"event_id": "event:campaign", "event_count": 1},),
                },
                maximum_claim_strength="candidate_mechanism",
                supported_evidence_types=("observed_comparison",),
            )
        ),
    )

    with pytest.raises(
        CapabilityTaskAdapterContractError,
        match=("^primitive_evidence_type_not_declared:candidate_mechanism$"),
    ):
        execute(task, CapabilityAttempt.create(plan, task))


def test_trust_boundary_envelope_cannot_raise_claim_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def invalid_boundary(*, result_refs, **_kwargs):
        return make_evidence_envelope(
            "market_channel_context",
            evidence_type="trust_boundary",
            strength="high",
            wording_limit="supported",
            typed_payload={"claim_ceiling": "high"},
            result_refs=result_refs,
        )

    monkeypatch.setattr(
        capability_task_adapter,
        "market_channel_context",
        invalid_boundary,
    )
    plan = _plan(("market_channel_context",), seed="invalid-boundary")
    task = plan.capability_tasks[0]
    execute = builtin_capability_adapter_registry().bind(
        plan,
        _runtime(_bound(plan)),
    )

    with pytest.raises(
        CapabilityTaskAdapterContractError,
        match="^primitive_trust_boundary_claim_ceiling_invalid$",
    ):
        execute(task, CapabilityAttempt.create(plan, task))


def test_formula_adapter_uses_formula_graph_reconciliation() -> None:
    plan = _plan(("formula_decompose",))
    task = plan.capability_tasks[0]
    execute = builtin_capability_adapter_registry().bind(
        plan,
        _runtime(_bound(plan, payload=_formula_payload())),
    )

    output = execute(task, CapabilityAttempt.create(plan, task))

    assert output.status == "succeeded"
    assert output.output_payload["formula_path_id"] == "frequency_ticket_size"
    assert output.output_payload["formula_contract_ref"] == (
        "contracts/metrics/paid-amount.metric.yaml@0.1"
    )
    decomposition = output.output_payload["decomposition"]
    assert decomposition["status"] == "reconciled"
    assert decomposition["direction"] == "increase"
    assert {item["metric_id"] for item in decomposition["contributions"]} == {
        "paid_users",
        "paid_frequency",
        "avg_order_amount",
    }
    assert output.evidence[0].evidence_kind == "derived"
    assert output.evidence[0].observation_facts[0]["formula_path_id"] == (
        "frequency_ticket_size"
    )
    observed_decomposition = output.evidence[0].observation_facts[0]["decomposition"]
    assert observed_decomposition["contributions"] == decomposition["contributions"]
    assert all(
        {"metric_id", "contribution", "contribution_share"}.issubset(item)
        for item in observed_decomposition["contributions"]
    )
    assert sum(
        item["contribution_share"] for item in observed_decomposition["contributions"]
    ) == pytest.approx(1.0)
    grouped = observed_decomposition["grouped_decompositions"]
    assert len(grouped) == 1
    assert grouped[0]["grouping_id"] == (
        "paid_users_vs_paid_amount_per_paid_user"
    )
    assert {item["metric_id"] for item in grouped[0]["contributions"]} == {
        "paid_users",
        "paid_amount_per_paid_user",
    }
    interpretation = output.evidence[0].observation_facts[0]["interpretation_contract"]
    assert interpretation["contribution_share_denominator"] == (
        "decomposition.contribution_total"
    )
    assert interpretation["contribution_share_range"] == "unbounded_signed"
    assert interpretation["dimension_localization_relationship"] == (
        "co_report_only_no_shared_rank_sum_or_share"
    )
    assert interpretation["contract_id"] == (
        "formula-accounting-decomposition-interpretation.v2"
    )
    assert canonical_value(
        interpretation["factor_hierarchy"]["groupings"]
    ) == [
        {
            "grouping_id": "paid_users_vs_paid_amount_per_paid_user",
            "method": "grouped_shapley",
            "factors": [
                {
                    "factor_ref": "paid_users",
                    "member_metric_refs": ["paid_users"],
                },
                {
                    "factor_ref": "paid_amount_per_paid_user",
                    "member_metric_refs": [
                        "paid_frequency",
                        "avg_order_amount",
                    ],
                },
            ],
        }
    ]


def test_funnel_adapter_compares_reconciled_daily_rates_without_lifetime_inference() -> None:
    plan = _plan(("funnel_decompose",), seed="p4-funnel")
    task = plan.capability_tasks[0]
    payload = {
        "contract_id": "new-user-funnel-decomposition.v1",
        "source_grain": "dashboard_daily",
        "lifetime_first_payment_supported": False,
        "target_window_ref": plan.resolved_window_refs[0],
        "baseline_window_ref": plan.resolved_window_refs[1],
        "stages": (
            {
                "stage_id": "registration",
                "numerator_metric": "registrations",
                "denominator_metric": "new_users",
                "rate_metric": "registration_rate_new_base",
                "target_numerator": 80,
                "target_denominator": 100,
                "target_rate": 0.8,
                "target_recomputed_rate": 0.8,
                "target_reconciled": True,
                "baseline_numerator": 60,
                "baseline_denominator": 100,
                "baseline_rate": 0.6,
                "baseline_recomputed_rate": 0.6,
                "baseline_reconciled": True,
            },
            {
                "stage_id": "first_payment",
                "numerator_metric": "first_paid_users",
                "denominator_metric": "new_users",
                "rate_metric": "first_pay_rate_new_base",
                "target_numerator": 20,
                "target_denominator": 100,
                "target_rate": 0.2,
                "target_recomputed_rate": 0.2,
                "target_reconciled": True,
                "baseline_numerator": 10,
                "baseline_denominator": 100,
                "baseline_rate": 0.1,
                "baseline_recomputed_rate": 0.1,
                "baseline_reconciled": True,
            },
        ),
    }
    execute = builtin_capability_adapter_registry().bind(
        plan,
        _runtime(
            _bound(
                plan,
                payload=payload,
                maximum_claim_strength="directional",
                supported_evidence_types=("observed_comparison",),
            )
        ),
    )

    output = execute(task, CapabilityAttempt.create(plan, task))

    typed = output.output_payload["typed_payload"]
    stages = {item["stage_id"]: item for item in typed["stages"]}
    assert output.status == "succeeded"
    assert stages["registration"]["rate_delta"] == pytest.approx(0.2)
    assert stages["first_payment"]["rate_delta"] == pytest.approx(0.1)
    assert typed["interpretation_contract"] == {
        "contract_id": "funnel-comparison-interpretation.v1",
        "analysis_role": "window_funnel_comparison",
        "rate_semantics": "ratio_recomputed_from_window_sums",
        "cross_stage_additivity": "forbidden",
        "lifetime_first_payment_inference": "forbidden",
        "causal_interpretation": "forbidden",
    }
    assert output.limitation_refs == (
        "dashboard_daily_funnel_not_lifetime_cohort",
    )


def test_funnel_adapter_fails_closed_on_rate_reconciliation_mismatch() -> None:
    plan = _plan(("funnel_decompose",), seed="p4-funnel-mismatch")
    task = plan.capability_tasks[0]
    payload = {
        "contract_id": "new-user-funnel-decomposition.v1",
        "source_grain": "dashboard_daily",
        "lifetime_first_payment_supported": False,
        "target_window_ref": plan.resolved_window_refs[0],
        "baseline_window_ref": plan.resolved_window_refs[1],
        "stages": (
            {
                "stage_id": "registration",
                "target_reconciled": False,
                "baseline_reconciled": True,
            },
        ),
    }
    execute = builtin_capability_adapter_registry().bind(
        plan,
        _runtime(_bound(plan, payload=payload)),
    )

    output = execute(task, CapabilityAttempt.create(plan, task))

    assert output.status == "unavailable"
    assert output.retryability == "replan_required"
    assert output.limitation_refs == ("funnel-rate-reconciliation-mismatch",)
    assert output.evidence == ()


def test_formula_graph_missing_input_is_unavailable_and_mismatch_is_typed_failure() -> (
    None
):
    missing_plan = _plan(("formula_decompose",), seed="missing")
    missing_task = missing_plan.capability_tasks[0]
    missing_payload = _formula_payload()
    missing_payload["target_metrics"] = {
        "paid_users": 12,
        "paid_frequency": 2,
    }
    missing_execute = builtin_capability_adapter_registry().bind(
        missing_plan,
        _runtime(_bound(missing_plan, payload=missing_payload)),
    )

    missing = missing_execute(
        missing_task,
        CapabilityAttempt.create(missing_plan, missing_task),
    )

    assert missing.status == "unavailable"
    assert missing.output_payload["decomposition"]["status"] == "missing"
    assert missing.failure is None

    mismatch_plan = _plan(("formula_decompose",), seed="mismatch")
    mismatch_task = mismatch_plan.capability_tasks[0]
    mismatch_payload = _formula_payload()
    mismatch_payload["observed_target"] = 10
    mismatch_execute = builtin_capability_adapter_registry().bind(
        mismatch_plan,
        _runtime(_bound(mismatch_plan, payload=mismatch_payload)),
    )

    mismatch = mismatch_execute(
        mismatch_task,
        CapabilityAttempt.create(mismatch_plan, mismatch_task),
    )

    assert mismatch.status == "integrity_failed"
    assert mismatch.failure is not None
    assert mismatch.failure.kind == "formula_reconciliation_mismatch"
    assert mismatch.failure.integrity_level == "task"


def test_each_qualified_hierarchy_path_becomes_independent_evidence() -> None:
    plan = _plan(("candidate_dimension_screen",), seed="hierarchies")
    task = plan.capability_tasks[0]
    rows_by_dimension = {
        "region": (
            {
                "region": "A",
                "group": "baseline",
                "amount": 70,
                "paid_orders": 20,
                "paid_users": 10,
                "n": 20,
            },
            {
                "region": "A",
                "group": "target",
                "amount": 20,
                "paid_orders": 8,
                "paid_users": 5,
                "n": 20,
            },
            {
                "region": "B",
                "group": "baseline",
                "amount": 30,
                "paid_orders": 10,
                "paid_users": 8,
                "n": 5,
            },
            {
                "region": "B",
                "group": "target",
                "amount": 60,
                "paid_orders": 22,
                "paid_users": 12,
                "n": 5,
            },
        ),
        "device_brand": (
            {
                "device_brand": "iOS",
                "group": "baseline",
                "amount": 50,
                "paid_orders": 16,
                "paid_users": 9,
                "n": 20,
            },
            {
                "device_brand": "iOS",
                "group": "target",
                "amount": 70,
                "paid_orders": 24,
                "paid_users": 13,
                "n": 20,
            },
            {
                "device_brand": "Android",
                "group": "baseline",
                "amount": 50,
                "paid_orders": 14,
                "paid_users": 9,
                "n": 20,
            },
            {
                "device_brand": "Android",
                "group": "target",
                "amount": 10,
                "paid_orders": 5,
                "paid_users": 3,
                "n": 20,
            },
        ),
    }
    execute = builtin_capability_adapter_registry().bind(
        plan,
        _runtime(
            _bound(
                plan,
                payload={
                    "rows_by_dimension": rows_by_dimension,
                    "overall_by_group": {"baseline": 100, "target": 80},
                    "complete_dimensions": ("region", "device_brand"),
                    "dimension_metadata": {
                        "region": {"hierarchy_id": "geo", "hierarchy_level": "region"},
                        "device_brand": {
                            "hierarchy_id": "device",
                            "hierarchy_level": "brand",
                        },
                    },
                    "min_sample_size": 10,
                },
            )
        ),
    )

    output = execute(task, CapabilityAttempt.create(plan, task))

    qualified = tuple(item for item in output.evidence if item.hierarchy_qualified)
    main = next(item for item in output.evidence if not item.hierarchy_qualified)
    assert len(qualified) == 2
    assert {item.dimension_path for item in qualified} == {
        ("region",),
        ("device_brand",),
    }
    assert len({item.evidence_ref for item in qualified}) == 2
    qualified_by_dimension = {item.dimension_path[-1]: item for item in qualified}
    assert main.limitation_refs == ()
    assert output.limitation_refs == ()
    assert qualified_by_dimension["region"].limitation_refs == (
        "sparse_dimension_values:region",
    )
    assert qualified_by_dimension["device_brand"].limitation_refs == ()
    interpretation_contract = output.output_payload["typed_payload"][
        "interpretation_contract"
    ]
    assert any(
        observation.get("interpretation_contract") == interpretation_contract
        for observation in main.observation_facts
    )
    priorities = {
        item["dimension"]: item
        for item in output.output_payload["typed_payload"]["diagnostic_priorities"]
    }
    for item in qualified:
        observation = item.observation_facts[0]
        priority = priorities[item.dimension_path[-1]]
        assert observation["interpretation_contract"] == interpretation_contract
        assert observation["priority_rank"] == priority["priority_rank"]
        assert (
            observation["diagnostic_priority_score"]
            == priority["diagnostic_priority_score"]
        )
        assert "business_readout" not in observation


def test_metric_timeseries_preserves_ordered_authoritative_aggregate_points() -> None:
    plan = _plan(("metric_timeseries",), seed="timeseries")
    task = plan.capability_tasks[0]
    execute = builtin_capability_adapter_registry().bind(
        plan,
        _runtime(
            _bound(
                plan,
                payload={
                    "rows": (
                        {
                            "window_id": "target",
                            "window_role": "target",
                            "observation_key": "2026-06-02",
                            "paid_amount": "120.25",
                        },
                        {
                            "window_id": "baseline",
                            "window_role": "baseline",
                            "observation_key": "2026-06-01",
                            "paid_amount": "100.00",
                        },
                    ),
                    "metric_id": "paid_amount",
                },
            )
        ),
    )

    output = execute(task, CapabilityAttempt.create(plan, task))

    assert output.status == "succeeded"
    series = output.output_payload["typed_payload"]
    assert series["metric_id"] == "paid_amount"
    assert series["point_count"] == 2
    assert tuple(point["observation_key"] for point in series["points"]) == (
        "2026-06-01",
        "2026-06-02",
    )
    assert tuple(point["value"] for point in series["points"]) == (
        "100.00",
        "120.25",
    )
    assert series["trend_claim_allowed"] is False


def test_metric_timeseries_exposes_invalid_bound_rows_without_unavailable_fallback() -> (
    None
):
    plan = _plan(("metric_timeseries",), seed="timeseries-invalid")
    task = plan.capability_tasks[0]
    execute = builtin_capability_adapter_registry().bind(
        plan,
        _runtime(
            _bound(
                plan,
                payload={
                    "rows": (
                        {
                            "window_id": "target",
                            "window_role": "target",
                            "observation_key": "2026-06-02",
                        },
                    ),
                    "metric_id": "paid_amount",
                },
            )
        ),
    )

    with pytest.raises(ValueError, match="^metric_timeseries_value_missing$"):
        execute(task, CapabilityAttempt.create(plan, task))


@pytest.mark.parametrize(
    ("capability_id", "payload_key"),
    (
        ("segment_breakdown", "dimension_breakdowns"),
        ("segment_shift_compare", "dimension_shifts"),
    ),
)
def test_segment_distribution_adapters_keep_each_dimension_independent(
    capability_id: str,
    payload_key: str,
) -> None:
    plan = _plan((capability_id,), seed=capability_id)
    task = plan.capability_tasks[0]
    execute = builtin_capability_adapter_registry().bind(
        plan,
        _runtime(
            _bound(
                plan,
                payload={
                    "rows_by_dimension": {
                        "region": (
                            {
                                "region": "A",
                                "window_role": "baseline",
                                "paid_amount": 60,
                            },
                            {
                                "region": "B",
                                "window_role": "baseline",
                                "paid_amount": 40,
                            },
                            {"region": "A", "window_role": "target", "paid_amount": 30},
                            {"region": "B", "window_role": "target", "paid_amount": 90},
                        ),
                        "device_brand": (
                            {
                                "device_brand": "iOS",
                                "window_role": "baseline",
                                "paid_amount": 50,
                            },
                            {
                                "device_brand": "Android",
                                "window_role": "baseline",
                                "paid_amount": 50,
                            },
                            {
                                "device_brand": "iOS",
                                "window_role": "target",
                                "paid_amount": 90,
                            },
                            {
                                "device_brand": "Android",
                                "window_role": "target",
                                "paid_amount": 30,
                            },
                        ),
                    },
                    "metric_id": "paid_amount",
                },
            )
        ),
    )

    output = execute(task, CapabilityAttempt.create(plan, task))

    assert output.status == "succeeded"
    payload = output.output_payload["typed_payload"]
    assert len(payload[payload_key]) == 2
    assert payload["cross_dimension_additivity_allowed"] is False
    qualified = tuple(item for item in output.evidence if item.hierarchy_qualified)
    assert {item.dimension_path for item in qualified} == {
        ("region",),
        ("device_brand",),
    }
    region_evidence = next(
        item for item in qualified if item.dimension_path == ("region",)
    )
    material_summary = region_evidence.observation_facts[0]
    assert material_summary["projection_kind"] == "claim_material_summary"
    assert material_summary["reconciliation_status"] == "passed"
    assert "members" not in material_summary
    if capability_id == "segment_breakdown":
        region = next(
            item for item in payload[payload_key] if item["dimension_id"] == "region"
        )
        member_b = next(item for item in region["members"] if item["member"] == "B")
        assert member_b["baseline_share"] == pytest.approx(0.4)
        assert member_b["target_share"] == pytest.approx(0.75)
        assert material_summary["top_lifts"][0]["member"] == "B"
        assert material_summary["top_drags"][0]["member"] == "A"
    else:
        region = next(
            item for item in payload[payload_key] if item["dimension_id"] == "region"
        )
        member_b = next(item for item in region["members"] if item["member"] == "B")
        assert member_b["share_delta"] == pytest.approx(0.35)
        assert material_summary["top_share_lifts"][0]["member"] == "B"
        assert material_summary["top_share_drags"][0]["member"] == "A"


def test_dense_segment_distribution_keeps_full_result_and_bounded_ranked_evidence() -> (
    None
):
    plan = _plan(("segment_breakdown",), seed="segment-dense-material")
    task = plan.capability_tasks[0]
    rows = tuple(
        row
        for index in range(200)
        for row in (
            {
                "device_model": f"member-{index:04d}",
                "window_role": "baseline",
                "paid_amount": 1000,
            },
            {
                "device_model": f"member-{index:04d}",
                "window_role": "target",
                "paid_amount": (
                    1000 + index + 1 if index % 2 == 0 else 1000 - index - 1
                ),
            },
        )
    )
    execute = builtin_capability_adapter_registry().bind(
        plan,
        _runtime(
            _bound(
                plan,
                payload={
                    "rows_by_dimension": {"device_model": rows},
                    "metric_id": "paid_amount",
                },
            )
        ),
    )

    output = execute(task, CapabilityAttempt.create(plan, task))

    full = output.output_payload["typed_payload"]["dimension_breakdowns"][0]
    evidence = next(item for item in output.evidence if item.hierarchy_qualified)
    summary = evidence.observation_facts[0]
    encoded = json.dumps(
        evidence.to_dict()["observation_facts"],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    ranked_members = {
        item["member"] for item in (*summary["top_lifts"], *summary["top_drags"])
    }

    assert len(full["members"]) == 200
    assert len(summary["top_lifts"]) == 5
    assert len(summary["top_drags"]) == 5
    assert "member-0199" in ranked_members
    assert summary["member_count"] == 200
    assert len(encoded) <= CAPABILITY_EVIDENCE_OBSERVATION_BYTE_LIMIT


def test_segment_breakdown_exposes_offsetting_omitted_movement() -> None:
    plan = _plan(("segment_breakdown",), seed="segment-offsetting-material")
    task = plan.capability_tasks[0]
    changes = (100, -100, 99, -99, 98, -98, 97, -97, 96, -96, 95, -95)
    rows = tuple(
        row
        for index, change in enumerate(changes)
        for row in (
            {
                "device_model": f"member-{index:02d}",
                "window_role": "baseline",
                "paid_amount": 1000,
            },
            {
                "device_model": f"member-{index:02d}",
                "window_role": "target",
                "paid_amount": 1000 + change,
            },
        )
    )
    execute = builtin_capability_adapter_registry().bind(
        plan,
        _runtime(
            _bound(
                plan,
                payload={
                    "rows_by_dimension": {"device_model": rows},
                    "metric_id": "paid_amount",
                },
            )
        ),
    )

    output = execute(task, CapabilityAttempt.create(plan, task))

    evidence = next(item for item in output.evidence if item.hierarchy_qualified)
    summary = evidence.observation_facts[0]

    assert summary["total_delta"] == pytest.approx(0)
    assert summary["displayed_delta"] == pytest.approx(0)
    assert summary["remainder_delta"] == pytest.approx(0)
    assert summary["omitted_member_count"] == 2
    assert summary["remainder_positive_delta"] == pytest.approx(95)
    assert summary["remainder_negative_delta"] == pytest.approx(-95)
    assert summary["remainder_absolute_movement"] == pytest.approx(190)


def test_segment_distribution_missing_comparison_group_is_exposed() -> None:
    plan = _plan(("segment_shift_compare",), seed="segment-invalid")
    task = plan.capability_tasks[0]
    execute = builtin_capability_adapter_registry().bind(
        plan,
        _runtime(
            _bound(
                plan,
                payload={
                    "rows_by_dimension": {
                        "region": (
                            {"region": "A", "window_role": "target", "paid_amount": 30},
                        ),
                    },
                    "metric_id": "paid_amount",
                },
            )
        ),
    )

    with pytest.raises(
        ValueError,
        match="^segment_distribution_group_missing:region:baseline$",
    ):
        execute(task, CapabilityAttempt.create(plan, task))


def _formula_payload() -> dict:
    return {
        "formula_path_id": "frequency_ticket_size",
        "formula_contract_ref": "contracts/metrics/paid-amount.metric.yaml@0.1",
        "formula_ast": {
            "op": "multiply",
            "args": (
                {"op": "metric", "metric_id": "paid_users"},
                {"op": "metric", "metric_id": "paid_frequency"},
                {"op": "metric", "metric_id": "avg_order_amount"},
            ),
        },
        "baseline_metrics": {
            "paid_users": 10,
            "paid_frequency": 2,
            "avg_order_amount": 5,
        },
        "target_metrics": {
            "paid_users": 12,
            "paid_frequency": 2,
            "avg_order_amount": 5,
        },
        "factor_metric_ids": (
            "paid_users",
            "paid_frequency",
            "avg_order_amount",
        ),
        "factor_groupings": (
            {
                "grouping_id": "paid_users_vs_paid_amount_per_paid_user",
                "method": "grouped_shapley",
                "groups": (
                    {
                        "factor_id": "paid_users",
                        "member_metric_ids": ("paid_users",),
                    },
                    {
                        "factor_id": "paid_amount_per_paid_user",
                        "member_metric_ids": ("paid_frequency", "avg_order_amount"),
                    },
                ),
            },
        ),
        "observed_baseline": 100,
        "observed_target": 120,
    }
