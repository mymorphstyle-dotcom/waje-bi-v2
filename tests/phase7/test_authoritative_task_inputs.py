from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from bi_agent.conversation.store import InMemoryConversationStore
from bi_agent.runtime.analysis_contract_compiler import AnalysisCompileOutcome
from bi_agent.runtime.analysis_contracts import (
    AnalysisContract,
    CapabilityExecutionPlan,
    CapabilityInputSlot,
    CompletenessReport,
    ContractGap,
    MetricBinding,
    QueryContract,
    QueryResultEnvelope,
    ResolvedWindow,
    ResultShape,
    analysis_contract_signature,
    completeness_state_from_assertions,
    query_contract_signature,
)
from bi_agent.runtime.authoritative_task_inputs import (
    AuthoritativeTaskInputContractError,
    _align_cross_source_panel_rows,
    _capability_context_rows,
    _compile_material,
    _execute_journaled_query,
    _task_query_disposition,
    _window_metric_value,
    materialize_authoritative_task_inputs,
)
from bi_agent.runtime.analysis_runtime import (
    AnalysisRuntime,
    pinned_dataset_catalog,
)
from bi_agent.runtime.capability_authority import CapabilityAttempt
from bi_agent.runtime.capability_execution import BoundCapabilityInput
from bi_agent.runtime.capability_task_adapter import (
    ExpectedCapabilityGap,
    builtin_capability_adapter_registry,
)
from bi_agent.runtime.dataset_catalog import (
    DatasetCatalog,
    DatasetSnapshot,
    dataset_snapshot_release_ref,
)
from bi_agent.runtime.evidence_authority import EvidenceIntegrityError
from bi_agent.runtime.durable_call_journal import InMemoryDurableCallJournal
from bi_agent.runtime.plan_authority import (
    AnalysisAxis,
    AuthorityContext,
    ClaimObligation,
    EvidenceRequirement,
    PlanContextWindowSpec,
    PlanRevision,
)
from bi_agent.runtime.single_authority import (
    DecisionLedger,
    DecisionRecord,
    IntentRevision,
)
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)
from tests.support.temporal_authority import resolved_test_temporal_authority
from bi_agent.runtime.runtime_persistence import CapabilitySettlementAuthority


TARGET_DATE = "2026-06-19"


class _Registry:
    contract_version = "runtime.v1"
    source_payload_digest = "d" * 64

    @property
    def all_customer_safe_filter_fields(self) -> tuple[str, ...]:
        return ()

    def analysis_goal_question_family_ref(self, goal_id: str) -> str:
        assert goal_id == "explain_change"
        return "paid_amount_change_explanation"

    def capability_inputs(self, capability_id: str):
        assert capability_id == "compare_periods"
        return {
            "task_input_binding": {
                "payload_kind": "window_metric_comparison",
                "query_families": {"primary": "daily_metric_baselines"},
            }
        }


class _Executor:
    def __init__(self, result: QueryResultEnvelope) -> None:
        self.result = result
        self.calls = []

    def execute(
        self,
        contract,
        snapshots,
        *,
        execution_attempt_ref,
        release_resolver,
    ):
        self.calls.append(
            (contract, snapshots, execution_attempt_ref, release_resolver)
        )
        return replace(
            self.result,
            query_contract_ref=contract.query_contract_id,
            execution_attempt_ref=execution_attempt_ref,
        )

    def accept_durable_result(self, contract, snapshots, result):
        assert result.query_contract_ref == contract.query_contract_id
        assert set(snapshots) == set(contract.dataset_snapshot_refs)
        return result


class _Runtime:
    def __init__(self, catalog, executor, *, registry=None) -> None:
        self.registry = registry or _Registry()
        self.executor = executor
        self.release_resolver = object()
        self.evidence_resolver = object()
        self.rows_loader = object()
        self.evidence_writer = object()
        self._catalog = catalog
        self._pinned_catalogs = {}

    def catalog_for_authority_context(self, authority_context):
        return self._pinned_catalogs.get(
            authority_context.authority_context_ref,
            self._catalog,
        )


def _records():
    intent = IntentRevision.create(
        run_attempt_id="run-authoritative-input",
        original_user_text=f"{TARGET_DATE}付费金额为什么上涨？",
        goal_bindings=({"goal_id": "explain_change", "role": "primary"},),
        target_metric_refs=("paid_amount",),
        scope={"scope_type": "full_sample", "filters": []},
        time_spec={"kind": "date", "target": TARGET_DATE},
        comparison_spec={
            "kind": "decision_slot",
            "slot_id": "comparison_baseline",
        },
        direction_premise="user_hypothesis_positive",
        requested_analysis_axes=("change_validation",),
        desired_decisions=(
            {"decision_kind": "explain_change", "target_ref": "paid_amount"},
        ),
        ambiguity_slots=(
            {
                "slot_id": "comparison_baseline",
                "slot_kind": "baseline",
                "materiality": "material",
                "status": "unresolved",
                "question": "与哪个基准比较？",
                "allowed_value_refs": ("previous_day",),
            },
        ),
        source_spans=(),
        schema_version="intent-revision.v1",
        prompt_version="single-authority-intent.v1",
        model_version="test-model",
    )
    decision = DecisionRecord.create(
        intent_revision_id=intent.intent_revision_id,
        slot_id="comparison_baseline",
        value={"baseline_id": "previous_day"},
        source="user",
        status="user_confirmed",
        materiality="material",
        affected_plan_fields=("resolved_window_refs",),
        option_id="baseline.previous_day",
    )
    ledger = DecisionLedger().append(decision)
    context = AuthorityContext.create(
        run_attempt_id=intent.run_attempt_id,
        actual_as_of="2026-07-17T08:00:00Z",
        release_refs=("release:paid:r1",),
        snapshot_refs=("snapshot:paid:r1",),
        dataset_coverage=(
            {
                "dataset_id": "paid_order_success",
                "availability": "claim_ready",
                "release_ref": "release:paid:r1",
                "snapshot_refs": ("snapshot:paid:r1",),
                "limitation_ref": None,
            },
        ),
        contract_versions={
            "runtime_bindings": "runtime.v1",
            "runtime_bindings_digest": "d" * 64,
        },
    )
    obligation = ClaimObligation.create(
        claim_kind="comparative_change",
        role="user_required",
        subject={
            "target_metric_ref": "paid_amount",
            "scope": {"scope_type": "full_sample", "filters": []},
            "outcome_refs": ("outcome:comparative_change",),
            "goal_refs": ("explain_change",),
        },
        evidence_requirement=EvidenceRequirement.create(
            operator="any_of",
            evidence_kinds=("statistical_association",),
        ),
        success_policy={
            "policy": "verified_or_explicit_boundary",
            "minimum_claim_strength": "directional",
        },
    )
    axis = AnalysisAxis.create(
        axis_id="change_validation",
        role="required",
        axis_kind="change_validation",
        target_metric_refs=("paid_amount",),
        metric_refs=(),
        dimension_refs=(),
        context_source_refs=(),
        capability_refs=("compare_periods",),
        reconciliation_group="paid_amount_change",
        selection_policy="primary_baseline_required",
        source_refs=("contract:paid-amount",),
        goal_refs=("explain_change",),
        supports_obligation_ids=(obligation.obligation_id,),
    )
    temporal_authority = resolved_test_temporal_authority(
        time_spec=intent.time_spec,
        comparison_spec=intent.comparison_spec,
        decision_ledger=ledger,
        require_physical_baseline=True,
    )
    plan = PlanRevision.create(
        run_attempt_id=intent.run_attempt_id,
        supersedes_plan_revision_id=None,
        intent_revision_id=intent.intent_revision_id,
        decision_refs=(decision.decision_id,),
        authority_context_ref=context.authority_context_ref,
        planner_proposal_ref="planner-proposal:test",
        proposal_admission_ref="proposal-admission:test",
        temporal_authority=temporal_authority,
        resolved_window_refs=temporal_authority.resolved_window_refs,
        context_window_specs=(),
        claim_obligations=(obligation,),
        analysis_axes=(axis,),
        capability_task_specs=(
            {
                "task_key": "change_validation:compare_periods",
                "capability_id": "compare_periods",
                "normalized_input_refs": (
                    context.authority_context_ref,
                    axis.analysis_axis_ref,
                    f"window:target:{TARGET_DATE}",
                    "window:baseline:previous_day",
                    "metric:paid_amount",
                    "dataset:paid_order_success",
                ),
                "dependency_task_keys": (),
                "obligation_edges": (
                    {"obligation_id": obligation.obligation_id, "required": True},
                ),
                "execution_rank": 1,
                "declared_budget_units": 1,
                "governor_inputs": {
                    "expected_information_gain": "obligation_closing",
                    "materiality": "user_required",
                    "actionability": "decision_supporting",
                    "statistical_risk": "contract_bounded",
                },
                "execution_policy": {
                    "degradation_policy": {"missing_required_input": "block_claim"},
                    "integrity_failure": "fail_closed",
                    "input_states": (
                        {
                            "input_ref": "dataset:paid_order_success",
                            "availability": "claim_ready",
                            "limitation_ref": None,
                        },
                    ),
                },
            },
        ),
        assumption_refs=(),
        budget_policy_ref="budget-policy:test",
        contract_versions=context.contract_versions,
    )
    return intent, ledger, context, plan


def _windows():
    return (
        ResolvedWindow(
            window_id="target_day",
            role="target",
            label=TARGET_DATE,
            start_inclusive=TARGET_DATE,
            end_exclusive="2026-06-20",
            timezone="Africa/Lagos",
            aggregation="sum_of_complete_days",
            required_complete_days=1,
            source_watermark_requirement=TARGET_DATE,
        ),
        ResolvedWindow(
            window_id="previous_day",
            role="baseline",
            label="2026-06-18",
            start_inclusive="2026-06-18",
            end_exclusive=TARGET_DATE,
            timezone="Africa/Lagos",
            aggregation="sum_of_complete_days",
            required_complete_days=1,
            source_watermark_requirement="2026-06-18",
        ),
    )


def test_formula_consumes_sum_and_mean_window_aggregates_without_reaggregation():
    mean_window = ResolvedWindow(
        window_id="rolling_7_day_baseline",
        role="baseline",
        label="rolling 7 day baseline",
        start_inclusive="2026-06-12",
        end_exclusive=TARGET_DATE,
        timezone="Africa/Lagos",
        aggregation="mean_of_complete_days",
        required_complete_days=7,
        source_watermark_requirement="2026-06-18",
    )
    sum_window = replace(mean_window, aggregation="sum_of_complete_days")
    mean_row = {
        "window_id": mean_window.window_id,
        "observation_key": mean_window.window_id,
        "paid_amount": 100,
        "paid_users": 10,
        "paid_orders": 20,
        "paid_frequency": 2,
        "avg_order_amount": 5,
    }
    sum_row = {
        **mean_row,
        "paid_amount": 700,
        "paid_users": 70,
        "paid_orders": 140,
    }

    for metric_id in (
        "paid_amount",
        "paid_users",
        "paid_orders",
        "paid_frequency",
        "avg_order_amount",
    ):
        assert float(
            _window_metric_value(
                (mean_row,),
                mean_window,
                metric_id,
                aggregate_result=True,
            )
        ) == pytest.approx(mean_row[metric_id])
        assert float(
            _window_metric_value(
                (sum_row,),
                sum_window,
                metric_id,
                aggregate_result=True,
            )
        ) == pytest.approx(sum_row[metric_id])

    assert (
        mean_row["paid_users"]
        * mean_row["paid_frequency"]
        * mean_row["avg_order_amount"]
        == mean_row["paid_amount"]
    )
    assert (
        sum_row["paid_users"] * sum_row["paid_frequency"] * sum_row["avg_order_amount"]
        == sum_row["paid_amount"]
    )

    with pytest.raises(
        EvidenceIntegrityError,
        match="authoritative_window_aggregate_invalid",
    ):
        _window_metric_value(
            (
                mean_row,
                {**mean_row, "observation_key": "2026-06-18"},
            ),
            mean_window,
            "paid_amount",
            aggregate_result=True,
        )


def test_materializer_rejects_replayed_raw_identifier_filter_before_compilation():
    intent, _ledger, _context, plan = _records()
    replayed_intent = replace(
        intent,
        scope={
            "scope_type": "full_sample",
            "filters": ({"field": "user_id", "op": "eq", "value": "u-00042"},),
        },
    )
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)

    with pytest.raises(
        AuthoritativeTaskInputContractError,
        match="authoritative_scope_filter_field_unapproved:user_id",
    ):
        _compile_material(
            plan=plan,
            intent=replayed_intent,
            registry=registry,
        )


class _ChangePointRegistry(_Registry):
    def capability_inputs(self, capability_id: str):
        assert capability_id == "change_point_scan"
        return {
            "context_window_policy": {
                "relation": "trailing_complete_periods",
                "allowed_units": ["day"],
                "count_bounds": {"day": [8, 900]},
                "aggregation": "daily_observations",
                "execution_default": {"unit": "day", "count": 8},
            },
            "task_input_binding": {
                "payload_kind": "change_point_scan",
                "query_families": {"primary": "daily_metric_baselines"},
                "fields": {
                    "time_key": "observation_key",
                    "value_key": "paid_amount",
                },
                "parameters": {
                    "min_total_samples": 8,
                    "min_segment_samples": 4,
                    "min_relative_level_shift": 0.2,
                    "min_standardized_level_shift": 2.0,
                    "max_candidates": 5,
                },
            },
        }


def _change_point_plan(plan: PlanRevision) -> PlanRevision:
    obligation = plan.claim_obligations[0]
    axis = AnalysisAxis.create(
        axis_id="anomaly_validation",
        role="required",
        axis_kind="anomaly_detection",
        target_metric_refs=("paid_amount",),
        metric_refs=(),
        dimension_refs=(),
        context_source_refs=(),
        capability_refs=("change_point_scan",),
        reconciliation_group="paid_amount_anomaly_validation",
        selection_policy="user_or_evidence_triggered",
        source_refs=("contract:change-point-scan",),
        goal_refs=("explain_change",),
        supports_obligation_ids=(obligation.obligation_id,),
    )
    spec = PlanContextWindowSpec.create(
        capability_id="change_point_scan",
        relation="trailing_complete_periods",
        unit="day",
        count=8,
    )
    return PlanRevision.create(
        run_attempt_id=plan.run_attempt_id,
        supersedes_plan_revision_id=None,
        intent_revision_id=plan.intent_revision_id,
        decision_refs=plan.decision_refs,
        authority_context_ref=plan.authority_context_ref,
        planner_proposal_ref=plan.planner_proposal_ref,
        proposal_admission_ref=plan.proposal_admission_ref,
        temporal_authority=plan.temporal_authority,
        resolved_window_refs=plan.resolved_window_refs,
        context_window_specs=(spec,),
        claim_obligations=(obligation,),
        analysis_axes=(axis,),
        capability_task_specs=(
            {
                "task_key": "anomaly_validation:change_point_scan",
                "capability_id": "change_point_scan",
                "normalized_input_refs": (
                    plan.authority_context_ref,
                    axis.analysis_axis_ref,
                    *plan.resolved_window_refs,
                    "metric:paid_amount",
                    "dataset:paid_order_success",
                    spec.normalized_input_ref,
                ),
                "dependency_task_keys": (),
                "obligation_edges": (
                    {
                        "obligation_id": obligation.obligation_id,
                        "required": True,
                    },
                ),
                "execution_rank": 1,
                "declared_budget_units": 1,
                "governor_inputs": {
                    "expected_information_gain": "obligation_closing",
                    "materiality": "user_required",
                    "actionability": "decision_supporting",
                    "statistical_risk": "contract_bounded",
                },
                "execution_policy": {
                    "degradation_policy": {"missing_required_input": "omit_path"},
                    "integrity_failure": "fail_closed",
                    "input_states": (
                        {
                            "input_ref": "dataset:paid_order_success",
                            "availability": "claim_ready",
                            "limitation_ref": None,
                        },
                    ),
                },
            },
        ),
        assumption_refs=(),
        budget_policy_ref=plan.budget_policy_ref,
        contract_versions=plan.contract_versions,
    )


def _change_point_outcome(plan: PlanRevision) -> AnalysisCompileOutcome:
    context_window = ResolvedWindow(
        window_id=("context__change_point_scan__trailing_complete_periods__8_day"),
        role="reference",
        label="2026-06-11..2026-06-18",
        start_inclusive="2026-06-11",
        end_exclusive=TARGET_DATE,
        timezone="Africa/Lagos",
        aggregation="mean_of_complete_days",
        required_complete_days=8,
        source_watermark_requirement="2026-06-18",
        capability_refs=("change_point_scan",),
    )
    windows = (*_windows(), context_window)
    metric = MetricBinding(
        metric_id="paid_amount",
        contract_ref="contract:paid-amount",
        dataset_id="paid_order_success",
        expression="sum(paid_amount)",
        aggregation="sum",
        required_fields=("paid_amount",),
        grain=("window_id",),
    )
    logical = {
        "analysis_contract_ref": f"analysis:{plan.plan_revision_id}:1",
        "query_intent": "daily_metric_baselines",
        "dataset_snapshot_refs": ("snapshot:paid:r1",),
        "metric_bindings": (metric,),
        "dimension_bindings": (),
        "window_refs": tuple(item.window_id for item in windows),
        "resolved_windows": windows,
        "filters": (),
        "result_shape": ResultShape(
            required_fields=(
                "window_id",
                "window_role",
                "observation_key",
                "paid_amount",
            ),
            unique_key=("window_id", "observation_key"),
            grain=("window_id", "observation_key"),
            required_window_ids=tuple(item.window_id for item in windows),
        ),
        "completeness_assertions": (
            "required_fields_present",
            "required_windows_complete",
        ),
        "workload_class": "interactive_aggregate",
    }
    query = QueryContract(
        query_contract_id=f"query:{plan.plan_revision_id}:1",
        contract_signature=query_contract_signature(logical),
        **logical,
    )
    slot = CapabilityInputSlot(
        slot_id="daily_metric_baselines",
        query_contract_refs=(query.query_contract_id,),
        required=True,
        accepted_completeness=("complete",),
        required_fields=query.result_shape.required_fields,
        required_window_ids=query.result_shape.required_window_ids,
    )
    capability_plan = CapabilityExecutionPlan(
        capability_id="change_point_scan",
        capability_contract_ref="contract:capability:change-point-scan",
        required_input_slots=(slot,),
        optional_input_slots=(),
        merge_strategy="by_query_family",
        minimum_readiness={
            "required_slots": "all",
            "accepted_completeness": ("complete",),
        },
        degradation_policy={"missing_required_input": "omit_path"},
        supported_evidence_types=(
            "statistical_association",
            "insufficient_evidence",
        ),
        maximum_claim_strength="anomaly_candidate",
        analysis_contract_ref=logical["analysis_contract_ref"],
        supported_claim_types=(
            "external_shock_candidate_or_anomaly",
            "comparative_change",
        ),
    )
    analysis = AnalysisContract(
        analysis_contract_id=logical["analysis_contract_ref"],
        contract_version="runtime.v1",
        question_families=("paid_amount_change_explanation",),
        target_metric_refs=("paid_amount",),
        claim_intents=("comparative_change",),
        scope={"type": "full_sample"},
        business_timezone="Africa/Lagos",
        as_of="2026-07-17T08:00:00+00:00",
        resolved_windows=windows,
        metric_bindings=(metric,),
        dimension_bindings=(),
        dataset_requirements=("paid_order_success",),
        capability_requirements=("change_point_scan",),
        contract_gaps=(),
    )
    return AnalysisCompileOutcome(analysis, (query,), (capability_plan,))


def _outcome(plan: PlanRevision, *, gaps=()):
    windows = _windows()
    metric = MetricBinding(
        metric_id="paid_amount",
        contract_ref="contract:paid-amount",
        dataset_id="paid_order_success",
        expression="sum(paid_amount)",
        aggregation="sum",
        required_fields=("paid_amount",),
        grain=("window_id",),
    )
    logical = {
        "analysis_contract_ref": f"analysis:{plan.plan_revision_id}:1",
        "query_intent": "daily_metric_baselines",
        "dataset_snapshot_refs": ("snapshot:paid:r1",),
        "metric_bindings": (metric,),
        "dimension_bindings": (),
        "window_refs": tuple(item.window_id for item in windows),
        "resolved_windows": windows,
        "filters": (),
        "result_shape": ResultShape(
            required_fields=(
                "window_id",
                "window_role",
                "observation_key",
                "paid_amount",
            ),
            unique_key=("window_id", "observation_key"),
            grain=("window_id", "observation_key"),
            required_window_ids=tuple(item.window_id for item in windows),
        ),
        "completeness_assertions": (
            "required_fields_present",
            "required_windows_complete",
        ),
        "workload_class": "interactive_aggregate",
    }
    query = QueryContract(
        query_contract_id=f"query:{plan.plan_revision_id}:1",
        contract_signature=query_contract_signature(logical),
        **logical,
    )
    slot = CapabilityInputSlot(
        slot_id="daily_metric_baselines",
        query_contract_refs=(query.query_contract_id,),
        required=True,
        accepted_completeness=("complete",),
        required_fields=query.result_shape.required_fields,
        required_window_ids=query.result_shape.required_window_ids,
    )
    capability_plan = CapabilityExecutionPlan(
        capability_id="compare_periods",
        capability_contract_ref="contract:capability:compare-periods",
        required_input_slots=(slot,),
        optional_input_slots=(),
        merge_strategy="by_query_family",
        minimum_readiness={
            "required_slots": "all",
            "accepted_completeness": ("complete",),
        },
        degradation_policy={"missing_required_input": "block_claim"},
        supported_evidence_types=("statistical_association",),
        maximum_claim_strength="directional",
        analysis_contract_ref=logical["analysis_contract_ref"],
        supported_claim_types=("comparative_change",),
    )
    analysis = AnalysisContract(
        analysis_contract_id=logical["analysis_contract_ref"],
        contract_version="runtime.v1",
        question_families=("paid_amount_change_explanation",),
        target_metric_refs=("paid_amount",),
        claim_intents=("comparative_change",),
        scope={"type": "full_sample"},
        business_timezone="Africa/Lagos",
        as_of="2026-07-17T08:00:00+00:00",
        resolved_windows=windows,
        metric_bindings=(metric,),
        dimension_bindings=(),
        dataset_requirements=("paid_order_success",),
        capability_requirements=("compare_periods",),
        contract_gaps=tuple(gaps),
    )
    return AnalysisCompileOutcome(analysis, (query,), (capability_plan,))


def _physical(query: QueryContract):
    rows = (
        {
            "window_id": "target_day",
            "window_role": "target",
            "observation_key": TARGET_DATE,
            "paid_amount": 120,
        },
        {
            "window_id": "previous_day",
            "window_role": "baseline",
            "observation_key": "2026-06-18",
            "paid_amount": 100,
        },
    )
    result = QueryResultEnvelope(
        query_contract_ref=query.query_contract_id,
        query_id="clickhouse:query:1",
        query_hash="hash:query:1",
        result_ref="result:query:1",
        execution_status="succeeded",
        rows_ref="rows:query:1",
        row_count=2,
        completeness_report_ref="completeness:query:1",
        rows=rows,
        observed_schema={key: "String" for key in rows[0]},
        observed_windows=("target_day", "previous_day"),
        observed_grain=("window_id", "observation_key"),
        source_snapshot_refs=("snapshot:paid:r1",),
    )
    report = CompletenessReport(
        report_ref="completeness:query:1",
        query_contract_ref=query.query_contract_id,
        result_ref=result.result_ref,
        completeness_status="complete",
        analysis_readiness="ready",
        assertion_results=(
            {
                "assertion": "execution_succeeded",
                "passed": True,
                "failure_reasons": (),
                "failure_classes": (),
                "details": {},
            },
        ),
        failure_reasons=(),
        coverage_summary={"snapshot_refs": ("snapshot:paid:r1",)},
    )
    return result, report, rows


def _bound(plan, query, rows, *, status="ready", reasons=()):
    value = object.__new__(BoundCapabilityInput)
    defaults = {
        "capability_id": "compare_periods",
        "capability_contract_ref": "contract:capability:compare-periods",
        "capability_contract_version": "runtime.v1",
        "capability_contract_signature": "signature:compare",
        "analysis_contract_ref": f"analysis:{plan.plan_revision_id}:1",
        "status": status,
        "rows_by_slot": {"daily_metric_baselines": rows} if status != "blocked" else {},
        "reasons": tuple(reasons),
        "query_contract_refs": (query.query_contract_id,)
        if status != "blocked"
        else (),
        "result_refs": ("result:query:1",) if status != "blocked" else (),
        "completeness_report_refs": (
            ("completeness:query:1",) if status != "blocked" else ()
        ),
        "binding_manifest": {},
        "binding_manifest_ref": (
            "capability-binding:compare-periods" if status != "blocked" else ""
        ),
        "binding_manifest_digest": (
            "digest:compare-periods" if status != "blocked" else ""
        ),
    }
    tuple_fields = {
        name
        for name in BoundCapabilityInput.__annotations__
        if name.endswith("_refs")
        or name.endswith("_digests")
        or name.endswith("_hashes")
        or name
        in {
            "supported_evidence_types",
            "supported_claim_types",
            "input_completeness_statuses",
        }
    }
    for name in BoundCapabilityInput.__annotations__:
        default = () if name in tuple_fields else ""
        object.__setattr__(value, name, defaults.get(name, default))
    object.__setattr__(value, "supported_evidence_types", ("statistical_association",))
    object.__setattr__(value, "supported_claim_types", ("comparative_change",))
    object.__setattr__(value, "maximum_claim_strength", "directional")
    object.__setattr__(value, "maximum_claim_strength_rank", 1)
    return value


def _catalog(snapshot_ref="snapshot:paid:r1"):
    return DatasetCatalog(
        (
            DatasetSnapshot(
                snapshot_ref=snapshot_ref,
                dataset_id="paid_order_success",
                physical_table="analytics.paid_order_success",
                watermark="2026-07-04",
                schema_fingerprint="schema:paid:r1",
                schema_fields=("business_date", "paid_amount"),
                contract_ref="contract:paid-source",
                loaded_at="2026-07-05T00:00:00+00:00",
                status="active",
                release_ref="release:paid:r1",
                authority_record_ref="snapshot-record:paid:r1",
            ),
        )
    )


def _market_release_payloads(*, revision: str, suffix: str):
    payloads = (
        {
            "snapshot_ref": f"snapshot:market:{suffix}",
            "snapshot_id": "market-logical",
            "dataset_id": "market_dashboard",
            "physical_table": "analytics.market_dashboard_daily",
            "watermark": "2026-06-19",
            "schema_fingerprint": f"schema:market:{suffix}",
            "schema_fields": (
                "snapshot_id",
                "load_revision",
                "business_date",
                "paid_amount",
            ),
            "contract_ref": "contract:market-dashboard",
            "loaded_at": "2026-06-20T00:00:00+00:00",
            "status": "active",
            "evidence_state": "claim_ready",
            "reconciliation_status": "matched",
            "reconciliation_ref": f"reconciliation:market:{suffix}",
            "logical_snapshot_id": "market-logical",
            "load_revision": revision,
            "release_ref": "",
            "requires_release": True,
            "rows_content_hash": "a" * 64,
        },
        {
            "snapshot_ref": f"snapshot:market-channel:{suffix}",
            "snapshot_id": "market-logical",
            "dataset_id": "market_dashboard_channel",
            "physical_table": "analytics.market_dashboard_channel_daily",
            "watermark": "2026-06-19",
            "schema_fingerprint": f"schema:market-channel:{suffix}",
            "schema_fields": (
                "snapshot_id",
                "load_revision",
                "business_date",
                "channel",
                "paid_amount",
            ),
            "contract_ref": "contract:market-dashboard-channel",
            "loaded_at": "2026-06-20T00:00:00+00:00",
            "status": "active",
            "evidence_state": "context_only",
            "reconciliation_status": "matched",
            "reconciliation_ref": f"reconciliation:market-channel:{suffix}",
            "logical_snapshot_id": "market-logical",
            "load_revision": revision,
            "release_ref": "",
            "requires_release": True,
            "rows_content_hash": "b" * 64,
        },
    )
    release_ref = dataset_snapshot_release_ref(
        "market-logical",
        revision,
        (item["snapshot_ref"] for item in payloads),
    )
    return tuple({**item, "release_ref": release_ref} for item in payloads)


def _patch_physical(monkeypatch, plan, outcome, *, bound=None):
    query = outcome.query_contracts[0]
    result, report, rows = _physical(query)
    runtime = _Runtime(_catalog(), _Executor(result))
    compiled_calls = []
    monkeypatch.setattr(
        "bi_agent.runtime.authoritative_task_inputs.compile_analysis_contract",
        lambda **kwargs: compiled_calls.append(kwargs) or outcome,
    )
    monkeypatch.setattr(
        "bi_agent.runtime.authoritative_task_inputs.validate_clickhouse_query_contract",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "bi_agent.runtime.authoritative_task_inputs.validate_query_result",
        lambda *_args, **_kwargs: report,
    )
    monkeypatch.setattr(
        "bi_agent.runtime.authoritative_task_inputs.validate_query_set",
        lambda _contracts, _results, reports, **_kwargs: reports,
    )
    monkeypatch.setattr(
        "bi_agent.runtime.authoritative_task_inputs.bind_capability_inputs",
        lambda *_args, **_kwargs: bound or _bound(plan, query, rows),
    )
    settlement = _empty_settlement_authority(plan)
    monkeypatch.setattr(
        CapabilitySettlementAuthority,
        "from_resolver",
        lambda **_kwargs: settlement,
    )
    return runtime, compiled_calls


def _empty_settlement_authority(plan: PlanRevision):
    analysis = AnalysisContract(
        analysis_contract_id=f"analysis:{plan.plan_revision_id}:settlement",
        contract_version="test.v1",
        question_families=(),
        target_metric_refs=(),
        claim_intents=(),
        scope={"type": "test"},
        business_timezone="Asia/Shanghai",
        as_of="2026-07-18T00:00:00+08:00",
        resolved_windows=(),
        metric_bindings=(),
        dimension_bindings=(),
        dataset_requirements=(),
        capability_requirements=tuple(
            dict.fromkeys(task.capability_id for task in plan.capability_tasks)
        ),
        contract_gaps=(),
    )
    return CapabilitySettlementAuthority.create(
        run_id=plan.run_attempt_id,
        analysis_contract={
            **analysis.to_dict(),
            "contract_signature": analysis_contract_signature(analysis),
        },
        query_contracts=(),
        query_execution_records=(),
        rows_records=(),
        snapshot_records=(),
        completeness_records=(),
        capability_binding_records=(),
    )


def test_materializes_exact_typed_compile_input_and_task_payload(monkeypatch) -> None:
    intent, ledger, context, plan = _records()
    outcome = _outcome(plan)
    runtime, compiled_calls = _patch_physical(monkeypatch, plan, outcome)

    inputs = materialize_authoritative_task_inputs(
        plan_revision=plan,
        intent_revision=intent,
        decision_ledger=ledger,
        authority_context=context,
        analysis_runtime=runtime,
        attempt_journal=InMemoryDurableCallJournal(),
    )

    assert len(compiled_calls) == 1
    call = compiled_calls[0]
    assert call["run_id"] == plan.plan_revision_id
    assert call["accepted_capabilities"] == ("compare_periods",)
    assert call["as_of"] == datetime.fromisoformat("2026-07-17T08:00:00+00:00")
    assert call["temporal_authority"] == plan.temporal_authority
    assert call["proposal"]["target_metrics"] == ("paid_amount",)
    assert "target_semantic" not in call["proposal"]
    assert "fixed_window_bounds" not in call["proposal"]
    assert "baselines" not in call["proposal"]
    assert call["proposal"]["context_window_specs"] == ()
    assert call["proposal"]["scope"] == {"type": "full_sample"}
    assert runtime.executor.calls
    task = plan.capability_tasks[0]
    scoped = inputs.resolve_task_input(plan.plan_revision_id, task.task_id)
    assert scoped.binding_record_ref == "capability-binding:compare-periods"
    assert scoped.data_contract_state == "complete"
    assert scoped.result_refs == ("result:query:1",)
    assert scoped.payload["metric_id"] == "paid_amount"
    assert scoped.payload["primary_baseline_window_id"] == "previous_day"
    assert scoped.payload["contract"] == outcome.query_contracts[0]
    assert scoped.services["bound_capability_input"].status == "ready"


def test_ready_binding_omits_gap_for_unselected_alternative_dataset(monkeypatch) -> None:
    intent, ledger, context, plan = _records()
    gap = ContractGap(
        gap_type="source_unbound",
        gap_id="dataset:payment_attempt:source_unbound",
        dataset_id="payment_attempt",
        affected_capabilities=("compare_periods",),
        affected_claim_types=("comparative_change",),
    )
    outcome = _outcome(plan, gaps=(gap,))
    runtime, _ = _patch_physical(monkeypatch, plan, outcome)

    inputs = materialize_authoritative_task_inputs(
        plan_revision=plan,
        intent_revision=intent,
        decision_ledger=ledger,
        authority_context=context,
        analysis_runtime=runtime,
        attempt_journal=InMemoryDurableCallJournal(),
    )

    scoped = inputs.resolve_task_input(
        plan.plan_revision_id, plan.capability_tasks[0].task_id
    )
    assert scoped.data_contract_state == "complete"
    assert "contract-gap:dataset:payment_attempt:source_unbound" not in (
        scoped.limitation_refs
    )


def test_ready_binding_keeps_gap_for_selected_dataset(monkeypatch) -> None:
    intent, ledger, context, plan = _records()
    gap = ContractGap(
        gap_type="contract_partial",
        gap_id="dataset:paid_order_success:contract_partial:watermark",
        dataset_id="paid_order_success",
        affected_capabilities=("compare_periods",),
        affected_claim_types=("comparative_change",),
    )
    outcome = _outcome(plan, gaps=(gap,))
    runtime, _ = _patch_physical(monkeypatch, plan, outcome)

    inputs = materialize_authoritative_task_inputs(
        plan_revision=plan,
        intent_revision=intent,
        decision_ledger=ledger,
        authority_context=context,
        analysis_runtime=runtime,
        attempt_journal=InMemoryDurableCallJournal(),
    )

    scoped = inputs.resolve_task_input(
        plan.plan_revision_id, plan.capability_tasks[0].task_id
    )
    assert scoped.limitation_refs == (
        "contract-gap:dataset:paid_order_success:contract_partial:watermark",
    )


def test_materializer_rejects_runtime_registry_drift_before_compilation(
    monkeypatch,
) -> None:
    intent, ledger, context, plan = _records()
    runtime, compiled_calls = _patch_physical(
        monkeypatch,
        plan,
        _outcome(plan),
    )
    runtime.registry.source_payload_digest = "e" * 64

    with pytest.raises(
        AuthoritativeTaskInputContractError,
        match="^authoritative_runtime_registry_drift$",
    ):
        materialize_authoritative_task_inputs(
            plan_revision=plan,
            intent_revision=intent,
            decision_ledger=ledger,
            authority_context=context,
            analysis_runtime=runtime,
            attempt_journal=InMemoryDurableCallJournal(),
        )

    assert compiled_calls == []
    assert runtime.executor.calls == []


def test_pinned_catalog_survives_active_release_drift() -> None:
    store = InMemoryConversationStore()
    old_payloads = _market_release_payloads(
        revision="market-load:old",
        suffix="old",
    )
    old_release_ref = old_payloads[0]["release_ref"]
    store.publish_dataset_snapshot_release(
        release_ref=old_release_ref,
        logical_snapshot_id="market-logical",
        payloads=old_payloads,
    )
    context = AuthorityContext.create(
        run_attempt_id="run-pinned-market-release",
        actual_as_of="2026-06-21T00:00:00Z",
        release_refs=(old_release_ref,),
        snapshot_refs=tuple(item["snapshot_ref"] for item in old_payloads),
        dataset_coverage=tuple(
            {
                "dataset_id": item["dataset_id"],
                "availability": item["evidence_state"],
                "release_ref": old_release_ref,
                "snapshot_refs": (item["snapshot_ref"],),
                "limitation_ref": (
                    "limitation:context-only:market_dashboard_channel"
                    if item["evidence_state"] == "context_only"
                    else None
                ),
            }
            for item in old_payloads
        ),
        contract_versions={
            "runtime_bindings": "runtime.v1",
            "runtime_bindings_digest": "d" * 64,
        },
    )
    new_payloads = _market_release_payloads(
        revision="market-load:new",
        suffix="new",
    )
    store.publish_dataset_snapshot_release(
        release_ref=new_payloads[0]["release_ref"],
        logical_snapshot_id="market-logical",
        payloads=new_payloads,
    )

    catalog = pinned_dataset_catalog(context, release_resolver=store)

    assert {item.snapshot_ref for item in catalog.snapshots()} == set(
        context.snapshot_refs
    )
    assert all(item.status == "active" for item in catalog.snapshots())
    assert {
        item["status"]
        for item in store.list_dataset_snapshots()
        if item["snapshot_ref"] in set(context.snapshot_refs)
    } == {"superseded"}
    selected = catalog.resolve(
        "market_dashboard",
        as_of=datetime.fromisoformat(context.actual_as_of.replace("Z", "+00:00")),
        release_resolver=store,
    )
    assert selected.snapshot_ref == "snapshot:market:old"
    assert not hasattr(AnalysisRuntime, "compile")
    assert not hasattr(AnalysisRuntime, "execute")


def test_pinned_catalog_rejects_partial_release_membership() -> None:
    store = InMemoryConversationStore()
    payloads = _market_release_payloads(
        revision="market-load:partial",
        suffix="partial",
    )
    release_ref = payloads[0]["release_ref"]
    store.publish_dataset_snapshot_release(
        release_ref=release_ref,
        logical_snapshot_id="market-logical",
        payloads=payloads,
    )
    context = AuthorityContext.create(
        run_attempt_id="run-partial-release-context",
        actual_as_of="2026-06-21T00:00:00Z",
        release_refs=(release_ref,),
        snapshot_refs=(payloads[0]["snapshot_ref"],),
        dataset_coverage=(
            {
                "dataset_id": payloads[0]["dataset_id"],
                "availability": "claim_ready",
                "release_ref": release_ref,
                "snapshot_refs": (payloads[0]["snapshot_ref"],),
                "limitation_ref": None,
            },
        ),
        contract_versions={
            "runtime_bindings": "runtime.v1",
            "runtime_bindings_digest": "d" * 64,
        },
    )

    with pytest.raises(
        ValueError,
        match="^authority_context_release_snapshot_closure_mismatch:",
    ):
        pinned_dataset_catalog(context, release_resolver=store)


def test_crash_resume_replays_pinned_query_after_active_catalog_changes(
    monkeypatch,
) -> None:
    intent, ledger, context, plan = _records()
    outcome = _outcome(plan)
    runtime, compiled_calls = _patch_physical(monkeypatch, plan, outcome)
    pinned_catalog = _catalog()
    runtime._pinned_catalogs[context.authority_context_ref] = pinned_catalog
    _, report, _ = _physical(outcome.query_contracts[0])
    validations = 0

    def crash_after_query_acceptance(*_args, **_kwargs):
        nonlocal validations
        validations += 1
        if validations == 1:
            raise RuntimeError("worker_crashed_after_query_acceptance")
        return report

    monkeypatch.setattr(
        "bi_agent.runtime.authoritative_task_inputs.validate_query_result",
        crash_after_query_acceptance,
    )
    journal = InMemoryDurableCallJournal()

    with pytest.raises(
        RuntimeError,
        match="^worker_crashed_after_query_acceptance$",
    ):
        materialize_authoritative_task_inputs(
            plan_revision=plan,
            intent_revision=intent,
            decision_ledger=ledger,
            authority_context=context,
            analysis_runtime=runtime,
            attempt_journal=journal,
        )

    runtime._catalog = _catalog("snapshot:paid:new-active")
    resumed = materialize_authoritative_task_inputs(
        plan_revision=plan,
        intent_revision=intent,
        decision_ledger=ledger,
        authority_context=context,
        analysis_runtime=runtime,
        attempt_journal=journal,
    )

    assert len(runtime.executor.calls) == 1
    assert len(compiled_calls) == 2
    assert all(call["catalog"] is pinned_catalog for call in compiled_calls)
    assert resumed.accepted_query_attempt_refs


def test_partial_query_resume_replays_accepted_query_and_executes_only_missing_query():
    _, _, _, plan = _records()
    outcome = _outcome(plan)
    primary = outcome.query_contracts[0]
    secondary = replace(
        primary,
        query_contract_id=primary.query_contract_id + ":secondary",
        contract_signature="b" * 64,
    )
    catalog = _catalog()
    snapshots = {
        ref: next(item for item in catalog.snapshots() if item.snapshot_ref == ref)
        for ref in primary.dataset_snapshot_refs
    }
    executor = _Executor(_physical(primary)[0])
    journal = InMemoryDurableCallJournal()
    task = plan.capability_tasks[0]

    first, first_attempt_ref = _execute_journaled_query(
        plan=plan,
        task=task,
        contract=primary,
        snapshots=snapshots,
        executor=executor,
        release_resolver=object(),
        attempt_journal=journal,
    )
    replayed, replayed_attempt_ref = _execute_journaled_query(
        plan=plan,
        task=task,
        contract=primary,
        snapshots=snapshots,
        executor=executor,
        release_resolver=object(),
        attempt_journal=journal,
    )
    missing, missing_attempt_ref = _execute_journaled_query(
        plan=plan,
        task=task,
        contract=secondary,
        snapshots=snapshots,
        executor=executor,
        release_resolver=object(),
        attempt_journal=journal,
    )

    assert len(executor.calls) == 2
    assert replayed == first
    assert replayed_attempt_ref == first_attempt_ref
    assert missing.query_contract_ref == secondary.query_contract_id
    assert missing_attempt_ref != first_attempt_ref


def test_plan_context_window_reaches_phase3_query_before_execution(
    monkeypatch,
) -> None:
    intent, ledger, context, base_plan = _records()
    plan = _change_point_plan(base_plan)
    outcome = _change_point_outcome(plan)
    compiled_calls = []
    runtime = _Runtime(
        _catalog(),
        _Executor(_physical(outcome.query_contracts[0])[0]),
        registry=_ChangePointRegistry(),
    )
    monkeypatch.setattr(
        "bi_agent.runtime.authoritative_task_inputs.compile_analysis_contract",
        lambda **kwargs: compiled_calls.append(kwargs) or outcome,
    )
    monkeypatch.setattr(
        "bi_agent.runtime.authoritative_task_inputs.validate_clickhouse_query_contract",
        lambda *_args, **_kwargs: None,
    )
    execution_started = RuntimeError("phase3_physical_execution_started")

    def stop_at_execution(*_args, **_kwargs):
        raise execution_started

    runtime.executor.execute = stop_at_execution

    with pytest.raises(RuntimeError) as captured:
        materialize_authoritative_task_inputs(
            plan_revision=plan,
            intent_revision=intent,
            decision_ledger=ledger,
            authority_context=context,
            analysis_runtime=runtime,
            attempt_journal=InMemoryDurableCallJournal(),
        )

    assert captured.value is execution_started
    assert len(compiled_calls) == 1
    spec = plan.context_window_specs[0]
    assert compiled_calls[0]["proposal"]["context_window_specs"] == (spec.to_dict(),)
    reference_windows = tuple(
        window
        for window in outcome.query_contracts[0].resolved_windows
        if window.role == "reference"
    )
    assert len(reference_windows) == 1
    assert reference_windows[0].capability_refs == ("change_point_scan",)
    assert reference_windows[0].required_complete_days == 8
    assert reference_windows[0].window_id in (outcome.query_contracts[0].window_refs)


def test_compiled_capability_set_must_equal_active_plan(monkeypatch) -> None:
    intent, ledger, context, plan = _records()
    outcome = _outcome(plan)
    drifted = AnalysisCompileOutcome(
        replace(outcome.analysis_contract, capability_requirements=()),
        outcome.query_contracts,
        (),
    )
    runtime, _ = _patch_physical(monkeypatch, plan, drifted)

    with pytest.raises(
        AuthoritativeTaskInputContractError,
        match="^authoritative_compiled_capability_set_mismatch$",
    ):
        materialize_authoritative_task_inputs(
            plan_revision=plan,
            intent_revision=intent,
            decision_ledger=ledger,
            authority_context=context,
            analysis_runtime=runtime,
            attempt_journal=InMemoryDurableCallJournal(),
        )

    assert runtime.executor.calls == []


def test_unplanned_reference_window_is_rejected_before_execution(
    monkeypatch,
) -> None:
    intent, ledger, context, plan = _records()
    outcome = _outcome(plan)
    unplanned = ResolvedWindow(
        window_id="context__compare_periods__trailing_complete_periods__8_day",
        role="reference",
        label="2026-06-11..2026-06-18",
        start_inclusive="2026-06-11",
        end_exclusive=TARGET_DATE,
        timezone="Africa/Lagos",
        aggregation="mean_of_complete_days",
        required_complete_days=8,
        source_watermark_requirement="2026-06-18",
        capability_refs=("compare_periods",),
    )
    drifted = AnalysisCompileOutcome(
        replace(
            outcome.analysis_contract,
            resolved_windows=(*outcome.analysis_contract.resolved_windows, unplanned),
        ),
        outcome.query_contracts,
        outcome.capability_plans,
    )
    runtime, _ = _patch_physical(monkeypatch, plan, drifted)

    with pytest.raises(
        AuthoritativeTaskInputContractError,
        match="^authoritative_reference_window_closure_mismatch$",
    ):
        materialize_authoritative_task_inputs(
            plan_revision=plan,
            intent_revision=intent,
            decision_ledger=ledger,
            authority_context=context,
            analysis_runtime=runtime,
            attempt_journal=InMemoryDurableCallJournal(),
        )

    assert runtime.executor.calls == []


def test_unknown_compiled_window_role_is_rejected_before_execution(
    monkeypatch,
) -> None:
    intent, ledger, context, plan = _records()
    outcome = _outcome(plan)
    unknown = replace(
        outcome.analysis_contract.resolved_windows[0],
        window_id="future_window",
        role="future_role",
    )
    drifted = AnalysisCompileOutcome(
        replace(
            outcome.analysis_contract,
            resolved_windows=(*outcome.analysis_contract.resolved_windows, unknown),
        ),
        outcome.query_contracts,
        outcome.capability_plans,
    )
    runtime, _ = _patch_physical(monkeypatch, plan, drifted)

    with pytest.raises(
        AuthoritativeTaskInputContractError,
        match="^authoritative_compiled_window_role_invalid$",
    ):
        materialize_authoritative_task_inputs(
            plan_revision=plan,
            intent_revision=intent,
            decision_ledger=ledger,
            authority_context=context,
            analysis_runtime=runtime,
            attempt_journal=InMemoryDurableCallJournal(),
        )

    assert runtime.executor.calls == []


def test_snapshot_outside_authority_context_is_rejected_before_execution(
    monkeypatch,
) -> None:
    intent, ledger, context, plan = _records()
    outcome = _outcome(plan)
    query = replace(
        outcome.query_contracts[0],
        dataset_snapshot_refs=("snapshot:paid:other",),
    )
    outcome = AnalysisCompileOutcome(
        outcome.analysis_contract,
        (query,),
        outcome.capability_plans,
    )
    result, _, _ = _physical(query)
    runtime = _Runtime(_catalog("snapshot:paid:other"), _Executor(result))
    monkeypatch.setattr(
        "bi_agent.runtime.authoritative_task_inputs.compile_analysis_contract",
        lambda **_kwargs: outcome,
    )

    with pytest.raises(
        AuthoritativeTaskInputContractError,
        match="^authoritative_query_snapshot_authority_mismatch$",
    ):
        materialize_authoritative_task_inputs(
            plan_revision=plan,
            intent_revision=intent,
            decision_ledger=ledger,
            authority_context=context,
            analysis_runtime=runtime,
            attempt_journal=InMemoryDurableCallJournal(),
        )

    assert runtime.executor.calls == []


def test_expected_contract_gap_becomes_typed_task_input(monkeypatch) -> None:
    intent, ledger, context, plan = _records()
    gap = ContractGap(
        gap_type="missing_contract",
        gap_id="dataset:paid_order_success:contract_missing",
        dataset_id="paid_order_success",
        affected_capabilities=("compare_periods",),
        affected_claim_types=("comparative_change",),
    )
    outcome = _outcome(plan, gaps=(gap,))
    query = outcome.query_contracts[0]
    blocked = _bound(
        plan,
        query,
        (),
        status="blocked",
        reasons=("missing_required_slot:daily_metric_baselines",),
    )
    runtime, _ = _patch_physical(
        monkeypatch,
        plan,
        outcome,
        bound=blocked,
    )

    inputs = materialize_authoritative_task_inputs(
        plan_revision=plan,
        intent_revision=intent,
        decision_ledger=ledger,
        authority_context=context,
        analysis_runtime=runtime,
        attempt_journal=InMemoryDurableCallJournal(),
    )

    scoped = inputs.resolve_task_input(
        plan.plan_revision_id, plan.capability_tasks[0].task_id
    )
    assert scoped.binding_record_ref is None
    assert isinstance(scoped.expected_gap, ExpectedCapabilityGap)
    assert scoped.expected_gap.gap_type == "missing_contract"
    assert scoped.expected_gap.retryability == "replan_required"


def test_physical_service_exception_propagates_unchanged(monkeypatch) -> None:
    intent, ledger, context, plan = _records()
    outcome = _outcome(plan)
    runtime, _ = _patch_physical(monkeypatch, plan, outcome)
    sentinel = RuntimeError("clickhouse_transport_disconnected")

    def fail(*_args, **_kwargs):
        raise sentinel

    runtime.executor.execute = fail

    with pytest.raises(RuntimeError) as captured:
        materialize_authoritative_task_inputs(
            plan_revision=plan,
            intent_revision=intent,
            decision_ledger=ledger,
            authority_context=context,
            analysis_runtime=runtime,
            attempt_journal=InMemoryDurableCallJournal(),
        )

    assert captured.value is sentinel


def test_query_failure_is_task_scoped_typed_terminal_outcome(monkeypatch) -> None:
    intent, ledger, context, plan = _records()
    outcome = _outcome(plan)
    query = outcome.query_contracts[0]
    _, successful_report, _ = _physical(query)
    failed_result = replace(
        _physical(query)[0],
        execution_status="failed",
        rows=(),
        row_count=0,
        failure_reason="clickhouse_transport_unavailable",
    )
    failed_report = replace(
        successful_report,
        completeness_status="invalid",
        analysis_readiness="blocked",
        assertion_results=(
            {
                "assertion": "execution_succeeded",
                "passed": False,
                "failure_reasons": ("execution_status:failed",),
                "failure_classes": ("execution_technical",),
                "details": {},
            },
        ),
        failure_reasons=("execution_status:failed",),
    )
    blocked = _bound(
        plan,
        query,
        (),
        status="blocked",
        reasons=("query_execution_failed:daily_metric_baselines",),
    )
    runtime, _ = _patch_physical(monkeypatch, plan, outcome, bound=blocked)
    runtime.executor.result = failed_result
    monkeypatch.setattr(
        "bi_agent.runtime.authoritative_task_inputs.validate_query_result",
        lambda *_args, **_kwargs: failed_report,
    )
    monkeypatch.setattr(
        "bi_agent.runtime.authoritative_task_inputs.validate_query_set",
        lambda _contracts, _results, reports, **_kwargs: reports,
    )

    inputs = materialize_authoritative_task_inputs(
        plan_revision=plan,
        intent_revision=intent,
        decision_ledger=ledger,
        authority_context=context,
        analysis_runtime=runtime,
        attempt_journal=InMemoryDurableCallJournal(),
    )

    task = plan.capability_tasks[0]
    scoped = inputs.resolve_task_input(plan.plan_revision_id, task.task_id)
    assert scoped.expected_gap is None
    assert scoped.terminal_failure_status == "technical_failed"
    assert scoped.terminal_failure is not None
    assert scoped.terminal_failure.kind == "query_execution_failed"
    assert scoped.result_refs == (failed_result.result_ref,)
    output = builtin_capability_adapter_registry().bind(plan, inputs)(
        task,
        CapabilityAttempt.create(plan, task),
    )
    assert output.status == "technical_failed"
    assert output.failure == scoped.terminal_failure


@pytest.mark.parametrize(
    ("failure_class", "expected_gap_type", "failure_status"),
    (
        ("empty_result", "window_data_unavailable", None),
        ("freshness", "dataset_snapshot_unavailable_as_of", None),
        ("reconciliation", "contract_partial", None),
        ("result_consistency", None, "integrity_failed"),
        ("provider_truncation", None, "technical_failed"),
    ),
)
def test_query_disposition_uses_typed_completeness_classification(
    failure_class,
    expected_gap_type,
    failure_status,
) -> None:
    _intent, _ledger, _context, plan = _records()
    outcome = _outcome(plan)
    query = outcome.query_contracts[0]
    result, _report, _rows = _physical(query)
    assertion_results = (
        {
            "assertion": "typed_failure",
            "passed": False,
            "failure_reasons": ("diagnostic text has no policy meaning",),
            "failure_classes": (failure_class,),
            "details": {},
        },
    )
    completeness_status, analysis_readiness = completeness_state_from_assertions(
        assertion_results
    )
    report = CompletenessReport(
        report_ref=result.completeness_report_ref,
        query_contract_ref=query.query_contract_id,
        result_ref=result.result_ref,
        completeness_status=completeness_status,
        analysis_readiness=analysis_readiness,
        assertion_results=assertion_results,
        failure_reasons=("diagnostic text has no policy meaning",),
        coverage_summary={},
    )

    disposition = _task_query_disposition(
        task=plan.capability_tasks[0],
        execution_plan=outcome.capability_plans[0],
        results={query.query_contract_id: result},
        reports={query.query_contract_id: report},
    )

    assert disposition is not None
    if expected_gap_type is not None:
        assert disposition.expected_gap is not None
        assert disposition.expected_gap.gap_type == expected_gap_type
        assert disposition.failure is None
    else:
        assert disposition.expected_gap is None
        assert disposition.failure_status == failure_status
        assert disposition.failure is not None


def test_missing_query_authority_is_integrity_failure() -> None:
    _intent, _ledger, _context, plan = _records()
    outcome = _outcome(plan)

    disposition = _task_query_disposition(
        task=plan.capability_tasks[0],
        execution_plan=outcome.capability_plans[0],
        results={},
        reports={},
    )

    assert disposition is not None
    assert disposition.failure_status == "integrity_failed"
    assert disposition.failure is not None
    assert disposition.failure.kind == "query_authority_invalid"


def test_binding_programming_error_propagates_unchanged(monkeypatch) -> None:
    intent, ledger, context, plan = _records()
    outcome = _outcome(plan)
    runtime, _ = _patch_physical(monkeypatch, plan, outcome)
    sentinel = RuntimeError("unexpected_binding_programming_error")

    def fail(*_args, **_kwargs):
        raise sentinel

    monkeypatch.setattr(
        "bi_agent.runtime.authoritative_task_inputs.bind_capability_inputs",
        fail,
    )
    with pytest.raises(RuntimeError) as captured:
        materialize_authoritative_task_inputs(
            plan_revision=plan,
            intent_revision=intent,
            decision_ledger=ledger,
            authority_context=context,
            analysis_runtime=runtime,
            attempt_journal=InMemoryDurableCallJournal(),
        )
    assert captured.value is sentinel


def test_compile_material_does_not_reproject_temporal_authority() -> None:
    intent, _ledger, _context, plan = _records()
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)

    material = _compile_material(
        plan=plan,
        intent=intent,
        registry=registry,
    )

    assert "target_semantic" not in material
    assert "baselines" not in material
    assert "fixed_window_bounds" not in material
    assert plan.temporal_authority.resolved_window_refs == plan.resolved_window_refs


def test_all_builtin_adapters_have_registry_owned_typed_input_bindings() -> None:
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    adapters = builtin_capability_adapter_registry()
    assert all(
        isinstance(
            registry.capability_inputs(capability_id).get("task_input_binding"),
            dict,
        )
        for capability_id in adapters.capability_ids
    )
    executable_axis_capabilities = {
        capability_id
        for axis_id in registry.analysis_axis_ids
        for capability_id in registry.analysis_axis(axis_id)["capability_refs"]
        if not registry.capability_inputs(capability_id).get("completion_authority")
    }
    assert executable_axis_capabilities - set(adapters.capability_ids) == set()


def test_panel_alignment_projects_only_complete_cross_source_cells_and_coverage() -> (
    None
):
    outcome_rows = (
        {
            "window_id": "w",
            "observation_key": "2026-01-01",
            "channel": "a",
            "paid_amount": 10,
        },
        {
            "window_id": "w",
            "observation_key": "2026-01-02",
            "channel": "a",
            "paid_amount": 20,
        },
        {
            "window_id": "w",
            "observation_key": "2026-01-03",
            "channel": "a",
            "paid_amount": 30,
        },
    )
    candidate_rows = (
        {
            "window_id": "w",
            "observation_key": "2026-01-01",
            "channel": "a",
            "player_bet_amount": 100,
        },
        {
            "window_id": "w",
            "observation_key": "2026-01-02",
            "channel": "a",
            "player_bet_amount": 200,
        },
        {
            "window_id": "w",
            "observation_key": "2026-01-04",
            "channel": "a",
            "player_bet_amount": 400,
        },
    )

    rows, coverage, basis = _align_cross_source_panel_rows(
        outcome_rows,
        candidate_rows,
        join_keys=("window_id", "observation_key", "channel"),
        capability_id="cross_source_panel_association",
    )

    assert {row["observation_key"] for row in rows} == {
        "2026-01-01",
        "2026-01-02",
    }
    assert all("paid_amount" in row and "player_bet_amount" in row for row in rows)
    assert coverage == pytest.approx(2 / 3)
    assert basis == {
        "outcome": {
            "total_cells": 3,
            "matched_cells": 2,
            "coverage": pytest.approx(2 / 3),
        },
        "candidate": {
            "total_cells": 3,
            "matched_cells": 2,
            "coverage": pytest.approx(2 / 3),
        },
    }


def test_panel_alignment_with_no_common_cells_is_typed_zero_coverage() -> None:
    rows, coverage, basis = _align_cross_source_panel_rows(
        (
            {
                "window_id": "w",
                "observation_key": "2026-01-01",
                "channel": "a",
                "paid_amount": 10,
            },
        ),
        (
            {
                "window_id": "w",
                "observation_key": "2026-01-02",
                "channel": "a",
                "player_bet_amount": 100,
            },
        ),
        join_keys=("window_id", "observation_key", "channel"),
        capability_id="cross_source_panel_association",
    )

    assert rows == ()
    assert coverage == 0.0
    assert basis["outcome"]["coverage"] == 0.0
    assert basis["candidate"]["coverage"] == 0.0


def test_time_series_capability_consumes_its_owned_context_window_only() -> None:
    context_window = ResolvedWindow(
        window_id="context__cross_source_association__trailing_complete_periods__180_day",
        role="reference",
        label="2025-12-03..2026-05-31",
        start_inclusive="2025-12-03",
        end_exclusive="2026-06-01",
        timezone="Africa/Lagos",
        aggregation="mean_of_complete_days",
        required_complete_days=180,
        source_watermark_requirement="2026-05-31",
        capability_refs=("cross_source_association",),
    )
    rows = (
        {"window_id": "target_day", "observation_key": "2026-06-01", "paid_amount": 12},
        {
            "window_id": "previous_day",
            "observation_key": "2026-05-31",
            "paid_amount": 10,
        },
        {
            "window_id": context_window.window_id,
            "observation_key": "2026-05-31",
            "paid_amount": 10,
        },
    )

    selected = _capability_context_rows(
        rows,
        resolved_windows=(*_windows(), context_window),
        capability_id="cross_source_association",
    )

    assert selected == (rows[2],)


def test_module_has_no_legacy_runtime_authority_or_case_specific_rule() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "bi_agent/runtime/authoritative_task_inputs.py"
    ).read_text(encoding="utf-8")

    assert "analysis_runtime.execute" not in source
    assert "analysis_runtime.compile" not in source
    assert "accepted_graph" not in source
    assert "accepted_graph" not in source
    assert "Case B" not in source
    assert "case_b" not in source
