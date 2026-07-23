from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import json

import pytest

from bi_agent.runtime.authoritative_execution_result import (
    AuthoritativeExecutionResult,
)
from bi_agent.runtime.capability_authority import (
    CapabilityAdapterOutput,
    CapabilityAttempt,
    CapabilityEvidence,
    CapabilityFailure,
    CapabilityOutcome,
    EvidenceLedgerEntry,
    ExecutionSnapshot,
    ExplorationStopRecord,
    FailureRecord,
)
from bi_agent.runtime.capability_scheduler import (
    capability_execution_transition_payloads,
)
from bi_agent.runtime.claim_coverage import (
    PLAN_EXPANSION_PROVIDER_TASK,
    AdmissibleAxisRoute,
    ClaimCoverageCheckpoint,
    ClaimCoverageContractError,
    ClaimCoverageEvaluation,
    PlanExpansionDecision,
    PlanPatch,
    claim_coverage_transition_payloads,
    evaluate_claim_coverage,
)
from bi_agent.runtime.contracts import load_contract
from bi_agent.runtime.evidence_authority import canonical_digest
from bi_agent.runtime.plan_authority import (
    AnalysisAxis,
    AuthorityContext,
    ClaimObligation,
    EvidenceRequirement,
    PlanRevision,
)
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)
from bi_agent.runtime.single_authority import DurableTransition
from tests.support.temporal_authority import resolved_test_temporal_authority


def _registry(*, auxiliary_budget_limit: int | None = None) -> RuntimeContractRegistry:
    if auxiliary_budget_limit is None:
        return RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    payload = load_contract(CANONICAL_RUNTIME_BINDINGS_PATH)
    payload["exploration_budget_policy"]["auxiliary_budget_limit"] = (
        auxiliary_budget_limit
    )
    return RuntimeContractRegistry(payload)


def _authority_context() -> AuthorityContext:
    registry = _registry()
    return AuthorityContext.create(
        run_attempt_id="run-claim-coverage",
        actual_as_of="2026-07-18T08:00:00Z",
        release_refs=("release:paid-order-success",),
        snapshot_refs=("snapshot:paid-order-success",),
        dataset_coverage=(
            {
                "dataset_id": "paid_order_success",
                "availability": "claim_ready",
                "release_ref": "release:paid-order-success",
                "snapshot_refs": ("snapshot:paid-order-success",),
                "limitation_ref": None,
            },
            {
                "dataset_id": "payment_final_outcome",
                "availability": "unavailable",
                "release_ref": None,
                "snapshot_refs": (),
                "limitation_ref": "limitation:payment-attempt",
            },
        ),
        contract_versions={
            "runtime_bindings": registry.contract_version,
            "runtime_bindings_digest": registry.source_payload_digest,
        },
    )


def _plan(
    *,
    claim_kind: str = "comparative_change",
    evidence_kinds: str | tuple[str, ...] = "observed",
    minimum_claim_strength: str = "directional",
    axis_id: str = "change_validation",
    capability_id: str = "compare_periods",
    runtime_registry: RuntimeContractRegistry | None = None,
) -> PlanRevision:
    registry = runtime_registry or _registry()
    authority_context = _authority_context()
    obligation = ClaimObligation.create(
        claim_kind=claim_kind,
        role="user_required",
        subject={
            "target_metric_ref": "paid_amount",
            "scope": {"type": "full_sample"},
            "outcome_refs": ("direction_and_magnitude",),
            "goal_refs": ("explain_change",),
        },
        evidence_requirement=EvidenceRequirement.create(
            operator="any_of",
            evidence_kinds=(
                (evidence_kinds,) if isinstance(evidence_kinds, str) else evidence_kinds
            ),
        ),
        success_policy={
            "policy": "verified_or_explicit_boundary",
            "minimum_claim_strength": minimum_claim_strength,
            "outcome_refs": ("direction_and_magnitude",),
        },
    )
    axis_contract = registry.analysis_axis(axis_id)
    axis = AnalysisAxis.create(
        axis_id=axis_id,
        role="required",
        axis_kind=axis_contract["axis_kind"],
        target_metric_refs=("paid_amount",),
        metric_refs=axis_contract["metric_refs"],
        dimension_refs=axis_contract["dimension_refs"],
        context_source_refs=axis_contract["context_source_refs"],
        capability_refs=(capability_id,),
        reconciliation_group=axis_contract["reconciliation_group"],
        selection_policy=axis_contract["selection_policy"],
        source_refs=axis_contract["source_refs"],
        goal_refs=("explain_change",),
        supports_obligation_ids=(obligation.obligation_id,),
    )
    temporal_authority = resolved_test_temporal_authority(
        time_spec={"kind": "date", "target": "2026-06-19"},
        comparison_spec={
            "kind": "fixed_window",
            "baseline_class": "prior_period",
            "baseline_start": "2026-06-18",
            "baseline_end": "2026-06-18",
            "aggregation": "sum_of_complete_days",
        },
        require_physical_baseline=True,
    )
    return PlanRevision.create(
        run_attempt_id="run-claim-coverage",
        supersedes_plan_revision_id=None,
        intent_revision_id="intent-claim-coverage",
        decision_refs=("decision:baseline",),
        authority_context_ref=authority_context.authority_context_ref,
        planner_proposal_ref="planner-proposal:claim-coverage",
        proposal_admission_ref="proposal-admission:claim-coverage",
        temporal_authority=temporal_authority,
        resolved_window_refs=temporal_authority.resolved_window_refs,
        context_window_specs=(),
        claim_obligations=(obligation,),
        analysis_axes=(axis,),
        capability_task_specs=(
            {
                "task_key": f"{axis_id}:{capability_id}",
                "capability_id": capability_id,
                "normalized_input_refs": (
                    authority_context.authority_context_ref,
                    axis.analysis_axis_ref,
                    *temporal_authority.resolved_window_refs,
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
                    "input_states": (),
                },
            },
        ),
        assumption_refs=(),
        budget_policy_ref=(registry.exploration_budget_policy.budget_policy_ref),
        contract_versions={"runtime": "claim-coverage-test.v1"},
    )


def _execution(
    *,
    succeeded: bool,
    evidence_kind: str = "observed",
    maximum_claim_strength: str = "directional",
    claim_kind: str = "comparative_change",
    evidence_kinds: str | tuple[str, ...] = "observed",
    minimum_claim_strength: str = "directional",
    axis_id: str = "change_validation",
    capability_id: str = "compare_periods",
    limitation_refs: tuple[str, ...] = (),
    hard_budget_limit: int | None = None,
    runtime_registry: RuntimeContractRegistry | None = None,
) -> AuthoritativeExecutionResult:
    registry = runtime_registry or _registry()
    plan = _plan(
        claim_kind=claim_kind,
        evidence_kinds=evidence_kinds,
        minimum_claim_strength=minimum_claim_strength,
        axis_id=axis_id,
        capability_id=capability_id,
        runtime_registry=registry,
    )
    task = plan.capability_tasks[0]
    attempt = CapabilityAttempt.create(plan, task)
    if succeeded:
        evidence = CapabilityEvidence.create(
            evidence_ref="evidence:claim-coverage",
            binding_record_ref="binding:claim-coverage",
            execution_state="available",
            evidence_kind=evidence_kind,
            data_contract_state="complete",
            supported_claim_kinds=(claim_kind,),
            evidence_strength="high",
            maximum_claim_strength=maximum_claim_strength,
            observation_facts=({"name": "paid_amount_delta", "value": 12},),
            scope="full_sample",
            window_refs=plan.resolved_window_refs,
            dimension_path=(),
            limitation_refs=limitation_refs,
            result_refs=("result:claim-coverage",),
            completeness_report_refs=("completeness:claim-coverage",),
            hierarchy_qualified=False,
        )
        output = CapabilityAdapterOutput.create(
            status="succeeded",
            output_payload={"result_ref": "result:claim-coverage"},
            evidence=(evidence,),
            affected_obligation_ids=task.supports_obligation_ids,
            limitation_refs=limitation_refs,
            retryability="never",
        )
        outcome = CapabilityOutcome.create(
            attempt,
            task,
            output,
            failure_ref=None,
            budget_units=1,
        )
        bundle = (
            attempt,
            outcome,
            (EvidenceLedgerEntry.create(plan, task, outcome, evidence),),
            (),
        )
    else:
        failure = CapabilityFailure.create(
            layer="capability",
            kind="comparison_unavailable",
            scope="task",
            affected_refs=(task.task_id, *task.supports_obligation_ids),
            integrity_level="task",
            retryability="never",
            user_actionable=False,
            business_boundary="comparison_evidence_unavailable",
            technical_detail_ref="technical-detail:claim-coverage",
        )
        output = CapabilityAdapterOutput.create(
            status="unavailable",
            output_payload={"status": "unavailable"},
            evidence=(),
            affected_obligation_ids=task.supports_obligation_ids,
            limitation_refs=("limitation:comparison-unavailable",),
            retryability="never",
            failure=failure,
        )
        failure_record = FailureRecord.create(attempt, failure)
        outcome = CapabilityOutcome.create(
            attempt,
            task,
            output,
            failure_ref=failure_record.failure_ref,
            budget_units=1,
        )
        bundle = (attempt, outcome, (), (failure_record,))
    stop = ExplorationStopRecord.create(
        plan,
        (outcome,),
        reason="plan_exhausted",
        hard_budget_limit=(
            hard_budget_limit
            if hard_budget_limit is not None
            else registry.exploration_budget_policy.effective_hard_budget_limit(plan)
        ),
    )
    snapshot = ExecutionSnapshot.create(
        plan,
        stop,
        (outcome,),
        bundle[2],
        bundle[3],
    )
    input_payload, output_payload = capability_execution_transition_payloads(
        plan,
        snapshot,
        stop,
    )
    transition = DurableTransition.create(
        node_name="execute_capability_dag",
        parent_transition_id="transition:claim-coverage-plan",
        run_attempt_id=plan.run_attempt_id,
        intent_revision_id=plan.intent_revision_id,
        decision_ledger_position=1,
        input_digest=canonical_digest(input_payload),
        output_digest=canonical_digest(output_payload),
        execution_attempt=1,
        provider_ref="waje-capability-runtime",
        model_ref="deterministic-capability-dag.v1",
        status="succeeded",
        acceptance_state="accepted",
        next_transition="phase03_evidence_bound",
        started_at="2026-07-18T08:00:00+00:00",
        finished_at="2026-07-18T08:00:01+00:00",
    )
    return AuthoritativeExecutionResult.from_records(
        plan_revision=plan,
        execution_snapshot=snapshot,
        exploration_stop_record=stop,
        capability_outcome_bundles=(bundle,),
        durable_transition=transition,
    )


def test_weak_evidence_stays_pending_and_keeps_expansion_routes() -> None:
    authority_context = _authority_context()
    plan = _plan()
    execution = _execution(
        succeeded=True,
        maximum_claim_strength="descriptive",
    )

    evaluation = evaluate_claim_coverage(
        authority_context=authority_context,
        plan_revision=plan,
        execution_result=execution,
        route_catalog=_registry(),
    )

    coverage = evaluation.obligation_coverages[0]
    assert coverage.status == "evidence_present"
    assert coverage.success_policy == plan.claim_obligations[0].success_policy
    assert coverage.required_claim_strength == "directional"
    assert len(coverage.evidence_assessments) == 1
    assessment = coverage.evidence_assessments[0]
    assert assessment.maximum_claim_strength == "descriptive"
    assert assessment.publication_ceiling == {
        "claim_class": "observed_fact",
        "strength": "descriptive",
    }
    assert assessment.limitation_refs == ()
    assert evaluation.unresolved_obligation_ids == (
        plan.claim_obligations[0].obligation_id,
    )
    assert "time_context" in {route.axis_id for route in evaluation.admissible_routes}
    assert evaluation.exploration_stop_ref == (
        execution.exploration_stop_record.stop_ref
    )
    assert evaluation.exploration_stop_policy == (
        execution.exploration_stop_record.policy_decision
    )


def test_explicit_boundary_closes_only_with_typed_ceiling_and_limitation() -> None:
    execution = _execution(
        succeeded=True,
        evidence_kind="boundary",
        maximum_claim_strength="trust_boundary",
        claim_kind="contract_coverage_and_trust_boundary",
        evidence_kinds="boundary",
        minimum_claim_strength="trust_boundary",
        axis_id="data_quality",
        capability_id="data_quality_check",
        limitation_refs=("limitation:payment-attempt-contract",),
    )

    evaluation = evaluate_claim_coverage(
        authority_context=_authority_context(),
        plan_revision=execution.plan_revision,
        execution_result=execution,
        route_catalog=_registry(),
    )

    coverage = evaluation.obligation_coverages[0]
    assert coverage.status == "explicit_boundary"
    assert coverage.evidence_assessments[0].publication_ceiling == {
        "claim_class": "boundary",
        "strength": "trust_boundary",
    }
    assert coverage.evidence_assessments[0].limitation_refs == (
        "limitation:payment-attempt-contract",
    )
    assert evaluation.unresolved_obligation_ids == ()
    assert evaluation.admissible_routes == ()
    assert PlanExpansionDecision.deterministic_seal(evaluation).decision == "seal"


def _provider_audit(
    output: dict,
    *,
    raw_output: dict | None = None,
) -> dict:
    return {
        "task": PLAN_EXPANSION_PROVIDER_TASK,
        "provider": "openai",
        "model": "gpt-5.4",
        "prompt_version": "claim-coverage-expansion.v1",
        "raw_response_content": json.dumps(raw_output or output),
        "structured_output": output,
    }


def _coverage_transition(
    *,
    execution: AuthoritativeExecutionResult,
    evaluation: ClaimCoverageEvaluation,
    decision: PlanExpansionDecision,
    plan_patch: PlanPatch | None,
) -> DurableTransition:
    input_payload, output_payload = claim_coverage_transition_payloads(
        evaluation=evaluation,
        decision=decision,
        plan_patch=plan_patch,
    )
    return DurableTransition.create(
        node_name="evaluate_claim_coverage",
        parent_transition_id=execution.transition_id,
        run_attempt_id=execution.run_attempt_id,
        intent_revision_id=execution.intent_revision_id,
        decision_ledger_position=(
            execution.durable_transition.decision_ledger_position
        ),
        input_digest=canonical_digest(input_payload),
        output_digest=canonical_digest(output_payload),
        execution_attempt=1,
        provider_ref=(
            decision.provider_ref
            if decision.decision_authority == "provider"
            else "local_deterministic"
        ),
        model_ref=(
            decision.model_ref
            if decision.decision_authority == "provider"
            else "claim-coverage-contract.v1"
        ),
        status="succeeded",
        acceptance_state="accepted",
        next_transition=(
            "compile_plan_patch"
            if decision.decision == "patch"
            else "seal_authority_bundle"
        ),
        started_at="2026-07-18T08:00:02+00:00",
        finished_at="2026-07-18T08:00:03+00:00",
    )


def test_unresolved_coverage_exposes_only_unscheduled_contract_routes() -> None:
    execution = _execution(succeeded=False)
    evaluation = evaluate_claim_coverage(
        authority_context=_authority_context(),
        plan_revision=execution.plan_revision,
        execution_result=execution,
        route_catalog=_registry(),
    )

    obligation = execution.plan_revision.claim_obligations[0]
    assert evaluation.unresolved_obligation_ids == (obligation.obligation_id,)
    assert evaluation.scheduled_axis_ids == ("change_validation",)
    routes = {item.axis_id: item for item in evaluation.admissible_routes}
    assert set(routes) == {"payment_outcome_health", "time_context"}
    payment_route = routes["payment_outcome_health"]
    assert payment_route.business_name == "支付最终状态与成功率"
    assert payment_route.incremental_capability_ids == ("payment_outcome_compare",)
    time_route = routes["time_context"]
    assert time_route.business_name == "时间背景"
    assert time_route.semantics
    assert time_route.selection_policy == "periodic_context_when_available"
    assert time_route.maximum_claim_strength_by_obligation == {
        obligation.obligation_id: "directional"
    }
    assert time_route.expected_value_projection == {
        "actionability": "decision_supporting",
        "expected_information_gain": "obligation_closing",
        "materiality": "user_required",
        "statistical_risk": "contract_bounded",
    }
    assert time_route.estimated_budget_units == 4
    assert time_route.estimated_auxiliary_budget_units == 2
    assert time_route.remaining_auxiliary_budget_units is None
    assert set(time_route.incremental_capability_ids) == {
        "metric_timeseries",
        "rolling_window_compare",
        "compare_period_phases",
        "weekday_calendar_compare",
    }
    assert set(time_route.protected_incremental_capability_ids) == {
        "metric_timeseries",
        "rolling_window_compare",
    }
    assert set(time_route.auxiliary_incremental_capability_ids) == {
        "compare_period_phases",
        "weekday_calendar_compare",
    }
    assert {item.capability_id for item in time_route.evidence_routes} == {
        "metric_timeseries"
    }
    assert all(
        route.supported_obligation_ids == (obligation.obligation_id,)
        and route.supported_claim_kinds == ("comparative_change",)
        and "observed" in route.evidence_kinds
        for route in routes.values()
    )
    assert (
        ClaimCoverageEvaluation.from_dict(
            evaluation.to_dict(),
            authority_context=_authority_context(),
            plan_revision=execution.plan_revision,
            execution_result=execution,
            route_catalog=_registry(),
        )
        == evaluation
    )
    with pytest.raises(FrozenInstanceError):
        evaluation.status = "changed"


def test_route_excludes_capabilities_whose_claim_class_cannot_reach_policy() -> None:
    execution = _execution(
        succeeded=False,
        claim_kind="segment_contribution_or_mix_shift",
        evidence_kinds=("derived", "statistical_association"),
        minimum_claim_strength="candidate_driver",
    )
    evaluation = evaluate_claim_coverage(
        authority_context=_authority_context(),
        plan_revision=execution.plan_revision,
        execution_result=execution,
        route_catalog=_registry(),
    )

    route = next(
        item
        for item in evaluation.admissible_routes
        if item.axis_id == "dimension_localization"
    )
    capability_ids = {item.capability_id for item in route.evidence_routes}
    assert "candidate_dimension_screen" in capability_ids
    assert "joint_attribution" in capability_ids
    assert "high_value_user_contribution" in capability_ids
    assert capability_ids.isdisjoint(
        {
            "segment_contribution",
            "segment_breakdown",
            "segment_shift_compare",
        }
    )
    obligation_id = execution.plan_revision.claim_obligations[0].obligation_id
    assert route.maximum_claim_strength_by_obligation == {
        obligation_id: "candidate_driver"
    }
    assert all(
        item.required_claim_strength == "candidate_driver"
        and item.publication_ceiling["strength"] == "candidate_driver"
        for item in route.evidence_routes
    )


@pytest.mark.parametrize("auxiliary_budget_limit", [0, 1])
def test_auxiliary_route_is_removed_while_required_route_survives_budget(
    auxiliary_budget_limit: int,
) -> None:
    registry = _registry(auxiliary_budget_limit=auxiliary_budget_limit)
    execution = _execution(
        succeeded=False,
        runtime_registry=registry,
    )
    evaluation = evaluate_claim_coverage(
        authority_context=_authority_context(),
        plan_revision=execution.plan_revision,
        execution_result=execution,
        route_catalog=registry,
    )

    assert execution.exploration_stop_record.used_budget_units == 1
    assert execution.exploration_stop_record.hard_budget_limit == (
        1 + auxiliary_budget_limit
    )
    assert tuple(item.axis_id for item in evaluation.admissible_routes) == (
        "payment_outcome_health",
    )


def test_required_closing_tasks_do_not_consume_auxiliary_route_budget() -> None:
    registry = _registry(auxiliary_budget_limit=2)
    execution = _execution(
        succeeded=False,
        runtime_registry=registry,
    )
    evaluation = evaluate_claim_coverage(
        authority_context=_authority_context(),
        plan_revision=execution.plan_revision,
        execution_result=execution,
        route_catalog=registry,
    )

    route = next(
        item for item in evaluation.admissible_routes if item.axis_id == "time_context"
    )
    assert route.estimated_budget_units == 4
    assert route.estimated_auxiliary_budget_units == 2
    assert route.remaining_auxiliary_budget_units == 2
    assert route.protected_incremental_capability_ids == (
        "metric_timeseries",
        "rolling_window_compare",
    )
    assert route.auxiliary_incremental_capability_ids == (
        "compare_period_phases",
        "weekday_calendar_compare",
    )


def test_selected_route_union_cannot_exceed_remaining_auxiliary_budget() -> None:
    registry = _registry(auxiliary_budget_limit=3)
    execution = _execution(
        succeeded=False,
        runtime_registry=registry,
    )
    source_evaluation = evaluate_claim_coverage(
        authority_context=_authority_context(),
        plan_revision=execution.plan_revision,
        execution_result=execution,
        route_catalog=registry,
    )
    source_route = next(
        item
        for item in source_evaluation.admissible_routes
        if item.axis_id == "time_context"
    )
    second_route = AdmissibleAxisRoute.create(
        axis_id="second_time_context",
        business_name=source_route.business_name,
        semantics=source_route.semantics,
        selection_policy=source_route.selection_policy,
        supported_obligation_ids=source_route.supported_obligation_ids,
        supported_claim_kinds=source_route.supported_claim_kinds,
        capability_ids=source_route.capability_ids,
        incremental_capability_ids=(source_route.incremental_capability_ids),
        protected_incremental_capability_ids=(
            source_route.protected_incremental_capability_ids
        ),
        auxiliary_incremental_capability_ids=(
            source_route.auxiliary_incremental_capability_ids
        ),
        evidence_kinds=source_route.evidence_kinds,
        evidence_routes=source_route.evidence_routes,
        maximum_claim_strength_by_obligation=(
            source_route.maximum_claim_strength_by_obligation
        ),
        expected_value_projection=source_route.expected_value_projection,
        estimated_budget_units=source_route.estimated_budget_units,
        estimated_auxiliary_budget_units=(
            source_route.estimated_auxiliary_budget_units
        ),
        remaining_auxiliary_budget_units=(
            source_route.remaining_auxiliary_budget_units
        ),
        contract_refs=source_route.contract_refs,
    )
    evaluation = ClaimCoverageEvaluation.create(
        plan_revision=execution.plan_revision,
        execution_result=execution,
        obligation_coverages=source_evaluation.obligation_coverages,
        admissible_routes=(source_route, second_route),
    )

    single_route_decision = PlanExpansionDecision.from_provider_audit(
        evaluation=evaluation,
        provider_audit=_provider_audit(
            {"decision": "patch", "selected_axis_ids": ["time_context"]}
        ),
    )
    assert single_route_decision.selected_auxiliary_budget_units == 2

    with pytest.raises(
        ClaimCoverageContractError,
        match="plan_expansion_patch_budget_exceeded",
    ):
        PlanExpansionDecision.from_provider_audit(
            evaluation=evaluation,
            provider_audit=_provider_audit(
                {
                    "decision": "patch",
                    "selected_axis_ids": [
                        "time_context",
                        "second_time_context",
                    ],
                }
            ),
        )


def test_verifier_ready_evidence_can_be_sealed_only_by_provider_judgment() -> None:
    execution = _execution(succeeded=True)
    evaluation = evaluate_claim_coverage(
        authority_context=_authority_context(),
        plan_revision=execution.plan_revision,
        execution_result=execution,
        route_catalog=_registry(),
    )

    assert evaluation.unresolved_obligation_ids == (
        execution.plan_revision.claim_obligations[0].obligation_id,
    )
    assert {route.axis_id for route in evaluation.admissible_routes} == {
        "payment_outcome_health",
        "time_context",
    }
    assert evaluation.obligation_coverages[0].status == "evidence_present"
    assert evaluation.obligation_coverages[0].evidence_entry_refs

    decision = PlanExpansionDecision.from_provider_audit(
        evaluation=evaluation,
        provider_audit=_provider_audit({"decision": "seal", "selected_axis_ids": []}),
    )
    assert decision.decision == "seal"
    assert decision.decision_authority == "provider"
    assert decision.provider_audit_ref is not None
    assert (
        PlanExpansionDecision.from_dict(decision.to_dict(), evaluation=evaluation)
        == decision
    )


def test_provider_patch_and_plan_patch_close_all_source_authority() -> None:
    execution = _execution(succeeded=False)
    evaluation = evaluate_claim_coverage(
        authority_context=_authority_context(),
        plan_revision=execution.plan_revision,
        execution_result=execution,
        route_catalog=_registry(),
    )
    output = {
        "decision": "patch",
        "selected_axis_ids": ["time_context"],
    }
    decision = PlanExpansionDecision.from_provider_audit(
        evaluation=evaluation,
        provider_audit=_provider_audit(output),
    )
    obligation_id = execution.plan_revision.claim_obligations[0].obligation_id

    assert decision.decision == "patch"
    assert decision.selected_axis_ids == ("time_context",)
    assert decision.selected_obligation_ids == (obligation_id,)
    assert decision.selected_auxiliary_budget_units == 2
    assert decision.provider_audit_ref.startswith(
        "plan-expansion-provider-audit:sha256:"
    )
    assert decision.raw_response_ref.startswith("restricted-provider-response:sha256:")
    assert (
        PlanExpansionDecision.from_dict(decision.to_dict(), evaluation=evaluation)
        == decision
    )

    patch = PlanPatch.create(
        plan_revision=execution.plan_revision,
        execution_result=execution,
        evaluation=evaluation,
        decision=decision,
    )
    assert patch.run_attempt_id == execution.run_attempt_id
    assert patch.intent_revision_id == execution.intent_revision_id
    assert patch.authority_context_ref == execution.authority_context_ref
    assert patch.source_plan_revision_id == execution.plan_revision_id
    assert (
        patch.source_execution_result_ref
        == execution.authoritative_execution_result_ref
    )
    assert patch.source_unresolved_obligation_ids == (obligation_id,)
    assert patch.selected_axis_ids == ("time_context",)
    assert patch.selected_obligation_ids == (obligation_id,)
    assert patch.selected_auxiliary_budget_units == 2
    assert patch.provider_audit_ref == decision.provider_audit_ref
    assert (
        PlanPatch.from_dict(
            patch.to_dict(),
            plan_revision=execution.plan_revision,
            execution_result=execution,
            evaluation=evaluation,
            decision=decision,
        )
        == patch
    )


def test_patch_selection_is_strict_and_deterministic_seal_cannot_skip_routes() -> None:
    execution = _execution(succeeded=False)
    evaluation = evaluate_claim_coverage(
        authority_context=_authority_context(),
        plan_revision=execution.plan_revision,
        execution_result=execution,
        route_catalog=_registry(),
    )

    with pytest.raises(
        ClaimCoverageContractError,
        match="plan_expansion_deterministic_seal_forbidden",
    ):
        PlanExpansionDecision.deterministic_seal(evaluation)

    with pytest.raises(
        ClaimCoverageContractError,
        match="plan_expansion_patch_selection_empty",
    ):
        PlanExpansionDecision.from_provider_audit(
            evaluation=evaluation,
            provider_audit=_provider_audit(
                {"decision": "patch", "selected_axis_ids": []}
            ),
        )

    with pytest.raises(
        ClaimCoverageContractError,
        match="plan_expansion_patch_route_not_admissible",
    ):
        PlanExpansionDecision.from_provider_audit(
            evaluation=evaluation,
            provider_audit=_provider_audit(
                {
                    "decision": "patch",
                    "selected_axis_ids": ["change_validation"],
                }
            ),
        )


def test_provider_seal_preserves_provider_judgment_but_cannot_create_patch() -> None:
    execution = _execution(succeeded=False)
    evaluation = evaluate_claim_coverage(
        authority_context=_authority_context(),
        plan_revision=execution.plan_revision,
        execution_result=execution,
        route_catalog=_registry(),
    )
    decision = PlanExpansionDecision.from_provider_audit(
        evaluation=evaluation,
        provider_audit=_provider_audit({"decision": "seal", "selected_axis_ids": []}),
    )

    assert decision.decision == "seal"
    assert decision.decision_authority == "provider"
    with pytest.raises(
        ClaimCoverageContractError,
        match="plan_patch_decision_invalid",
    ):
        PlanPatch.create(
            plan_revision=execution.plan_revision,
            execution_result=execution,
            evaluation=evaluation,
            decision=decision,
        )


def test_provider_audit_and_evidence_contract_mismatches_fail_closed() -> None:
    execution = _execution(succeeded=False)
    evaluation = evaluate_claim_coverage(
        authority_context=_authority_context(),
        plan_revision=execution.plan_revision,
        execution_result=execution,
        route_catalog=_registry(),
    )
    output = {
        "decision": "patch",
        "selected_axis_ids": ["time_context"],
    }
    with pytest.raises(
        ClaimCoverageContractError,
        match="plan_expansion_provider_audit_invalid",
    ):
        PlanExpansionDecision.from_provider_audit(
            evaluation=evaluation,
            provider_audit=_provider_audit(
                output,
                raw_output={"decision": "seal", "selected_axis_ids": []},
            ),
        )

    incompatible_execution = _execution(
        succeeded=True,
        evidence_kind="derived",
    )
    with pytest.raises(
        ClaimCoverageContractError,
        match="claim_coverage_evidence_contract_invalid",
    ):
        evaluate_claim_coverage(
            authority_context=_authority_context(),
            plan_revision=incompatible_execution.plan_revision,
            execution_result=incompatible_execution,
            route_catalog=_registry(),
        )


def test_roundtrip_rejects_tampered_patch_authority_ref() -> None:
    execution = _execution(succeeded=False)
    evaluation = evaluate_claim_coverage(
        authority_context=_authority_context(),
        plan_revision=execution.plan_revision,
        execution_result=execution,
        route_catalog=_registry(),
    )
    decision = PlanExpansionDecision.from_provider_audit(
        evaluation=evaluation,
        provider_audit=_provider_audit(
            {
                "decision": "patch",
                "selected_axis_ids": ["time_context"],
            }
        ),
    )
    patch = PlanPatch.create(
        plan_revision=execution.plan_revision,
        execution_result=execution,
        evaluation=evaluation,
        decision=decision,
    )
    tampered = patch.to_dict()
    tampered["authority_context_ref"] = "authority-context:tampered"

    with pytest.raises(
        ClaimCoverageContractError,
        match="plan_patch_integrity_invalid",
    ):
        PlanPatch.from_dict(
            tampered,
            plan_revision=execution.plan_revision,
            execution_result=execution,
            evaluation=evaluation,
            decision=decision,
        )


@pytest.mark.parametrize("decision_kind", ["seal", "patch"])
def test_claim_coverage_checkpoint_roundtrips_the_exact_execution_branch(
    decision_kind: str,
) -> None:
    execution = _execution(succeeded=decision_kind == "seal")
    evaluation = evaluate_claim_coverage(
        authority_context=_authority_context(),
        plan_revision=execution.plan_revision,
        execution_result=execution,
        route_catalog=_registry(),
    )
    if decision_kind == "seal":
        decision = PlanExpansionDecision.from_provider_audit(
            evaluation=evaluation,
            provider_audit=_provider_audit(
                {"decision": "seal", "selected_axis_ids": []}
            ),
        )
        patch = None
    else:
        decision = PlanExpansionDecision.from_provider_audit(
            evaluation=evaluation,
            provider_audit=_provider_audit(
                {
                    "decision": "patch",
                    "selected_axis_ids": ["time_context"],
                }
            ),
        )
        patch = PlanPatch.create(
            plan_revision=execution.plan_revision,
            execution_result=execution,
            evaluation=evaluation,
            decision=decision,
        )
    transition = _coverage_transition(
        execution=execution,
        evaluation=evaluation,
        decision=decision,
        plan_patch=patch,
    )

    checkpoint = ClaimCoverageCheckpoint.create(
        plan_revision=execution.plan_revision,
        execution_result=execution,
        evaluation=evaluation,
        decision=decision,
        plan_patch=patch,
        transition=transition,
    )

    assert checkpoint.source_plan_revision_id == execution.plan_revision_id
    assert (
        checkpoint.source_execution_result_ref
        == execution.authoritative_execution_result_ref
    )
    assert checkpoint.transition_id == transition.transition_id
    assert checkpoint.plan_patch_ref == (
        None if patch is None else patch.plan_patch_ref
    )
    assert (
        ClaimCoverageCheckpoint.from_dict(
            checkpoint.to_dict(),
            authority_context=_authority_context(),
            plan_revision=execution.plan_revision,
            execution_result=execution,
            route_catalog=_registry(),
        )
        == checkpoint
    )


def test_claim_coverage_checkpoint_rejects_a_transition_from_another_parent() -> None:
    execution = _execution(succeeded=True)
    evaluation = evaluate_claim_coverage(
        authority_context=_authority_context(),
        plan_revision=execution.plan_revision,
        execution_result=execution,
        route_catalog=_registry(),
    )
    decision = PlanExpansionDecision.from_provider_audit(
        evaluation=evaluation,
        provider_audit=_provider_audit({"decision": "seal", "selected_axis_ids": []}),
    )
    transition = _coverage_transition(
        execution=execution,
        evaluation=evaluation,
        decision=decision,
        plan_patch=None,
    )
    with pytest.raises(
        ClaimCoverageContractError,
        match="claim_coverage_checkpoint_transition_invalid",
    ):
        ClaimCoverageCheckpoint.create(
            plan_revision=execution.plan_revision,
            execution_result=execution,
            evaluation=evaluation,
            decision=decision,
            plan_patch=None,
            transition=replace(
                transition,
                parent_transition_id="transition:other-execution",
            ),
        )
