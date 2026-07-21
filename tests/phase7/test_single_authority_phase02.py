from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError, fields
from hashlib import sha256
import inspect
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bi_agent.conversation.agent_core import _finalize_authoritative_plan
from bi_agent.conversation.models import ConversationRunRequest
from bi_agent.runtime import langgraph_workflow
from bi_agent.runtime.authoritative_plan_result import (
    parse_authoritative_plan_result,
)
from bi_agent.runtime.contracts import load_contract
from bi_agent.runtime.evidence_authority import (
    EvidenceIntegrityError,
    canonical_digest,
    canonical_value,
)
from bi_agent.runtime.durable_call_journal import InMemoryDurableCallJournal
from bi_agent.runtime.llm_client import LLMResult
from bi_agent.runtime.plan_authority import (
    AnalysisAxis,
    AuthorityContext,
    CapabilityTask,
    ClaimObligation,
    EvidenceRequirement,
    PlanAuthorityContractError,
    PlannerProposal,
    PlanRevision,
    ProposalAdmissionRecord,
)
from bi_agent.runtime.plan_compiler import AuthoritativePlanCompiler
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)
from bi_agent.runtime.single_authority import (
    DecisionLedger,
    DecisionRecord,
    DurableTransition,
    IntentRevision,
)
from bi_agent.runtime.temporal_comparison import (
    TemporalComparisonContractError,
    capability_supports_temporal_authority,
    resolve_effective_comparison,
    temporal_execution_mode,
)
from tests.support.temporal_authority import resolved_test_temporal_authority


ROOT = Path(__file__).resolve().parents[2]
TARGET_DATE = "2026-06-19"
TARGET_WINDOW_REF = f"window:target:{TARGET_DATE}"
BASELINE_WINDOW_REF = "window:baseline:previous_day"


def _registry() -> RuntimeContractRegistry:
    return RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)


def _intent_revision(registry: RuntimeContractRegistry) -> IntentRevision:
    original_user_text = f"{TARGET_DATE}付费金额为什么上涨？"
    metric_text = "付费金额"
    metric_start = original_user_text.index(metric_text)
    date_start = original_user_text.index(TARGET_DATE)
    return IntentRevision.create(
        run_attempt_id="run-attempt-phase02",
        original_user_text=original_user_text,
        goal_bindings=({"goal_id": "explain_change", "role": "primary"},),
        target_metric_refs=("paid_amount",),
        scope={"scope_type": "full_sample", "filters": []},
        time_spec={"kind": "date", "target": TARGET_DATE},
        comparison_spec={
            "kind": "decision_slot",
            "slot_id": "comparison_baseline",
        },
        direction_premise="user_hypothesis_positive",
        requested_analysis_axes=(
            "formula_tree",
            "dimension_localization",
            "time_context",
        ),
        desired_decisions=(
            {"decision_kind": "explain_change", "target_ref": "paid_amount"},
        ),
        ambiguity_slots=(
            {
                "slot_id": "comparison_baseline",
                "slot_kind": "baseline",
                "materiality": "material",
                "status": "unresolved",
                "question": "目标日期要与哪个基准比较？",
                "allowed_value_refs": [
                    "previous_day",
                    "rolling_7_day_baseline",
                    "same_weekday_last_week",
                ],
            },
        ),
        source_spans=(
            {
                "field": "target_metric_refs[0]",
                "start": metric_start,
                "end": metric_start + len(metric_text),
                "text": metric_text,
            },
            {
                "field": "time_spec.target",
                "start": date_start,
                "end": date_start + len(TARGET_DATE),
                "text": TARGET_DATE,
            },
        ),
        schema_version="intent-revision.v1",
        prompt_version="single-authority-intent.v1",
        model_version="deepseek-v4-flash",
        known_goal_ids=set(registry.analysis_goal_ids),
        known_metric_ids=set(registry.metric_ids),
        known_analysis_axis_ids=set(registry.analysis_axis_ids),
        known_scope_types=set(registry.public_scope_types),
    )


def _decision_ledger(intent: IntentRevision) -> DecisionLedger:
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
    return DecisionLedger().append(decision)


def _goal_temporal_fixture(
    goal_id: str,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    if goal_id == "explain_change":
        return (
            {"kind": "date", "target": TARGET_DATE},
            {
                "kind": "fixed_window",
                "baseline_class": "prior_period",
                "baseline_start": "2026-06-18",
                "baseline_end": "2026-06-18",
                "aggregation": "sum_of_complete_days",
            },
            "single_day_window_pair",
        )
    if goal_id == "business_object_impact_review":
        return (
            {"kind": "custom", "expression": "6月活动前后"},
            {
                "kind": "event_relative_window",
                "event_ref": "business-event:campaign-june-2026",
                "target_start": "2026-06-16",
                "target_end": "2026-06-30",
                "baseline_start": "2026-06-01",
                "baseline_end": "2026-06-15",
                "aggregation": "sum_of_complete_days",
            },
            "event_relative",
        )
    if goal_id == "data_quality_or_evidence_review":
        return (
            {"kind": "date", "target": TARGET_DATE},
            {"kind": "none"},
            "target_only",
        )
    aggregate_windows = {
        "pattern_explanation": (
            "2026-04-01",
            "2026-06-30",
            "2026-01-01",
            "2026-03-31",
            "prior_period",
        ),
        "revenue_health_review": (
            "2026-06-01",
            "2026-06-30",
            "2026-05-01",
            "2026-05-31",
            "prior_period",
        ),
        "segment_or_factor_attribution": (
            "2026-06-01",
            "2026-06-30",
            "2026-05-01",
            "2026-05-31",
            "prior_period",
        ),
        "anomaly_or_black_swan_review": (
            "2026-06-16",
            "2026-06-30",
            "2026-06-01",
            "2026-06-15",
            "prior_period",
        ),
        "custom_baseline_comparison": (
            "2026-06-01",
            "2026-06-30",
            "2026-04-01",
            "2026-04-30",
            "custom_control_window",
        ),
    }
    try:
        target_start, target_end, baseline_start, baseline_end, baseline_class = (
            aggregate_windows[goal_id]
        )
    except KeyError as exc:
        raise AssertionError(f"launch_goal_temporal_fixture_missing:{goal_id}") from exc
    return (
        {"kind": "date_range", "start": target_start, "end": target_end},
        {
            "kind": "fixed_window",
            "baseline_class": baseline_class,
            "baseline_start": baseline_start,
            "baseline_end": baseline_end,
            "aggregation": "sum_of_complete_days",
        },
        "aggregate_window_pair",
    )


def test_temporal_authority_resolves_case_b_to_exact_daily_window_pair() -> None:
    registry = _registry()
    intent = _intent_revision(registry)

    authority = resolve_effective_comparison(
        time_spec=intent.time_spec,
        comparison_spec=intent.comparison_spec,
        decision_ledger=_decision_ledger(intent),
        require_physical_baseline=True,
    )

    assert authority.mode == "window_pair"
    assert authority.target_window.to_dict() == {
        "window_ref": TARGET_WINDOW_REF,
        "role": "target",
        "start": TARGET_DATE,
        "end": TARGET_DATE,
        "boundary": "inclusive",
        "aggregation": "sum_of_complete_days",
    }
    assert authority.baseline_window is not None
    assert authority.baseline_window.start == "2026-06-18"
    assert authority.baseline_window.end == "2026-06-18"
    assert authority.resolved_window_refs == (
        TARGET_WINDOW_REF,
        BASELINE_WINDOW_REF,
    )
    assert authority.authority_ref == (
        "temporal-comparison:sha256:" + authority.content_digest
    )
    assert authority.to_dict()["mode"] == "window_pair"


def test_calendar_partition_canonicalizes_member_sets_and_maps_same_month_phase() -> (
    None
):
    time_spec = {
        "kind": "date_range",
        "start": "2024-01-01",
        "end": "2026-06-30",
    }
    base_spec = {
        "kind": "calendar_partition",
        "baseline_class": "same_month_phase",
        "period_grain": "month",
        "partition_field": "month_phase",
        "target_members": ["start"],
        "baseline_members": ["end", "mid"],
        "aggregation": "mean_of_complete_days",
    }
    paraphrase_spec = {
        **base_spec,
        "baseline_members": ["mid", "end"],
    }

    first = resolve_effective_comparison(
        time_spec=time_spec,
        comparison_spec=base_spec,
        decision_ledger=DecisionLedger(),
        require_physical_baseline=False,
    )
    second = resolve_effective_comparison(
        time_spec=time_spec,
        comparison_spec=paraphrase_spec,
        decision_ledger=DecisionLedger(),
        require_physical_baseline=False,
    )

    assert first.mode == "calendar_partition"
    assert first.baseline_window is None
    assert first.calendar_partition == {
        "baseline_class": "same_month_phase",
        "period_grain": "month",
        "partition_field": "month_phase",
        "target_members": ("start",),
        "baseline_members": ("mid", "end"),
        "aggregation": "mean_of_complete_days",
    }
    assert first.content_digest == second.content_digest
    assert first.authority_ref == second.authority_ref
    pattern_contract = {
        "task_input_binding": {
            "payload_kind": "pattern",
            "pattern_mode": "intra_period",
        }
    }
    assert capability_supports_temporal_authority(pattern_contract, first) is False
    assert (
        capability_supports_temporal_authority(
            _registry().capability_inputs("compare_period_phases"),
            first,
        )
        is True
    )
    compiler = AuthoritativePlanCompiler(runtime_registry=_registry())
    assert (
        compiler._compile_context_window_specs(
            ({"capability_id": "compare_period_phases"},),
            temporal_authority=first,
        )
        == ()
    )


def test_only_required_primary_baseline_axis_forces_a_physical_window_pair() -> None:
    registry = _registry()
    base = _intent_revision(registry)
    intent = IntentRevision.create(
        run_attempt_id=base.run_attempt_id,
        original_user_text=base.original_user_text,
        goal_bindings=({"goal_id": "pattern_explanation", "role": "primary"},),
        target_metric_refs=base.target_metric_refs,
        scope=base.scope,
        time_spec={
            "kind": "date_range",
            "start": "2024-01-01",
            "end": "2026-06-30",
        },
        comparison_spec={
            "kind": "calendar_partition",
            "baseline_class": "same_month_phase",
            "period_grain": "month",
            "partition_field": "month_phase",
            "target_members": ["start"],
            "baseline_members": ["mid", "end"],
            "aggregation": "mean_of_complete_days",
        },
        direction_premise="unknown",
        requested_analysis_axes=(),
        desired_decisions=(),
        ambiguity_slots=(),
        source_spans=base.source_spans,
        schema_version=base.schema_version,
        prompt_version=base.prompt_version,
        model_version=base.model_version,
        known_goal_ids=set(registry.analysis_goal_ids),
        known_metric_ids=set(registry.metric_ids),
        known_analysis_axis_ids=set(registry.analysis_axis_ids),
        known_scope_types=set(registry.public_scope_types),
    )
    compiler = AuthoritativePlanCompiler(runtime_registry=registry)

    authority = compiler._effective_temporal_authority(
        intent_revision=intent,
        decision_ledger=DecisionLedger(),
        goal_axes={"change_validation": {"role": "auxiliary"}},
    )
    assert authority.mode == "calendar_partition"

    with pytest.raises(
        PlanAuthorityContractError,
        match="temporal_physical_baseline_required",
    ):
        compiler._effective_temporal_authority(
            intent_revision=intent,
            decision_ledger=DecisionLedger(),
            goal_axes={"change_validation": {"role": "required"}},
        )


def test_event_decision_slot_can_supply_the_only_physical_window_authority() -> None:
    time_spec = {"kind": "custom", "expression": "6月活动后"}
    comparison_spec = {
        "kind": "decision_slot",
        "slot_id": "event_relative_window",
    }
    unresolved = resolve_effective_comparison(
        time_spec=time_spec,
        comparison_spec=comparison_spec,
        decision_ledger=DecisionLedger(),
        require_physical_baseline=False,
    )
    assert unresolved.mode == "unresolved"
    assert unresolved.has_physical_target is False

    decision = DecisionRecord.create(
        intent_revision_id="intent-event",
        slot_id="event_relative_window",
        value={
            "kind": "event_relative_window",
            "event_ref": "business-event:campaign-june-2026",
            "target_start": "2026-06-16",
            "target_end": "2026-06-30",
            "baseline_start": "2026-06-01",
            "baseline_end": "2026-06-15",
            "aggregation": "sum_of_complete_days",
        },
        source="user",
        status="user_confirmed",
        materiality="material",
        affected_plan_fields=("resolved_window_refs",),
        option_id="event-relative.user-window",
    )
    resolved = resolve_effective_comparison(
        time_spec=time_spec,
        comparison_spec=comparison_spec,
        decision_ledger=DecisionLedger().append(decision),
        require_physical_baseline=True,
    )

    assert resolved.mode == "event_relative"
    assert resolved.source == "decision"
    assert resolved.event_ref == "business-event:campaign-june-2026"
    assert resolved.target_window.start == "2026-06-16"
    assert resolved.target_window.end == "2026-06-30"
    assert resolved.baseline_window is not None
    assert resolved.baseline_window.start == "2026-06-01"
    assert resolved.baseline_window.end == "2026-06-15"


def test_explicit_comparison_conflicts_with_a_ledger_comparison_decision() -> None:
    registry = _registry()
    intent = _intent_revision(registry)
    with pytest.raises(
        TemporalComparisonContractError,
        match="temporal_comparison_authority_conflict",
    ):
        resolve_effective_comparison(
            time_spec=intent.time_spec,
            comparison_spec={
                "kind": "fixed_window",
                "baseline_class": "prior_period",
                "baseline_start": "2026-06-18",
                "baseline_end": "2026-06-18",
                "aggregation": "sum_of_complete_days",
            },
            decision_ledger=_decision_ledger(intent),
            require_physical_baseline=True,
        )


def test_multi_day_window_pair_requires_explicit_aggregate_window_contract() -> None:
    authority = resolve_effective_comparison(
        time_spec={
            "kind": "date_range",
            "start": "2026-04-01",
            "end": "2026-06-30",
        },
        comparison_spec={
            "kind": "fixed_window",
            "baseline_class": "prior_period",
            "baseline_start": "2026-01-01",
            "baseline_end": "2026-03-31",
            "aggregation": "sum_of_complete_days",
        },
        decision_ledger=DecisionLedger(),
        require_physical_baseline=True,
    )
    binding = {"task_input_binding": {"payload_kind": "formula_graph"}}

    assert capability_supports_temporal_authority(binding, authority) is False
    assert (
        capability_supports_temporal_authority(
            _registry().capability_inputs("formula_decompose"),
            authority,
        )
        is True
    )


def _authority_context(
    registry: RuntimeContractRegistry,
    *,
    release_suffix: str = "r1",
    payment_attempt_status: str = "missing_contract",
) -> AuthorityContext:
    release_ref = f"release:paid-order-success:{release_suffix}"
    snapshot_ref = f"snapshot:paid-order-success:{release_suffix}"
    return AuthorityContext.create(
        run_attempt_id="run-attempt-phase02",
        actual_as_of="2026-07-17T08:00:00Z",
        release_refs=(release_ref,),
        snapshot_refs=(snapshot_ref,),
        dataset_coverage=(
            {
                "dataset_id": "paid_order_success",
                "availability": "claim_ready",
                "release_ref": release_ref,
                "snapshot_refs": (snapshot_ref,),
                "limitation_ref": None,
            },
            {
                "dataset_id": "payment_attempt",
                "availability": payment_attempt_status,
                "release_ref": None,
                "snapshot_refs": (),
                "limitation_ref": "limitation:payment-attempt-contract",
            },
            {
                "dataset_id": "external_event",
                "availability": "missing_contract",
                "release_ref": None,
                "snapshot_refs": (),
                "limitation_ref": "limitation:external-event-contract",
            },
            {
                "dataset_id": "internal_operation_event",
                "availability": "missing_contract",
                "release_ref": None,
                "snapshot_refs": (),
                "limitation_ref": "limitation:operation-event-contract",
            },
        ),
        contract_versions={
            "runtime_bindings": registry.contract_version,
            "runtime_bindings_digest": registry.source_payload_digest,
        },
    )


def _planner_proposal(
    intent: IntentRevision,
    authority_context: AuthorityContext,
    decision_refs: tuple[str, ...],
    *,
    include_unknown_axis: bool = False,
) -> PlannerProposal:
    auxiliary_axis_proposals: list[dict[str, Any]] = [
        {
            "proposal_item_id": "proposal-axis-business-context",
            "axis_id": "business_context",
            "rationale": "检查活动与运营事件是否能解释同期变化。",
            "supports_claim_kinds": ("candidate_mechanism",),
        }
    ]
    if include_unknown_axis:
        auxiliary_axis_proposals.append(
            {
                "proposal_item_id": "proposal-axis-invented",
                "axis_id": "invented_axis",
                "rationale": "探索一个合同中尚不存在的辅助分析轴。",
                "supports_claim_kinds": ("candidate_mechanism",),
            }
        )
    return PlannerProposal.create(
        run_attempt_id=intent.run_attempt_id,
        intent_revision_id=intent.intent_revision_id,
        decision_refs=decision_refs,
        authority_context_ref=authority_context.authority_context_ref,
        issue_tree=(
            {
                "issue_id": "issue-primary-change",
                "parent_issue_id": None,
                "question": "实际变化多大，哪些因素共同解释这次变化？",
                "target_claim_kind": "comparative_change",
            },
        ),
        hypotheses=(
            {
                "proposal_item_id": "hypothesis-event-overlap",
                "statement": "运营活动可能与变化窗口重叠，但需要事件证据验证。",
                "target_claim_kind": "candidate_mechanism",
                "requested_axis_ids": (
                    "invented_axis" if include_unknown_axis else "business_context",
                ),
                "assumption_refs": (),
            },
        ),
        auxiliary_axes=tuple(auxiliary_axis_proposals),
        priority_proposals=(
            {
                "proposal_item_id": "proposal-priority-change",
                "target_ref": "change_validation",
                "rationale": "先确认变化方向和幅度。",
            },
            {
                "proposal_item_id": "proposal-priority-formula",
                "target_ref": "formula_tree",
                "rationale": "再检查公式因素贡献。",
            },
            {
                "proposal_item_id": "proposal-priority-dimension",
                "target_ref": "dimension_localization",
                "rationale": "随后定位分群贡献。",
            },
            {
                "proposal_item_id": "proposal-priority-time",
                "target_ref": "time_context",
                "rationale": "补充时间背景。",
            },
            {
                "proposal_item_id": "proposal-priority-business-context",
                "target_ref": "business_context",
                "rationale": "最后核查业务事件候选解释。",
            },
        ),
        assumption_proposals=(),
        raw_provider_response_ref="restricted-provider-response:phase02",
        schema_version="planner-proposal.v1",
        prompt_version="single-authority-plan-proposal.v1",
        model_version="deepseek-v4-flash",
    )


def _compile_plan(
    *,
    include_unknown_axis: bool = False,
    authority_context: AuthorityContext | None = None,
    supersedes_plan_revision: PlanRevision | None = None,
    registry: RuntimeContractRegistry | None = None,
) -> tuple[
    RuntimeContractRegistry,
    IntentRevision,
    AuthorityContext,
    PlannerProposal,
    ProposalAdmissionRecord,
    PlanRevision,
]:
    registry = registry or _registry()
    intent = _intent_revision(registry)
    context = authority_context or _authority_context(registry)
    ledger = _decision_ledger(intent)
    decision_refs = tuple(record.decision_id for record in ledger.records)
    proposal = _planner_proposal(
        intent,
        context,
        decision_refs,
        include_unknown_axis=include_unknown_axis,
    )
    compiler = AuthoritativePlanCompiler(runtime_registry=registry)
    result = compiler.compile(
        intent_revision=intent,
        decision_ledger=ledger,
        authority_context=context,
        planner_proposal=proposal,
        supersedes_plan_revision=supersedes_plan_revision,
    )
    return (
        registry,
        intent,
        context,
        proposal,
        result.proposal_admission,
        result.plan_revision,
    )


def _assert_content_addressed_roundtrip(record: Any, *, id_field: str) -> None:
    payload = record.to_dict()
    assert getattr(record, id_field)
    assert len(record.content_digest) == 64
    assert type(record).from_dict(payload) == record

    tampered = record.to_dict()
    tampered["content_digest"] = "0" * 64
    with pytest.raises(ValueError):
        type(record).from_dict(tampered)


def _assert_acyclic(tasks: tuple[CapabilityTask, ...]) -> None:
    task_ids = {task.task_id for task in tasks}
    assert len(task_ids) == len(tasks)
    assert all(
        dependency_id in task_ids
        for task in tasks
        for dependency_id in task.dependency_task_ids
    )

    dependencies = {task.task_id: set(task.dependency_task_ids) for task in tasks}
    visited: set[str] = set()
    while dependencies:
        ready = {
            task_id
            for task_id, dependency_ids in dependencies.items()
            if dependency_ids <= visited
        }
        assert ready, "capability_task_dag_contains_cycle"
        visited.update(ready)
        for task_id in ready:
            dependencies.pop(task_id)


def test_phase02_records_are_frozen_content_addressed_and_roundtrip() -> None:
    registry = _registry()
    intent = _intent_revision(registry)
    context = _authority_context(registry)
    ledger = _decision_ledger(intent)
    decision_refs = tuple(record.decision_id for record in ledger.records)
    proposal = _planner_proposal(intent, context, decision_refs)
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
            evidence_kinds=("verified_observation",),
        ),
        success_policy={
            "policy": "verified_or_explicit_boundary",
            "minimum_claim_strength": "directional",
        },
    )
    axis_contract = registry.analysis_axis("change_validation")
    axis = AnalysisAxis.create(
        axis_id="change_validation",
        role="required",
        axis_kind=axis_contract["axis_kind"],
        target_metric_refs=("paid_amount",),
        metric_refs=tuple(axis_contract["metric_refs"]),
        dimension_refs=tuple(axis_contract["dimension_refs"]),
        context_source_refs=tuple(axis_contract["context_source_refs"]),
        capability_refs=tuple(axis_contract["capability_refs"]),
        reconciliation_group=axis_contract["reconciliation_group"],
        selection_policy=axis_contract["selection_policy"],
        source_refs=tuple(axis_contract["source_refs"]),
        goal_refs=("explain_change",),
    )
    admission = ProposalAdmissionRecord.create(
        planner_proposal_ref=proposal.planner_proposal_id,
        intent_revision_id=intent.intent_revision_id,
        decision_refs=decision_refs,
        authority_context_ref=context.authority_context_ref,
        admission_entries=(
            {
                "proposal_item_ref": "proposal-axis-business-context",
                "item_kind": "analysis_axis",
                "status": "admitted",
                "reason_code": "supported_auxiliary_axis",
                "contract_refs": ["clickhouse-analysis-bindings#business_context"],
                "normalized_execution_ref": "business_context",
            },
        ),
        compiler_version="single-authority-plan-compiler.v1",
        contract_versions=dict(context.contract_versions),
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
        decision_refs=decision_refs,
        authority_context_ref=context.authority_context_ref,
        temporal_authority=temporal_authority,
        resolved_window_refs=temporal_authority.resolved_window_refs,
        context_window_specs=(),
        claim_obligations=(obligation,),
        analysis_axes=(axis,),
        capability_task_specs=(
            {
                "task_key": "primary-comparison",
                "capability_id": "compare_periods",
                "normalized_input_refs": (
                    TARGET_WINDOW_REF,
                    BASELINE_WINDOW_REF,
                    "metric:paid_amount",
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
                    "degradation_policy": {
                        "missing_required_input": "block_claim",
                    },
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
        budget_policy_ref="budget-policy:default",
        contract_versions=context.contract_versions,
        planner_proposal_ref=proposal.planner_proposal_id,
        proposal_admission_ref=admission.proposal_admission_id,
    )

    records_and_ids = (
        (context, "authority_context_ref"),
        (proposal, "planner_proposal_id"),
        (obligation, "obligation_id"),
        (axis, "analysis_axis_ref"),
        (admission, "proposal_admission_id"),
        (plan, "plan_revision_id"),
    )
    for record, id_field in records_and_ids:
        _assert_content_addressed_roundtrip(record, id_field=id_field)
        dataclass_field = next(
            field for field in fields(record) if field.name == id_field
        )
        with pytest.raises(FrozenInstanceError):
            setattr(record, dataclass_field.name, "mutated")

    assert (
        CapabilityTask.from_dict(
            plan.capability_tasks[0].to_dict(),
            contract_versions=plan.contract_versions,
        )
        == (plan.capability_tasks[0])
    )


def test_case_b_plan_derives_the_complete_mandatory_analysis_spine() -> None:
    registry, _, context, _, _, plan = _compile_plan()

    axes = {axis.axis_id: axis for axis in plan.analysis_axes}
    assert {
        "change_validation",
        "formula_tree",
        "dimension_localization",
        "time_context",
        "data_quality",
    }.issubset(axes)
    assert set(axes["formula_tree"].metric_refs) == set(
        registry.analysis_axis("formula_tree")["metric_refs"]
    )
    assert set(axes["dimension_localization"].dimension_refs) == set(
        registry.analysis_axis("dimension_localization")["dimension_refs"]
    )
    assert "payment_success_rate" in axes["formula_tree"].metric_refs

    claim_kinds = {item.claim_kind for item in plan.claim_obligations}
    assert {
        "comparative_change",
        "formula_component_contribution",
        "segment_contribution_or_mix_shift",
        "contract_coverage_and_trust_boundary",
    }.issubset(claim_kinds)

    capability_ids = {task.capability_id for task in plan.capability_tasks}
    assert {
        "compare_periods",
        "formula_decompose",
        "candidate_dimension_screen",
        "metric_timeseries",
        "rolling_window_compare",
        "data_quality_profile",
    }.issubset(capability_ids)
    assert "answer_verify" not in capability_ids
    assert "evidence_reduce" not in capability_ids
    assert plan.authority_context_ref == context.authority_context_ref
    assert set(plan.resolved_window_refs) == {
        TARGET_WINDOW_REF,
        BASELINE_WINDOW_REF,
    }
    assert plan.executable is True


def test_context_window_execution_defaults_are_method_aligned() -> None:
    registry = _registry()
    expected = {
        "cross_source_association": ("day", 180),
        "cross_source_panel_association": ("day", 180),
        "compare_period_phases": ("month", 24),
        "outlier_scan": ("day", 28),
        "change_point_scan": ("day", 8),
        "rolling_window_compare": ("day", 10),
    }

    for capability_id, (unit, count) in expected.items():
        contract = registry.capability_inputs(capability_id)
        policy = contract["context_window_policy"]
        assert policy["execution_default"] == {"unit": unit, "count": count}
        assert unit in policy["allowed_units"]
        lower, upper = policy["count_bounds"][unit]
        assert lower <= count <= upper

    assert registry.capability_inputs("rolling_window_compare")["task_input_binding"][
        "parameters"
    ] == {
        "materiality_floor": 0.0,
        "rolling_span_policy": "target_window_duration_with_minimum",
        "minimum_span_days": 3,
        "rolling_step_policy": "target_window_duration",
        "min_periods": 8,
    }
    assert (
        registry.capability_inputs("market_channel_context")["task_input_binding"][
            "parameters"
        ]["required_window_presence"]
        == "reconciled_zero_fill"
    )
    assert (
        registry.capability_inputs("compare_period_phases")["task_input_binding"][
            "parameters"
        ]["min_periods"]
        == 24
    )
    assert (
        registry.capability_inputs("change_point_scan")["task_input_binding"][
            "parameters"
        ]["min_total_samples"]
        == 8
    )


def test_context_window_execution_default_must_satisfy_policy_bounds() -> None:
    payload = load_contract(CANONICAL_RUNTIME_BINDINGS_PATH)
    payload["capability_inputs"]["change_point_scan"]["context_window_policy"][
        "execution_default"
    ] = {"unit": "day", "count": 7}

    with pytest.raises(
        ValueError,
        match=(
            "runtime_context_window_policy_invalid:change_point_scan:"
            "execution_default:policy"
        ),
    ):
        RuntimeContractRegistry(payload)


def test_plan_context_window_specs_are_typed_and_bound_to_owner_tasks() -> None:
    registry, _, _, _, _, plan = _compile_plan()
    task_capabilities = {task.capability_id for task in plan.capability_tasks}
    expected_capabilities = {
        capability_id
        for capability_id in task_capabilities
        if "context_window_policy" in registry.capability_inputs(capability_id)
    }

    assert {
        spec.capability_id for spec in plan.context_window_specs
    } == expected_capabilities
    assert len(plan.context_window_specs) == len(expected_capabilities)
    for spec in plan.context_window_specs:
        assert set(spec.to_dict()) == {
            "capability_id",
            "relation",
            "unit",
            "count",
        }
        policy = registry.capability_inputs(spec.capability_id)["context_window_policy"]
        assert spec.relation == policy["relation"]
        assert {"unit": spec.unit, "count": spec.count} == policy["execution_default"]
        owner_tasks = tuple(
            task
            for task in plan.capability_tasks
            if task.capability_id == spec.capability_id
        )
        assert owner_tasks
        assert all(
            spec.normalized_input_ref in task.normalized_input_refs
            for task in owner_tasks
        )
        assert all(
            spec.normalized_input_ref not in task.normalized_input_refs
            for task in plan.capability_tasks
            if task.capability_id != spec.capability_id
        )

    assert PlanRevision.from_dict(plan.to_dict()) == plan
    if plan.context_window_specs:
        tampered = plan.to_dict()
        tampered["context_window_specs"].append(
            dict(tampered["context_window_specs"][0])
        )
        with pytest.raises(
            ValueError,
            match="plan_revision_context_window_capability_duplicated",
        ):
            PlanRevision.from_dict(tampered)
    missing = plan.to_dict()
    missing.pop("context_window_specs")
    with pytest.raises(ValueError, match="plan_revision_shape_invalid"):
        PlanRevision.from_dict(missing)


def test_planner_proposal_is_additive_and_cannot_remove_mandatory_obligations() -> None:
    _, _, _, proposal, _, plan = _compile_plan()

    assert proposal.auxiliary_axes == (
        {
            "proposal_item_id": "proposal-axis-business-context",
            "axis_id": "business_context",
            "rationale": "检查活动与运营事件是否能解释同期变化。",
            "supports_claim_kinds": ("candidate_mechanism",),
        },
    )
    axis_ids = {axis.axis_id for axis in plan.analysis_axes}
    assert {
        "change_validation",
        "formula_tree",
        "dimension_localization",
        "time_context",
        "data_quality",
    }.issubset(axis_ids)
    assert all(
        obligation.role == "user_required"
        for obligation in plan.claim_obligations
        if obligation.claim_kind
        in {
            "comparative_change",
            "formula_component_contribution",
            "segment_contribution_or_mix_shift",
            "contract_coverage_and_trust_boundary",
        }
    )


def test_primary_and_supporting_goals_merge_into_one_plan_revision() -> None:
    registry = _registry()
    base = _intent_revision(registry)
    intent = IntentRevision.create(
        run_attempt_id=base.run_attempt_id,
        original_user_text=base.original_user_text,
        goal_bindings=(
            {"goal_id": "explain_change", "role": "primary"},
            {
                "goal_id": "data_quality_or_evidence_review",
                "role": "supporting",
            },
        ),
        target_metric_refs=base.target_metric_refs,
        scope=base.scope,
        time_spec=base.time_spec,
        comparison_spec=base.comparison_spec,
        direction_premise=base.direction_premise,
        requested_analysis_axes=(),
        desired_decisions=base.desired_decisions,
        ambiguity_slots=base.ambiguity_slots,
        source_spans=base.source_spans,
        schema_version=base.schema_version,
        prompt_version=base.prompt_version,
        model_version=base.model_version,
        known_goal_ids=set(registry.analysis_goal_ids),
        known_metric_ids=set(registry.metric_ids),
        known_analysis_axis_ids=set(registry.analysis_axis_ids),
        known_scope_types=set(registry.public_scope_types),
    )
    ledger = _decision_ledger(intent)
    context = _authority_context(registry)
    proposal = _planner_proposal(
        intent,
        context,
        tuple(record.decision_id for record in ledger.active_records()),
    )

    compiled = AuthoritativePlanCompiler(runtime_registry=registry).compile(
        intent_revision=intent,
        decision_ledger=ledger,
        authority_context=context,
        planner_proposal=proposal,
    )
    plan = compiled.plan_revision

    assert plan.executable is True
    assert len(plan.analysis_axes) == len({axis.axis_id for axis in plan.analysis_axes})
    assert {goal_ref for axis in plan.analysis_axes for goal_ref in axis.goal_refs} >= {
        "explain_change",
        "data_quality_or_evidence_review",
    }
    trust_obligation = next(
        obligation
        for obligation in plan.claim_obligations
        if obligation.claim_kind == "contract_coverage_and_trust_boundary"
    )
    assert set(trust_obligation.subject["goal_refs"]) == {
        "explain_change",
        "data_quality_or_evidence_review",
    }


def test_auxiliary_proposals_are_admitted_or_omitted_without_clarification() -> None:
    _, _, _, proposal, admission, plan = _compile_plan(include_unknown_axis=True)

    admissions = {
        item["proposal_item_ref"]: item for item in admission.admission_entries
    }
    admitted = admissions["proposal-axis-business-context"]
    assert admitted["status"] == "admitted"
    assert admitted["normalized_execution_ref"] == "business_context"
    unknown = admissions["proposal-axis-invented"]
    assert unknown["status"] == "rejected"
    assert unknown["reason_code"] == "unknown_axis_ref"
    assert unknown.get("normalized_execution_ref") is None
    assert "invented_axis" not in {axis.axis_id for axis in plan.analysis_axes}
    assert plan.executable is True
    assert "clarification" not in str(unknown).lower()

    hypothesis_text = "运营活动可能与变化窗口重叠，但需要事件证据验证。"
    assert proposal.hypotheses[0]["statement"] == hypothesis_text
    restored = PlannerProposal.from_dict(proposal.to_dict())
    assert restored.hypotheses[0]["statement"] == hypothesis_text
    assert plan.planner_proposal_ref == proposal.planner_proposal_id
    assert plan.proposal_admission_ref == admission.proposal_admission_id


def test_capability_tasks_form_a_dag_and_requiredness_lives_on_edges() -> None:
    _, _, _, _, _, plan = _compile_plan()

    _assert_acyclic(plan.capability_tasks)
    obligation_ids = {obligation.obligation_id for obligation in plan.claim_obligations}
    supported_obligation_ids: set[str] = set()
    for task in plan.capability_tasks:
        payload = task.to_dict()
        assert "required" not in payload
        assert "is_required" not in payload
        assert "role" not in payload
        edge_refs = tuple(edge["obligation_id"] for edge in task.obligation_edges)
        assert task.supports_obligation_ids == edge_refs
        assert all(
            set(edge) == {"obligation_id", "required"}
            and type(edge["required"]) is bool
            for edge in task.obligation_edges
        )
        assert set(edge_refs) <= obligation_ids
        supported_obligation_ids.update(edge_refs)
    assert supported_obligation_ids == obligation_ids


def test_optional_payment_success_unavailability_keeps_plan_executable() -> None:
    registry = _registry()
    context = _authority_context(
        registry,
        payment_attempt_status="missing_contract",
    )
    _, _, _, _, _, plan = _compile_plan(authority_context=context)

    assert plan.executable is True
    formula_axis = next(
        axis for axis in plan.analysis_axes if axis.axis_id == "formula_tree"
    )
    assert "payment_success_rate" in formula_axis.metric_refs
    assert any(
        task.capability_id == "formula_decompose" for task in plan.capability_tasks
    )
    payment_coverage = next(
        item
        for item in context.dataset_coverage
        if item["dataset_id"] == "payment_attempt"
    )
    assert payment_coverage["availability"] == "missing_contract"


def test_plan_change_inherits_the_pinned_authority_context() -> None:
    registry, _, context, _, _, first = _compile_plan()
    _, _, _, _, _, patched = _compile_plan(
        authority_context=context,
        supersedes_plan_revision=first,
    )

    assert (
        first.budget_policy_ref == registry.exploration_budget_policy.budget_policy_ref
    )
    assert patched.supersedes_plan_revision_id == first.plan_revision_id
    assert patched.authority_context_ref == first.authority_context_ref
    assert patched.budget_policy_ref == first.budget_policy_ref

    different_context = _authority_context(registry, release_suffix="r2")
    with pytest.raises(ValueError, match="authority_context"):
        _compile_plan(
            authority_context=different_context,
            supersedes_plan_revision=first,
        )


def test_plan_change_rejects_a_different_budget_policy() -> None:
    registry, _, context, _, _, first = _compile_plan()
    payload = load_contract(CANONICAL_RUNTIME_BINDINGS_PATH)
    payload["exploration_budget_policy"]["auxiliary_budget_limit"] = 1
    changed_registry = RuntimeContractRegistry(payload)

    assert (
        changed_registry.exploration_budget_policy.budget_policy_ref
        != registry.exploration_budget_policy.budget_policy_ref
    )
    with pytest.raises(
        ValueError,
        match="plan_compiler_supersedes_budget_policy_mismatch",
    ):
        _compile_plan(
            registry=changed_registry,
            authority_context=context,
            supersedes_plan_revision=first,
            include_unknown_axis=True,
        )


def test_callers_cannot_inject_requested_nodes_into_the_authoritative_plan() -> None:
    conversation_request_fields = {
        field.name for field in fields(ConversationRunRequest)
    }
    compile_parameters = inspect.signature(AuthoritativePlanCompiler.compile).parameters

    assert "requested_nodes" not in conversation_request_fields
    assert "requested_nodes" not in compile_parameters


def test_live_graph_has_one_plan_compiler_and_no_route_repair_fallback() -> None:
    graph_source = inspect.getsource(langgraph_workflow.build_single_authority_graph)
    workflow_source = Path(langgraph_workflow.__file__).read_text(encoding="utf-8")

    assert "compile_authoritative_plan" in graph_source
    assert "design_analysis_route" not in graph_source
    assert "accept_analysis_route" not in graph_source
    assert "repair_analysis_route" not in graph_source
    assert "from bi_agent.runtime.compiler import compile_graph" not in (
        workflow_source
    )
    assert "compile_graph(" not in workflow_source
    assert not (ROOT / "bi_agent/runtime/compiler.py").exists()


def _planner_provider_output(
    intent: IntentRevision,
    context: AuthorityContext,
    decision_refs: tuple[str, ...],
) -> dict[str, Any]:
    proposal = _planner_proposal(intent, context, decision_refs)
    return {
        "issue_tree": canonical_value(proposal.issue_tree),
        "auxiliary_axes": canonical_value(proposal.auxiliary_axes),
        "hypotheses": canonical_value(proposal.hypotheses),
        "priority_proposals": canonical_value(proposal.priority_proposals),
        "assumption_proposals": canonical_value(proposal.assumption_proposals),
    }


class _PlannerLLM:
    supports_output_validator = True
    supports_model_tier = True
    supports_thinking_mode = True
    model = "phase02-planner-test-model"

    def __init__(
        self,
        output: dict[str, Any],
        *,
        allow_calls: bool = True,
        audit_model: str | None = None,
    ):
        self.output = deepcopy(output)
        self.allow_calls = allow_calls
        self.audit_model = audit_model or self.model
        self.calls: list[dict[str, Any]] = []

    def invoke_json(self, **kwargs: Any) -> LLMResult:
        if not self.allow_calls:
            raise AssertionError("planner_llm_must_not_be_reinvoked")
        self.calls.append(dict(kwargs))
        validator = kwargs.get("output_validator")
        if callable(validator):
            validator(self.output)
        raw_response = json.dumps(
            self.output,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return LLMResult(
            output=deepcopy(self.output),
            audit={
                "task": kwargs["task"],
                "provider": "phase02-test-provider",
                "model": self.audit_model,
                "prompt_version": kwargs["prompt_version"],
                "response_id": "phase02-planner-response-1",
                "raw_response_content": raw_response,
                "structured_output": deepcopy(self.output),
                "usage": {},
            },
        )


class _Phase02AuthorityStore:
    def __init__(self, ledger: DecisionLedger, *, fail_save_once: bool = False):
        self.ledger = ledger
        self.attempt_journal = InMemoryDurableCallJournal()
        self.fail_save_once = fail_save_once
        self.authority_context: AuthorityContext | None = None
        self.planner_proposal: PlannerProposal | None = None
        self.proposal_admission: ProposalAdmissionRecord | None = None
        self.plan_revision: PlanRevision | None = None
        self.transition: DurableTransition | None = None
        self.transition_input: dict[str, Any] | None = None
        self.transition_output: dict[str, Any] | None = None
        self.save_calls = 0
        self.runs: dict[str, dict[str, Any]] = {}
        self.audit_events: list[dict[str, Any]] = []

    def load_decision_ledger(self, intent_revision_id: str) -> DecisionLedger:
        assert all(
            record.intent_revision_id == intent_revision_id
            for record in self.ledger.records
        )
        return self.ledger

    def list_dataset_snapshots(self) -> tuple[Any, ...]:
        return ()

    def load_authority_context(self, run_attempt_id: str) -> AuthorityContext | None:
        if (
            self.authority_context is not None
            and self.authority_context.run_attempt_id != run_attempt_id
        ):
            return None
        return self.authority_context

    def resolve_active_plan_revision(self, run_attempt_id: str) -> PlanRevision | None:
        if (
            self.plan_revision is not None
            and self.plan_revision.run_attempt_id != run_attempt_id
        ):
            return None
        return self.plan_revision

    def load_planner_proposal(self, planner_proposal_id: str) -> PlannerProposal | None:
        if (
            self.planner_proposal is None
            or self.planner_proposal.planner_proposal_id != planner_proposal_id
        ):
            return None
        return self.planner_proposal

    def load_proposal_admission(
        self, proposal_admission_id: str
    ) -> ProposalAdmissionRecord | None:
        if (
            self.proposal_admission is None
            or self.proposal_admission.proposal_admission_id != proposal_admission_id
        ):
            return None
        return self.proposal_admission

    def save_plan_revision_transition(self, **kwargs: Any) -> dict[str, Any]:
        self.save_calls += 1
        if self.fail_save_once:
            self.fail_save_once = False
            raise RuntimeError("injected_plan_transition_write_failure")
        self.authority_context = kwargs["authority_context"]
        self.planner_proposal = kwargs["planner_proposal"]
        self.proposal_admission = kwargs["proposal_admission"]
        self.plan_revision = kwargs["plan_revision"]
        self.transition = kwargs["transition"]
        self.transition_input = canonical_value(kwargs["input_payload"])
        self.transition_output = canonical_value(kwargs["output_payload"])
        assert self.transition.input_digest == canonical_digest(self.transition_input)
        assert self.transition.output_digest == canonical_digest(self.transition_output)
        self.attempt_journal.bind_stage(
            run_attempt_id=self.transition.run_attempt_id,
            transition_attempt_id=self.transition.attempt_id,
            stage_name=self.transition.node_name,
            attempt_refs=kwargs["accepted_attempt_refs"],
        )
        return {"replayed": False}

    def load_accepted_transition(
        self,
        *,
        run_attempt_id: str,
        node_name: str,
        input_digest: str,
    ) -> dict[str, Any] | None:
        if (
            self.transition is None
            or self.transition.run_attempt_id != run_attempt_id
            or self.transition.node_name != node_name
            or self.transition.input_digest != input_digest
        ):
            return None
        return {
            "transition": self.transition,
            "input_payload": deepcopy(self.transition_input),
            "output_payload": deepcopy(self.transition_output),
        }

    def upsert_run(self, run_id: str, **record: Any) -> None:
        self.runs[run_id] = deepcopy(record)

    def add_audit_event(self, event_type: str, **record: Any) -> None:
        self.audit_events.append({"event_type": event_type, **deepcopy(record)})


def _phase02_compile_state(
    *,
    intent: IntentRevision,
    ledger: DecisionLedger,
    registry: RuntimeContractRegistry,
    store: _Phase02AuthorityStore,
    llm_client: _PlannerLLM,
) -> dict[str, Any]:
    return {
        "request": {
            "authority_store": store,
            "release_resolver": store,
            "runtime_registry": registry,
        },
        "run_id": intent.run_attempt_id,
        "intent_revision": intent.to_dict(),
        "decision_ledger_position": ledger.position,
        "checkpoint_events": [],
        "llm_calls": [],
        "llm_client": llm_client,
    }


def test_live_plan_compile_persists_raw_audit_resumes_without_planner_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    intent = _intent_revision(registry)
    ledger = _decision_ledger(intent)
    context = _authority_context(registry)
    decision_refs = tuple(record.decision_id for record in ledger.active_records())
    provider_output = _planner_provider_output(intent, context, decision_refs)
    planner = _PlannerLLM(provider_output)
    store = _Phase02AuthorityStore(ledger)
    state = _phase02_compile_state(
        intent=intent,
        ledger=ledger,
        registry=registry,
        store=store,
        llm_client=planner,
    )
    state["request"]["stop_after_phase"] = "phase02"
    monkeypatch.setattr(
        langgraph_workflow,
        "resolve_latest_authority_context",
        lambda **_: context,
    )

    planned = langgraph_workflow._compile_authoritative_plan(state)

    assert planned["workflow_status"] == "planned"
    assert "answer_package" not in planned
    assert len(planner.calls) == 1
    assert store.save_calls == 1
    assert store.transition_output is not None
    planner_audit = store.transition_output["planner_llm_audit"]
    assert planner_audit["raw_response_content"]
    assert store.planner_proposal is not None
    assert store.planner_proposal.raw_provider_response_ref == (
        "restricted-provider-response:sha256:"
        + sha256(planner_audit["raw_response_content"].encode("utf-8")).hexdigest()
    )

    no_reinvoke = _PlannerLLM(provider_output, allow_calls=False)
    resumed = langgraph_workflow._compile_authoritative_plan(
        _phase02_compile_state(
            intent=intent,
            ledger=ledger,
            registry=registry,
            store=store,
            llm_client=no_reinvoke,
        )
    )

    assert no_reinvoke.calls == []
    assert store.save_calls == 1
    assert resumed["plan_revision"] == planned["plan_revision"]
    assert resumed["authority_context"] == planned["authority_context"]
    assert any(
        audit.get("task") == "single_authority_plan_proposal"
        for audit in resumed["llm_calls"]
    )

    plan_result = resumed["plan_result"]
    finalized = _finalize_authoritative_plan(
        store=store,
        plan_result=plan_result,
        run_id=intent.run_attempt_id,
        thread_id="thread-phase02",
        turn_id="turn-phase02",
        topic_id="topic-phase02",
        request={
            "question": intent.original_user_text,
            "stop_after_phase": "phase02",
        },
        context_manifest={"manifest_id": "manifest-phase02"},
        turn_intent="new_topic",
        topic_relation="new_topic",
        llm_calls=tuple(resumed["llm_calls"]),
    )

    assert finalized["status"] == "planned"
    persisted_request = store.runs[intent.run_attempt_id]["request"]
    assert set(persisted_request["plan_result_refs"]) == {
        "schema_version",
        "intent_revision_id",
        "authority_context_ref",
        "planner_proposal_id",
        "proposal_admission_id",
        "plan_revision_id",
        "accepted_transition_id",
        "plan_patch_ref",
    }
    assert "plan_result" not in persisted_request
    assert "planner_proposal" not in persisted_request
    assert any(
        event["event_type"] == "authoritative_plan_accepted"
        for event in store.audit_events
    )


def test_plan_provider_success_replays_after_transition_write_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    intent = _intent_revision(registry)
    ledger = _decision_ledger(intent)
    context = _authority_context(registry)
    decision_refs = tuple(record.decision_id for record in ledger.active_records())
    provider_output = _planner_provider_output(intent, context, decision_refs)
    planner = _PlannerLLM(provider_output)
    store = _Phase02AuthorityStore(ledger, fail_save_once=True)
    monkeypatch.setattr(
        langgraph_workflow,
        "resolve_latest_authority_context",
        lambda **_: context,
    )

    with pytest.raises(
        langgraph_workflow.WorkflowFailure,
        match="injected_plan_transition_write_failure",
    ):
        langgraph_workflow._compile_authoritative_plan(
            _phase02_compile_state(
                intent=intent,
                ledger=ledger,
                registry=registry,
                store=store,
                llm_client=planner,
            )
        )

    planned = langgraph_workflow._compile_authoritative_plan(
        _phase02_compile_state(
            intent=intent,
            ledger=ledger,
            registry=registry,
            store=store,
            llm_client=planner,
        )
    )

    assert len(planner.calls) == 1
    assert store.save_calls == 2
    transition = DurableTransition.from_dict(planned["durable_checkpoint"])
    refs = store.attempt_journal.load_stage_attempt_refs(
        run_attempt_id=intent.run_attempt_id,
        transition_attempt_id=transition.attempt_id,
        stage_name="compile_authoritative_plan",
    )
    assert len(refs) == 1


def test_active_plan_with_wrong_routed_model_fails_closed_without_planner_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    intent = _intent_revision(registry)
    ledger = _decision_ledger(intent)
    context = _authority_context(registry)
    decision_refs = tuple(record.decision_id for record in ledger.active_records())
    provider_output = _planner_provider_output(intent, context, decision_refs)
    store = _Phase02AuthorityStore(ledger)
    monkeypatch.setattr(
        langgraph_workflow,
        "resolve_latest_authority_context",
        lambda **_: context,
    )
    langgraph_workflow._compile_authoritative_plan(
        _phase02_compile_state(
            intent=intent,
            ledger=ledger,
            registry=registry,
            store=store,
            llm_client=_PlannerLLM(provider_output),
        )
    )
    assert store.planner_proposal is not None
    assert store.transition is not None
    assert store.transition_output is not None

    routed_model = "phase02-critical-routed-model"
    stored_audit = {
        **store.transition_output["planner_llm_audit"],
        "model": routed_model,
    }
    store.transition_output = {
        **store.transition_output,
        "planner_llm_audit": stored_audit,
    }
    stored_transition = store.transition
    store.transition = DurableTransition.create(
        node_name=stored_transition.node_name,
        parent_transition_id=stored_transition.parent_transition_id,
        run_attempt_id=stored_transition.run_attempt_id,
        intent_revision_id=stored_transition.intent_revision_id,
        decision_ledger_position=(stored_transition.decision_ledger_position),
        input_digest=stored_transition.input_digest,
        output_digest=canonical_digest(store.transition_output),
        execution_attempt=stored_transition.execution_attempt,
        provider_ref=stored_transition.provider_ref,
        model_ref=routed_model,
        status=stored_transition.status,
        acceptance_state=stored_transition.acceptance_state,
        next_transition=stored_transition.next_transition,
        started_at=stored_transition.started_at,
        finished_at=stored_transition.finished_at,
    )
    no_reinvoke = _PlannerLLM(provider_output, allow_calls=False)

    with pytest.raises(
        langgraph_workflow.WorkflowFailure,
        match="accepted_plan_provider_audit_mismatch",
    ):
        langgraph_workflow._compile_authoritative_plan(
            _phase02_compile_state(
                intent=intent,
                ledger=ledger,
                registry=registry,
                store=store,
                llm_client=no_reinvoke,
            )
        )

    assert no_reinvoke.calls == []
    assert store.save_calls == 1


def test_plan_proposal_binds_the_actual_routed_provider_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    intent = _intent_revision(registry)
    ledger = _decision_ledger(intent)
    context = _authority_context(registry)
    decision_refs = tuple(record.decision_id for record in ledger.active_records())
    planner = _PlannerLLM(
        _planner_provider_output(intent, context, decision_refs),
        audit_model="phase02-critical-routed-model",
    )
    store = _Phase02AuthorityStore(ledger)
    monkeypatch.setattr(
        langgraph_workflow,
        "resolve_latest_authority_context",
        lambda **_: context,
    )

    planned = langgraph_workflow._compile_authoritative_plan(
        _phase02_compile_state(
            intent=intent,
            ledger=ledger,
            registry=registry,
            store=store,
            llm_client=planner,
        )
    )
    parsed = parse_authoritative_plan_result(
        planned["plan_result"],
        expected_run_id=intent.run_attempt_id,
        expected_llm_calls=planned["llm_calls"],
    )

    assert parsed.planner_proposal.model_version == ("phase02-critical-routed-model")
    assert parsed.transition.model_ref == "phase02-critical-routed-model"
    finalized = _finalize_authoritative_plan(
        store=store,
        plan_result=planned["plan_result"],
        run_id=intent.run_attempt_id,
        thread_id="thread-phase02-routed-model",
        turn_id="turn-phase02-routed-model",
        topic_id="topic-phase02-routed-model",
        request={
            "question": intent.original_user_text,
            "stop_after_phase": "phase02",
        },
        context_manifest={"manifest_id": "manifest-phase02-routed-model"},
        turn_intent="new_topic",
        topic_relation="new_topic",
        llm_calls=tuple(planned["llm_calls"]),
    )

    assert finalized["status"] == "planned"
    assert store.runs[intent.run_attempt_id]["status"] == "planned"


def test_invalid_planner_output_fails_without_persisted_fallback_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    intent = _intent_revision(registry)
    ledger = _decision_ledger(intent)
    context = _authority_context(registry)
    store = _Phase02AuthorityStore(ledger)
    invalid_planner = _PlannerLLM(
        {
            "issue_tree": [],
            "auxiliary_axes": [],
            "hypotheses": [],
            "priority_proposals": [],
            "assumption_proposals": [],
        }
    )
    monkeypatch.setattr(
        langgraph_workflow,
        "resolve_latest_authority_context",
        lambda **_: context,
    )

    with pytest.raises(
        langgraph_workflow.WorkflowFailure,
        match="planner_proposal_issue_tree_invalid",
    ):
        langgraph_workflow._compile_authoritative_plan(
            _phase02_compile_state(
                intent=intent,
                ledger=ledger,
                registry=registry,
                store=store,
                llm_client=invalid_planner,
            )
        )

    assert store.save_calls == 0
    assert store.plan_revision is None
    assert store.planner_proposal is None


@pytest.mark.parametrize("goal_id", _registry().analysis_goal_ids)
def test_every_launch_goal_compiles_to_one_executable_plan(
    goal_id: str,
) -> None:
    registry = _registry()
    base = _intent_revision(registry)
    goal = registry.analysis_goal_obligation(goal_id)
    time_spec, comparison_spec, expected_execution_mode = _goal_temporal_fixture(
        goal_id
    )
    intent = IntentRevision.create(
        run_attempt_id=f"run-phase02-goal-{goal_id}",
        original_user_text=f"验证 {goal['business_name']} 的完整分析计划。",
        goal_bindings=({"goal_id": goal_id, "role": "primary"},),
        target_metric_refs=base.target_metric_refs,
        scope=base.scope,
        time_spec=time_spec,
        comparison_spec=comparison_spec,
        direction_premise="unknown",
        requested_analysis_axes=(),
        desired_decisions=(),
        ambiguity_slots=(),
        source_spans=(),
        schema_version=base.schema_version,
        prompt_version=base.prompt_version,
        model_version=base.model_version,
        known_goal_ids=set(registry.analysis_goal_ids),
        known_metric_ids=set(registry.metric_ids),
        known_analysis_axis_ids=set(registry.analysis_axis_ids),
        known_scope_types=set(registry.public_scope_types),
    )
    ledger = DecisionLedger()
    coverage = tuple(
        {
            "dataset_id": dataset_id,
            "availability": (
                "claim_ready"
                if dataset_id == "paid_order_success"
                else "missing_contract"
            ),
            "release_ref": (
                "release:paid_order_success:v1"
                if dataset_id == "paid_order_success"
                else None
            ),
            "snapshot_refs": (
                ("snapshot:paid_order_success:v1",)
                if dataset_id == "paid_order_success"
                else ()
            ),
            "limitation_ref": (
                None
                if dataset_id == "paid_order_success"
                else f"limitation:missing-contract:{dataset_id}"
            ),
        }
        for dataset_id in registry.dataset_ids
    )
    context = AuthorityContext.create(
        run_attempt_id=intent.run_attempt_id,
        actual_as_of="2026-07-18T00:00:00Z",
        release_refs=("release:paid_order_success:v1",),
        snapshot_refs=("snapshot:paid_order_success:v1",),
        dataset_coverage=coverage,
        contract_versions={
            "runtime_bindings": registry.contract_version,
            "runtime_bindings_digest": registry.source_payload_digest,
        },
    )
    proposal = PlannerProposal.create(
        run_attempt_id=intent.run_attempt_id,
        intent_revision_id=intent.intent_revision_id,
        decision_refs=(),
        authority_context_ref=context.authority_context_ref,
        issue_tree=(
            {
                "issue_id": "launch-goal-root",
                "parent_issue_id": None,
                "question": "这类业务问题需要验证哪些结果与边界？",
                "target_claim_kind": next(
                    claim_kind
                    for claim_types in goal["outcome_claim_types"].values()
                    for claim_kind in claim_types
                ),
            },
        ),
        auxiliary_axes=(),
        hypotheses=(),
        priority_proposals=(),
        assumption_proposals=(),
        raw_provider_response_ref="restricted-provider-response:test",
        schema_version="planner-proposal.v1",
        prompt_version="phase02-goal-coverage-test",
        model_version="typed-test-model",
    )

    compiled = AuthoritativePlanCompiler(runtime_registry=registry).compile(
        intent_revision=intent,
        decision_ledger=ledger,
        authority_context=context,
        planner_proposal=proposal,
    )

    assert compiled.plan_revision.executable is True
    assert compiled.plan_revision.claim_obligations
    assert compiled.plan_revision.analysis_axes
    assert compiled.plan_revision.capability_tasks
    assert (
        temporal_execution_mode(compiled.plan_revision.temporal_authority)
        == expected_execution_mode
    )
    assert compiled.plan_revision.temporal_authority == (
        resolved_test_temporal_authority(
            time_spec=time_spec,
            comparison_spec=comparison_spec,
            decision_ledger=ledger,
            require_physical_baseline=(comparison_spec["kind"] != "none"),
        )
    )
    assert compiled.plan_revision.planner_proposal_ref == (proposal.planner_proposal_id)
    assert compiled.plan_revision.proposal_admission_ref == (
        compiled.proposal_admission.proposal_admission_id
    )
    if goal_id == "business_object_impact_review":
        impact_obligation = next(
            obligation
            for obligation in compiled.plan_revision.claim_obligations
            if obligation.claim_kind == "business_object_candidate_impact"
        )
        assert canonical_value(
            impact_obligation.success_policy["composite_support_policy"]
        ) == canonical_value(
            registry.claim_composite_support_policy("business_object_candidate_impact")
        )
        event_tasks = {
            task.capability_id: task
            for task in compiled.plan_revision.capability_tasks
            if task.capability_id in {"event_evidence", "event_window_compare"}
        }
        assert set(event_tasks) == {"event_evidence", "event_window_compare"}
        assert event_tasks["event_window_compare"].dependency_task_ids == (
            event_tasks["event_evidence"].task_id,
        )
        assert event_tasks["event_evidence"].execution_rank < (
            event_tasks["event_window_compare"].execution_rank
        )


def test_agent_core_rejects_unpersisted_plan_transition() -> None:
    registry = _registry()
    intent = _intent_revision(registry)
    ledger = _decision_ledger(intent)
    context = _authority_context(registry)
    decision_refs = tuple(record.decision_id for record in ledger.active_records())
    provider_output = _planner_provider_output(intent, context, decision_refs)
    store = _Phase02AuthorityStore(ledger)
    state = _phase02_compile_state(
        intent=intent,
        ledger=ledger,
        registry=registry,
        store=store,
        llm_client=_PlannerLLM(provider_output),
    )
    with patch.object(
        langgraph_workflow,
        "resolve_latest_authority_context",
        return_value=context,
    ):
        planned = langgraph_workflow._compile_authoritative_plan(state)
    store.transition = None

    with pytest.raises(
        EvidenceIntegrityError,
        match="single_authority_plan_persistence_mismatch",
    ):
        _finalize_authoritative_plan(
            store=store,
            plan_result=planned["plan_result"],
            run_id=intent.run_attempt_id,
            thread_id="thread-phase02",
            turn_id="turn-phase02",
            topic_id="topic-phase02",
            request={
                "question": intent.original_user_text,
                "stop_after_phase": "phase02",
            },
            context_manifest={"manifest_id": "manifest-phase02"},
            turn_intent="new_topic",
            topic_relation="new_topic",
            llm_calls=tuple(planned["llm_calls"]),
        )

    assert intent.run_attempt_id not in store.runs


def test_agent_core_rejects_plan_from_wrong_decision_transition_head() -> None:
    registry = _registry()
    intent = _intent_revision(registry)
    ledger = _decision_ledger(intent)
    context = _authority_context(registry)
    decision_refs = tuple(record.decision_id for record in ledger.active_records())
    store = _Phase02AuthorityStore(ledger)
    state = _phase02_compile_state(
        intent=intent,
        ledger=ledger,
        registry=registry,
        store=store,
        llm_client=_PlannerLLM(
            _planner_provider_output(intent, context, decision_refs)
        ),
    )
    with patch.object(
        langgraph_workflow,
        "resolve_latest_authority_context",
        return_value=context,
    ):
        planned = langgraph_workflow._compile_authoritative_plan(state)

    with pytest.raises(
        EvidenceIntegrityError,
        match="single_authority_plan_transition_parent_mismatch",
    ):
        _finalize_authoritative_plan(
            store=store,
            plan_result=planned["plan_result"],
            run_id=intent.run_attempt_id,
            thread_id="thread-phase02",
            turn_id="turn-phase02",
            topic_id="topic-phase02",
            request={
                "question": intent.original_user_text,
                "stop_after_phase": "phase02",
            },
            context_manifest={"manifest_id": "manifest-phase02"},
            turn_intent="new_topic",
            topic_relation="new_topic",
            llm_calls=tuple(planned["llm_calls"]),
            expected_parent_transition_id="transition-decision-head",
        )

    assert intent.run_attempt_id not in store.runs
