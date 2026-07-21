from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from bi_agent.runtime.authoritative_execution_result import (
    AuthoritativeExecutionResult,
)
from bi_agent.runtime.capability_authority import (
    CapabilityAdapterOutput,
    CapabilityAttempt,
    CapabilityEvidence,
    CapabilityOutcome,
    EvidenceLedgerEntry,
    ExecutionSnapshot,
    ExplorationStopRecord,
)
from bi_agent.runtime.capability_scheduler import (
    capability_execution_transition_payloads,
)
from bi_agent.runtime.claim_authority import ClaimAuthorityNamespace
from bi_agent.runtime.claim_coverage import (
    ClaimEvidenceCoverageAssessment,
    ClaimCoverageCheckpoint,
    ClaimCoverageEvaluation,
    ClaimObligationCoverage,
    PlanExpansionDecision,
    claim_coverage_transition_payloads,
)
from bi_agent.runtime.claim_settlement import (
    AuthorityBundleInputs,
    evidence_publication_ceiling,
)
from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value
from bi_agent.runtime.durable_call_journal import (
    DurableCallSpec,
    InMemoryDurableCallJournal,
)
from bi_agent.runtime.llm_client import LLMResult
from bi_agent.runtime.narrative_authority import RestrictedProviderResponse
from bi_agent.runtime.plan_authority import (
    AnalysisAxis,
    AuthorityContext,
    ClaimObligation,
    EvidenceRequirement,
    PlanRevision,
)
from bi_agent.runtime.single_authority import (
    DecisionLedger,
    DecisionRecord,
    DurableTransition,
    IntentRevision,
    LifecycleState,
)
from bi_agent.runtime.semantic_authority_workflow import (
    SemanticAuthorityResult,
    run_semantic_authority_workflow,
)
from tests.support.temporal_authority import resolved_test_temporal_authority


ROOT = Path(__file__).resolve().parents[2]


def _forbidden_internal_replay(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("typed_internal_authority_must_not_replay_serialized_children")


@dataclass(frozen=True)
class _Fixture:
    intent: IntentRevision
    decision: DecisionRecord
    context: AuthorityContext
    execution: AuthoritativeExecutionResult
    authority_inputs: AuthorityBundleInputs
    bundle: Any
    lifecycle: LifecycleState
    transition_input: Mapping[str, Any]
    transition_output: Mapping[str, Any]
    provider_responses: tuple[RestrictedProviderResponse, ...]
    semantic_result: SemanticAuthorityResult
    claim_coverage_checkpoint: ClaimCoverageCheckpoint
    claim_coverage_transition_input: Mapping[str, Any]
    claim_coverage_transition_output: Mapping[str, Any]
    settlement_transition: DurableTransition
    settlement_transition_input: Mapping[str, Any]
    settlement_transition_output: Mapping[str, Any]


class _SemanticLLM:
    supports_output_validator = True

    def __init__(self) -> None:
        self.calls = 0

    def invoke_json(self, **kwargs: Any) -> LLMResult:
        call_input = json.loads(kwargs["messages"][1]["content"])
        task = str(kwargs["task"])
        if task.endswith("candidate_claim_proposal"):
            output = {"candidate_claim_proposals": []}
        elif task.endswith("claim_verification"):
            output = {
                "decisions": {
                    item["claim_ref"]: {
                        "disposition": "accepted",
                        "veto_basis": None,
                        "reason_code": None,
                        "limitation_refs": [],
                    }
                    for item in call_input["payload"]["proposed_claims"]
                }
            }
        elif task.endswith("recommendation_proposal"):
            claim_ref = call_input["payload"]["verified_claims"][0]["claim_ref"]
            action = "Inspect the paid amount change."
            expected_value = "Preserve the accepted baseline."
            output = {
                "recommendation_proposals": [
                    {
                        "commitment_contract_version": (
                            "recommendation-commitments.v1"
                        ),
                        "commitments": [
                            {
                                "commitment_kind": "action",
                                "text": action,
                                "supporting_claim_refs": [claim_ref],
                                "diagnostic_mode": None,
                                "action_domain": "analysis",
                                "action_stage": "investigate",
                                "expected_value_kind": None,
                                "expected_value_mode": None,
                            },
                            {
                                "commitment_kind": "expected_outcome",
                                "text": expected_value,
                                "supporting_claim_refs": [claim_ref],
                                "diagnostic_mode": None,
                                "action_domain": None,
                                "action_stage": None,
                                "expected_value_kind": "information_gain",
                                "expected_value_mode": "expected_effect",
                            },
                        ],
                        "supporting_claim_refs": [claim_ref],
                        "assumption_refs": [],
                        "risk_refs": [],
                        "action": action,
                        "applicable_conditions": [
                            "The comparison contract remains complete."
                        ],
                        "expected_decision_value": expected_value,
                    }
                ]
            }
        elif task.endswith("recommendation_verification"):
            proposal_ref = call_input["payload"]["recommendation_proposal"][
                "recommendation_proposal_ref"
            ]
            output = {
                "decision": {
                    "subject_ref": proposal_ref,
                    "disposition": "accepted",
                    "veto_basis": None,
                    "reason_code": None,
                    "limitation_refs": [],
                    "verified_commitment_refs": call_input["payload"][
                        "recommendation_proposal"
                    ]["recommendation_commitment_refs"],
                }
            }
        else:
            raise AssertionError(f"unexpected semantic task: {task}")
        validator = kwargs["output_validator"]
        if validator is not None:
            validator(output)
        call_number = self.calls
        self.calls += 1
        return LLMResult(
            output=output,
            audit={
                "provider": "provider:test",
                "model": "semantic-authority:test",
                "prompt_version": kwargs["prompt_version"],
                "attempt_count": 1,
                "response_id": f"semantic-response:{call_number}",
                "raw_response_content": json.dumps(output, sort_keys=True),
                "messages": [dict(item) for item in kwargs["messages"]],
            },
        )


def _fixture(
    *,
    boundary_only: bool = False,
    candidate_zero: bool = False,
) -> _Fixture:
    run_attempt_id = "run-authority-seal"
    intent = IntentRevision.create(
        run_attempt_id=run_attempt_id,
        original_user_text="2026-06-19 paid amount change",
        goal_bindings=({"goal_id": "explain_change", "role": "primary"},),
        target_metric_refs=("paid_amount",),
        scope={"scope_type": "full_sample", "filters": []},
        time_spec={"kind": "date", "target": "2026-06-19"},
        comparison_spec={
            "kind": "decision_slot",
            "slot_id": "comparison_baseline",
        },
        direction_premise="unknown",
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
                "question": "请选择目标日比较基线。",
                "allowed_value_refs": ("previous_day",),
            },
        ),
        source_spans=(),
        schema_version="intent-revision.v1",
        prompt_version="test.intent.v1",
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
        option_id="comparison_baseline.previous_day",
    )
    context = AuthorityContext.create(
        run_attempt_id=run_attempt_id,
        actual_as_of="2026-07-18T00:00:00Z",
        release_refs=("release:paid-order-success:r1",),
        snapshot_refs=("snapshot:paid-order-success:r1",),
        dataset_coverage=(
            {
                "dataset_id": "paid_order_success",
                "availability": "claim_ready",
                "release_ref": "release:paid-order-success:r1",
                "snapshot_refs": ("snapshot:paid-order-success:r1",),
                "limitation_ref": None,
            },
        ),
        contract_versions={"runtime": "single-authority-phase03.v1"},
    )
    obligation = ClaimObligation.create(
        claim_kind="candidate_mechanism" if candidate_zero else "comparative_change",
        role="user_required",
        subject={
            "target_metric_ref": "paid_amount",
            "scope": {"market": "all", "currency": "USD"},
            "outcome_refs": ("outcome:explain_change",),
            "goal_refs": ("explain_change",),
        },
        evidence_requirement=EvidenceRequirement.create(
            operator="any_of",
            evidence_kinds=("observed",),
        ),
        success_policy={
            "policy": "verified_or_explicit_boundary",
            "minimum_claim_strength": "directional",
        },
    )
    axis = AnalysisAxis.create(
        axis_id="change_validation",
        role="required",
        axis_kind="comparison",
        target_metric_refs=("paid_amount",),
        metric_refs=(),
        dimension_refs=(),
        context_source_refs=(),
        capability_refs=("compare_periods",),
        reconciliation_group="paid_amount",
        selection_policy="retain_all_qualified_evidence",
        source_refs=("contract:test",),
        goal_refs=("explain_change",),
        supports_obligation_ids=(obligation.obligation_id,),
    )
    temporal_authority = resolved_test_temporal_authority(
        time_spec=intent.time_spec,
        comparison_spec=intent.comparison_spec,
        decision_ledger=DecisionLedger().append(decision),
        require_physical_baseline=True,
    )
    plan = PlanRevision.create(
        run_attempt_id=run_attempt_id,
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
                "task_key": "compare",
                "capability_id": "compare_periods",
                "normalized_input_refs": (
                    context.authority_context_ref,
                    *temporal_authority.resolved_window_refs,
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
                    "input_states": (),
                },
            },
        ),
        assumption_refs=(),
        budget_policy_ref="budget-policy:test",
        contract_versions=context.contract_versions,
    )
    task = plan.capability_tasks[0]
    attempt = CapabilityAttempt.create(plan, task)
    evidence_records = (
        ()
        if boundary_only
        else (
            CapabilityEvidence.create(
                evidence_ref="evidence:paid-amount-change",
                binding_record_ref="binding:paid-amount-change",
                execution_state="available",
                evidence_kind="observed",
                data_contract_state="complete",
                supported_claim_kinds=(
                    "candidate_mechanism" if candidate_zero else "comparative_change",
                ),
                evidence_strength="qualified",
                maximum_claim_strength=(
                    "candidate_mechanism" if candidate_zero else "directional"
                ),
                observation_facts=({"name": "absolute_change", "value": 12},),
                scope="scope:full-sample",
                window_refs=plan.resolved_window_refs,
                dimension_path=(),
                limitation_refs=(),
                result_refs=("result:compare",),
                completeness_report_refs=("completeness:compare",),
                hierarchy_qualified=False,
            ),
        )
    )
    output = CapabilityAdapterOutput.create(
        status="unavailable" if boundary_only else "succeeded",
        output_payload={} if boundary_only else {"absolute_change": 12},
        evidence=evidence_records,
        affected_obligation_ids=(obligation.obligation_id,),
        limitation_refs=("limitation:source-unavailable",) if boundary_only else (),
        retryability="replan_required" if boundary_only else "never",
        failure=None,
    )
    outcome = CapabilityOutcome.create(
        attempt,
        task,
        output,
        failure_ref=None,
        budget_units=1,
    )
    evidence_entries = tuple(
        EvidenceLedgerEntry.create(plan, task, outcome, evidence)
        for evidence in evidence_records
    )
    stop = ExplorationStopRecord.create(
        plan,
        (outcome,),
        reason="plan_exhausted",
        hard_budget_limit=None,
    )
    snapshot = ExecutionSnapshot.create(
        plan,
        stop,
        (outcome,),
        evidence_entries,
        (),
    )
    transition_input, transition_output = capability_execution_transition_payloads(
        plan, snapshot, stop
    )
    transition = DurableTransition.create(
        node_name="execute_capability_dag",
        parent_transition_id="transition:phase02-plan-bound",
        run_attempt_id=run_attempt_id,
        intent_revision_id=intent.intent_revision_id,
        decision_ledger_position=1,
        input_digest=canonical_digest(transition_input),
        output_digest=canonical_digest(transition_output),
        execution_attempt=1,
        provider_ref="waje-capability-runtime",
        model_ref="deterministic-capability-dag.v1",
        status="succeeded",
        acceptance_state="accepted",
        next_transition="phase03_evidence_bound",
        started_at="2026-07-18T00:00:00+00:00",
        finished_at="2026-07-18T00:00:01+00:00",
    )
    execution = AuthoritativeExecutionResult.from_records(
        plan_revision=plan,
        execution_snapshot=snapshot,
        exploration_stop_record=stop,
        capability_outcome_bundles=((attempt, outcome, evidence_entries, ()),),
        durable_transition=transition,
    )
    namespace = ClaimAuthorityNamespace.create(
        run_attempt_id=run_attempt_id,
        intent_revision_id=execution.intent_revision_id,
        plan_revision_id=execution.plan_revision_id,
    )
    semantic_result = run_semantic_authority_workflow(
        execution,
        authority_namespace=namespace,
        llm_client=_SemanticLLM(),
    )
    authority_inputs = semantic_result.authority_bundle_inputs
    bundle = authority_inputs.seal(
        bundle_revision=1,
        supersedes_bundle_ref=None,
        sealed_at="2026-07-18T00:00:02Z",
    )
    from bi_agent.runtime.authority_seal_persistence import (
        semantic_authority_transition_payloads,
    )

    evidence_assessments = tuple(
        ClaimEvidenceCoverageAssessment.create(
            evidence_entry_ref=entry.entry_ref,
            settlement_outcome_ref=outcome.outcome_ref,
            binding_record_ref=entry.binding_record_ref,
            evidence_kind=entry.evidence_kind,
            evidence_strength=entry.evidence_strength,
            maximum_claim_strength=entry.maximum_claim_strength,
            publication_ceiling=evidence_publication_ceiling(
                evidence_kind=entry.evidence_kind,
                source_claim_kind=obligation.claim_kind,
                maximum_claim_strength=entry.maximum_claim_strength,
            ).to_dict(),
            data_contract_state=entry.data_contract_state,
            supported_claim_kinds=entry.supported_claim_kinds,
            observation_facts=entry.observation_facts,
            scope=entry.scope,
            window_refs=entry.window_refs,
            dimension_path=entry.dimension_path,
            limitation_refs=entry.limitation_refs,
            result_refs=entry.result_refs,
            completeness_report_refs=entry.completeness_report_refs,
        )
        for entry in evidence_entries
    )
    obligation_coverage = ClaimObligationCoverage.create(
        obligation_id=obligation.obligation_id,
        claim_kind=obligation.claim_kind,
        role=obligation.role,
        subject=obligation.subject,
        success_policy=obligation.success_policy,
        status="uncovered" if boundary_only else "evidence_present",
        evidence_assessments=evidence_assessments,
    )
    claim_coverage_evaluation = ClaimCoverageEvaluation.create(
        plan_revision=plan,
        execution_result=execution,
        obligation_coverages=(obligation_coverage,),
        admissible_routes=(),
    )
    claim_coverage_decision = PlanExpansionDecision.deterministic_seal(
        claim_coverage_evaluation
    )
    (
        claim_coverage_transition_input,
        claim_coverage_transition_output,
    ) = claim_coverage_transition_payloads(
        evaluation=claim_coverage_evaluation,
        decision=claim_coverage_decision,
        plan_patch=None,
    )
    claim_coverage_transition = DurableTransition.create(
        node_name="evaluate_claim_coverage",
        parent_transition_id=execution.transition_id,
        run_attempt_id=run_attempt_id,
        intent_revision_id=intent.intent_revision_id,
        decision_ledger_position=transition.decision_ledger_position,
        input_digest=canonical_digest(claim_coverage_transition_input),
        output_digest=canonical_digest(claim_coverage_transition_output),
        execution_attempt=1,
        provider_ref="local_deterministic",
        model_ref="claim-coverage-contract.v1",
        status="succeeded",
        acceptance_state="accepted",
        next_transition="seal_authority_bundle",
        started_at="2026-07-18T00:00:01+00:00",
        finished_at="2026-07-18T00:00:02+00:00",
    )
    claim_coverage_checkpoint = ClaimCoverageCheckpoint.create(
        plan_revision=plan,
        execution_result=execution,
        evaluation=claim_coverage_evaluation,
        decision=claim_coverage_decision,
        plan_patch=None,
        transition=claim_coverage_transition,
    )

    settlement_transition_input, settlement_transition_output = (
        semantic_authority_transition_payloads(
            semantic_result,
            bundle,
            claim_coverage_checkpoint_ref=(claim_coverage_checkpoint.checkpoint_ref),
            claim_coverage_checkpoint_digest=(claim_coverage_checkpoint.content_digest),
        )
    )
    settlement_transition = DurableTransition.create(
        node_name="settle_claim_authority",
        parent_transition_id=claim_coverage_checkpoint.transition_id,
        run_attempt_id=run_attempt_id,
        intent_revision_id=intent.intent_revision_id,
        decision_ledger_position=transition.decision_ledger_position,
        input_digest=canonical_digest(settlement_transition_input),
        output_digest=canonical_digest(settlement_transition_output),
        execution_attempt=1,
        provider_ref="waje-semantic-authority",
        model_ref="single-authority-phase04.v1",
        status="succeeded",
        acceptance_state="accepted",
        next_transition="compose_claim_aware_narrative",
        started_at="2026-07-18T00:00:01+00:00",
        finished_at="2026-07-18T00:00:02+00:00",
    )
    lifecycle = LifecycleState.create(
        run_attempt_id=run_attempt_id,
        execution_state="complete",
        evidence_state="partial",
        publication_state="not_ready",
        cancellation_state="active",
        supersession_state="active",
    )
    return _Fixture(
        intent=intent,
        decision=decision,
        context=context,
        execution=execution,
        authority_inputs=authority_inputs,
        bundle=bundle,
        lifecycle=lifecycle,
        transition_input=transition_input,
        transition_output=transition_output,
        provider_responses=semantic_result.provider_responses,
        semantic_result=semantic_result,
        claim_coverage_checkpoint=claim_coverage_checkpoint,
        claim_coverage_transition_input=claim_coverage_transition_input,
        claim_coverage_transition_output=claim_coverage_transition_output,
        settlement_transition=settlement_transition,
        settlement_transition_input=settlement_transition_input,
        settlement_transition_output=settlement_transition_output,
    )


class _Cursor:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class _Connection:
    def __init__(
        self,
        fixture: _Fixture,
        *,
        existing_bundle_payload: Mapping[str, Any] | None = None,
        closure_overrides: Mapping[str, Any] | None = None,
        empty_insert_table: str | None = None,
        execute_error: RuntimeError | None = None,
    ) -> None:
        self.fixture = fixture
        self.existing_bundle_payload = existing_bundle_payload
        self.closure_overrides = dict(closure_overrides or {})
        self.empty_insert_table = empty_insert_table
        self.execute_error = execute_error
        self.statements: list[tuple[str, Mapping[str, Any]]] = []
        self.commits = 0
        self.rollbacks = 0
        self.attempt_journal = InMemoryDurableCallJournal()

    def execute(self, statement: str, params: Mapping[str, Any] | None = None):
        params = dict(params or {})
        self.statements.append((statement, params))
        if self.execute_error is not None and "authority_seal_preflight" in statement:
            raise self.execute_error
        if "pg_advisory_xact_lock" in statement:
            return _Cursor([(1,)])
        if "authority_seal_preflight" in statement:
            execution = self.fixture.execution
            lifecycle = self.fixture.lifecycle
            if self.existing_bundle_payload is not None:
                lifecycle = lifecycle.transition(
                    evidence_state=(
                        "boundary_only"
                        if self.fixture.bundle.authority_mode == "boundary_only"
                        else "complete"
                    )
                )
            return _Cursor(
                [
                    {
                        "owner_ref": "owner:authority-seal",
                        "thread_ref": "thread:authority-seal",
                        "intent_payload": self.fixture.intent.to_dict(),
                        "intent_content_digest": self.fixture.intent.content_digest,
                        "authority_context_payload": self.fixture.context.to_dict(),
                        "authority_context_content_digest": (
                            self.fixture.context.content_digest
                        ),
                        "plan_payload": execution.plan_revision.to_dict(),
                        "plan_content_digest": execution.plan_revision.content_digest,
                        "execution_snapshot_payload": (
                            execution.execution_snapshot.to_dict()
                        ),
                        "execution_snapshot_content_digest": (
                            execution.execution_snapshot.content_digest
                        ),
                        "stop_payload": execution.exploration_stop_record.to_dict(),
                        "stop_content_digest": (
                            execution.exploration_stop_record.content_digest
                        ),
                        "transition_payload": execution.durable_transition.to_dict(),
                        "transition_input_payload": canonical_value(
                            self.fixture.transition_input
                        ),
                        "transition_output_payload": canonical_value(
                            self.fixture.transition_output
                        ),
                        "lifecycle_payload": lifecycle.to_dict(),
                        "existing_bundle_payload": self.existing_bundle_payload,
                    }
                ]
            )
        if "authority_seal_claim_coverage_checkpoint" in statement:
            checkpoint = self.fixture.claim_coverage_checkpoint
            assert params["checkpoint_transition_id"] == checkpoint.transition_id
            assert params["execution_transition_id"] == (
                self.fixture.execution.transition_id
            )
            replaying = self.existing_bundle_payload is not None
            expected_head = (
                self.fixture.settlement_transition.transition_id
                if replaying
                else checkpoint.transition_id
            )
            assert params["head_transition_id"] == expected_head
            default_row = {
                "checkpoint_transition_payload": (checkpoint.transition.to_dict()),
                "checkpoint_input_payload": canonical_value(
                    self.closure_overrides.get(
                        "claim_coverage_input_payload",
                        self.fixture.claim_coverage_transition_input,
                    )
                ),
                "checkpoint_output_payload": canonical_value(
                    self.fixture.claim_coverage_transition_output
                ),
                "settlement_transition_payload": (
                    self.fixture.settlement_transition.to_dict() if replaying else None
                ),
                "settlement_input_payload": (
                    canonical_value(self.fixture.settlement_transition_input)
                    if replaying
                    else None
                ),
                "settlement_output_payload": (
                    canonical_value(self.fixture.settlement_transition_output)
                    if replaying
                    else None
                ),
                "head_transition_id": expected_head,
            }
            rows = self.closure_overrides.get(
                "claim_coverage_rows",
                (default_row,),
            )
            return _Cursor(list(rows))
        if "authority_seal_active_decisions" in statement:
            return _Cursor([(self.fixture.decision.to_dict(),)])
        if "authority_seal_provider_response_closure" in statement:
            rows = [
                _provider_response_row(response)
                for response in self.fixture.provider_responses
            ]
            return _Cursor(self.closure_overrides.get("provider_responses", rows))
        for kind, expected in _execution_closure_payloads(self.fixture).items():
            if f"authority_seal_execution_closure:{kind}" in statement:
                rows = self.closure_overrides.get(kind, expected)
                return _Cursor([(item,) for item in rows])
        if "authority_seal_exact_replay:" in statement:
            table = statement.split("authority_seal_exact_replay:", 1)[1].split(
                " */", 1
            )[0]
            if table == self.empty_insert_table:
                return _Cursor([])
            identity = next(
                value
                for key, value in params.items()
                if key.endswith("_ref")
                or key in {"attempt_id", "claim_key", "state_revision"}
            )
            return _Cursor([(identity,)])
        if statement.lstrip().startswith("INSERT INTO waje_runtime."):
            table = statement.split("INSERT INTO waje_runtime.", 1)[1].split()[0]
            if (
                table == self.empty_insert_table
                or self.existing_bundle_payload is not None
            ):
                return _Cursor([])
            identity = next(
                value
                for key, value in params.items()
                if key.endswith("_ref")
                or key in {"attempt_id", "claim_key", "state_revision"}
            )
            return _Cursor([(identity,)])
        raise AssertionError(f"unexpected SQL: {statement}")

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _execution_closure_payloads(
    fixture: _Fixture,
) -> dict[str, list[Mapping[str, Any]]]:
    bundles = fixture.execution.capability_outcome_bundles
    return {
        "attempts": [item[0].to_dict() for item in bundles],
        "outcomes": [item[1].to_dict() for item in bundles],
        "evidence": [entry.to_dict() for item in bundles for entry in item[2]],
        "failures": [failure.to_dict() for item in bundles for failure in item[3]],
    }


def _provider_response_row(
    response: RestrictedProviderResponse,
    *,
    owner_ref: str = "owner:authority-seal",
    run_attempt_id: str = "run-authority-seal",
) -> dict[str, Any]:
    return {
        "provider_response_ref": response.response_ref,
        "owner_ref": owner_ref,
        "run_attempt_id": run_attempt_id,
        "attempt_id": response.attempt_id,
        "purpose": response.purpose,
        "provider_ref": response.provider_ref,
        "model_ref": response.model_ref,
        "input_ref": response.input_ref,
        "input_digest": response.input_digest,
        "attempt_number": response.attempt_number,
        "raw_response_content": response.content,
        "content_digest": response.content_digest,
        "payload": response.to_dict(),
    }


def _seal(
    connection: _Connection,
    fixture: _Fixture,
    *,
    provider_responses: tuple[RestrictedProviderResponse, ...] | None = None,
    semantic_result: SemanticAuthorityResult | None = None,
    settlement_transition: DurableTransition | None = None,
):
    from bi_agent.runtime.authority_seal_persistence import (
        seal_authority_bundle,
    )

    accepted_attempt_refs: list[str] = []
    for index, response in enumerate(fixture.provider_responses):
        input_payload = {
            "test_semantic_provider_call": response.purpose,
            "index": index,
        }
        input_digest = canonical_digest(input_payload)
        spec = DurableCallSpec.create(
            run_attempt_id=fixture.bundle.run_attempt_id,
            intent_revision_id=fixture.bundle.intent_revision_id,
            plan_revision_id=fixture.bundle.plan_revision_id,
            task_id=None,
            stage_name="settle_claim_authority",
            call_kind="semantic_provider",
            operation_name=f"test_{response.purpose}_{index}",
            input_ref="provider-call-input:sha256:" + input_digest,
            input_payload=input_payload,
        )
        claim = connection.attempt_journal.claim(spec)
        if claim.replayed:
            accepted_attempt_refs.append(claim.attempt.attempt_ref)
            continue
        completion = connection.attempt_journal.succeed(
            claim.attempt,
            {"output": {"accepted": True}, "audit": {"index": index}},
        )
        assert completion.acceptance is not None
        accepted_attempt_refs.append(completion.acceptance.accepted_attempt_ref)

    return seal_authority_bundle(
        connection,
        owner_ref="owner:authority-seal",
        thread_ref="thread:authority-seal",
        authority_inputs=fixture.authority_inputs,
        authority_bundle=fixture.bundle,
        provider_responses=(
            fixture.provider_responses
            if provider_responses is None
            else provider_responses
        ),
        semantic_authority_result=semantic_result or fixture.semantic_result,
        claim_coverage_checkpoint=fixture.claim_coverage_checkpoint,
        settlement_transition=(settlement_transition or fixture.settlement_transition),
        attempt_journal=connection.attempt_journal,
        accepted_attempt_refs=tuple(accepted_attempt_refs),
    )


def test_typed_authority_inputs_do_not_replay_validated_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture()
    settlement_type = type(fixture.semantic_result.settlement)
    recommendation_type = type(fixture.semantic_result.recommendations[0])
    monkeypatch.setattr(
        AuthoritativeExecutionResult,
        "from_dict",
        classmethod(_forbidden_internal_replay),
    )
    monkeypatch.setattr(
        settlement_type,
        "from_dict",
        classmethod(_forbidden_internal_replay),
    )
    monkeypatch.setattr(
        recommendation_type,
        "from_dict",
        classmethod(_forbidden_internal_replay),
    )

    rebuilt = AuthorityBundleInputs.create(
        execution_result=fixture.execution,
        claim_settlement=fixture.semantic_result.settlement,
        recommendations=fixture.semantic_result.recommendations,
    )

    assert rebuilt == fixture.authority_inputs


def test_typed_authority_bundle_seal_does_not_replay_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture()
    monkeypatch.setattr(
        AuthorityBundleInputs,
        "replay",
        _forbidden_internal_replay,
    )

    rebuilt = fixture.authority_inputs.seal(
        bundle_revision=fixture.bundle.bundle_revision,
        supersedes_bundle_ref=fixture.bundle.supersedes_bundle_ref,
        sealed_at=fixture.bundle.sealed_at,
    )

    assert rebuilt == fixture.bundle


def test_authority_persistence_validates_typed_identity_without_deep_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture()
    monkeypatch.setattr(
        AuthorityBundleInputs,
        "from_dict",
        classmethod(_forbidden_internal_replay),
    )
    monkeypatch.setattr(
        SemanticAuthorityResult,
        "from_dict",
        classmethod(_forbidden_internal_replay),
    )
    monkeypatch.setattr(
        type(fixture.bundle),
        "from_dict",
        classmethod(_forbidden_internal_replay),
    )

    result = _seal(_Connection(fixture), fixture)

    assert result.status == "inserted"


def test_seal_persists_the_full_authority_closure_in_one_transaction() -> None:
    fixture = _fixture()
    connection = _Connection(fixture)

    result = _seal(connection, fixture)

    assert result.status == "inserted"
    assert result.bundle_ref == fixture.bundle.bundle_ref
    sealed_lifecycle = fixture.lifecycle.transition(evidence_state="complete")
    assert result.lifecycle_state_digest == sealed_lifecycle.content_digest
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert len(
        connection.attempt_journal.load_stage_attempt_refs(
            run_attempt_id=fixture.bundle.run_attempt_id,
            transition_attempt_id=fixture.settlement_transition.attempt_id,
            stage_name="settle_claim_authority",
        )
    ) == len(fixture.provider_responses)
    sql = "\n".join(statement for statement, _ in connection.statements)
    expected_tables = {
        "restricted_provider_responses",
        "claim_authority_namespaces",
        "claim_keys",
        "claim_support_edges",
        "claim_revisions",
        "claim_settlement_checkpoints",
        "claim_obligation_settlement_bases",
        "claim_verification_attempts",
        "claim_verification_decisions",
        "claim_verification_reports",
        "claim_obligation_coverages",
        "claim_graphs",
        "claim_settlements",
        "recommendation_proposals",
        "recommendation_verification_attempts",
        "recommendation_verification_decisions",
        "recommendation_records",
        "authority_bundles",
        "workflow_transition_attempts",
        "run_lifecycle_state_revisions",
    }
    assert all(f"waje_runtime.{table}" in sql for table in expected_tables)
    assert sql.index("pg_advisory_xact_lock") < sql.index("authority_seal_preflight")
    lock_params = next(
        params
        for statement, params in connection.statements
        if "pg_advisory_xact_lock" in statement
    )
    assert lock_params["lock_key"] == "single_authority:run-authority-seal"
    lifecycle_insert = next(
        params
        for statement, params in connection.statements
        if "INSERT INTO waje_runtime.run_lifecycle_state_revisions" in statement
    )
    assert lifecycle_insert["state_revision"] == sealed_lifecycle.state_revision
    assert lifecycle_insert["prior_state_digest"] == fixture.lifecycle.content_digest
    assert lifecycle_insert["execution_state"] == "complete"
    assert lifecycle_insert["evidence_state"] == "complete"
    assert lifecycle_insert["publication_state"] == "not_ready"
    assert "authority_sealed" not in sql
    assert "DO UPDATE" not in sql
    assert "response.raw_response_content" in sql
    assert "response.content_digest" in sql
    assert "response.response_digest" not in sql
    assert "WHEN plan.supersedes_plan_revision_id IS NULL" in sql
    assert "THEN 'compile_authoritative_plan'" in sql
    assert "ELSE 'compile_plan_patch'" in sql
    assert sql.index(
        "INSERT INTO waje_runtime.restricted_provider_responses"
    ) < sql.index("authority_seal_provider_response_closure")
    response_inserts = [
        params
        for statement, params in connection.statements
        if "INSERT INTO waje_runtime.restricted_provider_responses" in statement
    ]
    assert len(response_inserts) == len(fixture.provider_responses) == 3
    assert {item["purpose"] for item in response_inserts} == {
        "claim_verification",
        "recommendation_proposal",
        "recommendation_verification",
    }
    transition_insert = next(
        params
        for statement, params in connection.statements
        if "INSERT INTO waje_runtime.workflow_transition_attempts" in statement
    )
    assert transition_insert["node_name"] == "settle_claim_authority"
    assert transition_insert["parent_transition_id"] == (
        fixture.claim_coverage_checkpoint.transition_id
    )
    assert transition_insert["next_transition"] == "compose_claim_aware_narrative"
    assert transition_insert["failure_ref"] is None
    assert json.loads(transition_insert["input_payload"]) == canonical_value(
        fixture.settlement_transition_input
    )
    transition_output = json.loads(transition_insert["output_payload"])
    assert transition_output == canonical_value(fixture.settlement_transition_output)
    hydrated = SemanticAuthorityResult.from_dict(
        transition_output["semantic_authority_result"]
    )
    assert hydrated == fixture.semantic_result


def test_seal_requires_the_persisted_claim_coverage_checkpoint() -> None:
    fixture = _fixture()
    connection = _Connection(
        fixture,
        closure_overrides={"claim_coverage_rows": ()},
    )

    with pytest.raises(
        ValueError,
        match="^authority_seal_claim_coverage_checkpoint_missing$",
    ):
        _seal(connection, fixture)

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert not any(
        "INSERT INTO waje_runtime.authority_bundles" in statement
        for statement, _ in connection.statements
    )


def test_seal_rejects_persisted_claim_coverage_input_drift() -> None:
    fixture = _fixture()
    connection = _Connection(
        fixture,
        closure_overrides={
            "claim_coverage_input_payload": {
                **fixture.claim_coverage_transition_input,
                "source_execution_result_digest": "0" * 64,
            }
        },
    )

    with pytest.raises(
        ValueError,
        match="^authority_seal_claim_coverage_checkpoint_conflict$",
    ):
        _seal(connection, fixture)

    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_exact_bundle_and_child_replay_is_idempotent() -> None:
    fixture = _fixture()
    connection = _Connection(
        fixture,
        existing_bundle_payload=fixture.bundle.to_dict(),
    )

    result = _seal(connection, fixture)

    assert result.status == "replayed"
    assert connection.commits == 1
    assert connection.rollbacks == 0
    sql = "\n".join(statement for statement, _ in connection.statements)
    assert "authority_seal_exact_replay:authority_bundles" in sql
    assert "authority_seal_exact_replay:restricted_provider_responses" in sql
    assert "authority_seal_exact_replay:workflow_transition_attempts" in sql
    assert "authority_seal_exact_replay:run_lifecycle_state_revisions" in sql
    assert "INSERT INTO waje_runtime.restricted_provider_responses" not in sql
    assert "INSERT INTO waje_runtime.workflow_transition_attempts" not in sql
    assert "INSERT INTO waje_runtime.run_lifecycle_state_revisions" not in sql
    assert "DO UPDATE" not in sql


@pytest.mark.parametrize(
    ("missing_table", "collision"),
    (
        (
            "restricted_provider_responses",
            "restricted_provider_response",
        ),
        (
            "workflow_transition_attempts",
            "settle_claim_authority_transition",
        ),
        (
            "run_lifecycle_state_revisions",
            "authority_seal_lifecycle",
        ),
    ),
)
def test_sealed_run_replay_rejects_a_missing_semantic_checkpoint_record(
    missing_table: str,
    collision: str,
) -> None:
    fixture = _fixture()
    connection = _Connection(
        fixture,
        existing_bundle_payload=fixture.bundle.to_dict(),
        empty_insert_table=missing_table,
    )

    with pytest.raises(
        ValueError,
        match=f"authority_seal_immutable_conflict:{collision}",
    ):
        _seal(connection, fixture)

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert not any(
        f"INSERT INTO waje_runtime.{missing_table}" in statement
        for statement, _ in connection.statements
    )


def test_boundary_only_seal_uses_local_authority_without_semantic_attempt() -> None:
    fixture = _fixture(boundary_only=True)
    connection = _Connection(fixture)

    result = _seal(connection, fixture)

    assert result.status == "inserted"
    assert fixture.bundle.authority_mode == "boundary_only"
    assert (
        result.lifecycle_state_digest
        == fixture.lifecycle.transition(evidence_state="boundary_only").content_digest
    )
    sql = "\n".join(statement for statement, _ in connection.statements)
    assert "INSERT INTO waje_runtime.local_boundary_authorities" in sql
    assert "waje_runtime.claim_verification_reports" in sql
    assert "INSERT INTO waje_runtime.claim_verification_attempts" not in sql
    assert "INSERT INTO waje_runtime.claim_verification_decisions" not in sql
    assert "authority_seal_provider_response_closure" in sql
    assert "INSERT INTO waje_runtime.restricted_provider_responses" not in sql
    assert "INSERT INTO waje_runtime.run_lifecycle_state_revisions" in sql


def test_zero_candidate_boundary_retains_its_audited_proposal_response() -> None:
    fixture = _fixture(candidate_zero=True)
    connection = _Connection(fixture)

    result = _seal(connection, fixture)

    assert result.status == "inserted"
    assert fixture.bundle.authority_mode == "boundary_only"
    assert [item.purpose for item in fixture.provider_responses] == [
        "candidate_claim_proposal"
    ]
    assert fixture.semantic_result.claim_verification_attempt is None
    response_insert = next(
        params
        for statement, params in connection.statements
        if "INSERT INTO waje_runtime.restricted_provider_responses" in statement
    )
    assert response_insert["purpose"] == "candidate_claim_proposal"
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_run_cannot_be_resealed_with_a_different_bundle_digest() -> None:
    fixture = _fixture()
    conflicting = fixture.bundle.to_dict()
    conflicting["bundle_digest"] = "0" * 64
    connection = _Connection(fixture, existing_bundle_payload=conflicting)

    with pytest.raises(ValueError, match="authority_bundle_run_seal_conflict"):
        _seal(connection, fixture)

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert not any(
        statement.lstrip().startswith("INSERT INTO")
        for statement, _ in connection.statements
    )


def test_execution_child_set_must_match_the_authoritative_result_exactly() -> None:
    fixture = _fixture()
    connection = _Connection(fixture, closure_overrides={"evidence": []})

    with pytest.raises(ValueError, match="authority_seal_execution_evidence_conflict"):
        _seal(connection, fixture)

    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_semantic_attempts_require_the_exact_restricted_provider_response() -> None:
    fixture = _fixture()
    connection = _Connection(
        fixture,
        closure_overrides={"provider_responses": []},
    )

    with pytest.raises(
        ValueError,
        match="authority_seal_provider_response_set_conflict",
    ):
        _seal(connection, fixture)

    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_restricted_provider_response_columns_must_match_the_typed_payload() -> None:
    fixture = _fixture()
    response = fixture.provider_responses[0]
    conflicting_row = {
        "provider_response_ref": response.response_ref,
        "attempt_id": response.attempt_id,
        "purpose": response.purpose,
        "provider_ref": response.provider_ref,
        "model_ref": response.model_ref,
        "input_ref": response.input_ref,
        "input_digest": response.input_digest,
        "attempt_number": response.attempt_number,
        "raw_response_content": response.content,
        "content_digest": "0" * 64,
        "payload": response.to_dict(),
    }
    connection = _Connection(
        fixture,
        closure_overrides={"provider_responses": [conflicting_row]},
    )

    with pytest.raises(
        ValueError,
        match="authority_seal_provider_response_identity_conflict",
    ):
        _seal(connection, fixture)

    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_provider_response_argument_must_equal_the_semantic_checkpoint_exactly() -> (
    None
):
    fixture = _fixture()
    connection = _Connection(fixture)

    with pytest.raises(
        ValueError,
        match="authority_seal_provider_response_set_conflict",
    ):
        _seal(
            connection,
            fixture,
            provider_responses=fixture.provider_responses[:-1],
        )

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert not any(
        "INSERT INTO waje_runtime.restricted_provider_responses" in statement
        for statement, _ in connection.statements
    )


def test_extra_retry_response_not_present_in_checkpoint_is_rejected() -> None:
    fixture = _fixture()
    proposal_response = next(
        item
        for item in fixture.provider_responses
        if item.purpose == "recommendation_proposal"
    )
    extra = RestrictedProviderResponse.create(
        attempt_id="recommendation-proposal:unexpected-retry",
        purpose=proposal_response.purpose,
        provider_ref=proposal_response.provider_ref,
        model_ref=proposal_response.model_ref,
        input_ref=proposal_response.input_ref,
        input_digest=proposal_response.input_digest,
        attempt_number=2,
        content='{"recommendation_proposals":[]}',
    )
    connection = _Connection(fixture)

    with pytest.raises(
        ValueError,
        match="authority_seal_provider_response_set_conflict",
    ):
        _seal(
            connection,
            fixture,
            provider_responses=(*fixture.provider_responses, extra),
        )

    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_tampered_provider_response_is_rejected_before_any_insert() -> None:
    fixture = _fixture()
    tampered = replace(fixture.provider_responses[0], content='{"forged":true}')
    connection = _Connection(fixture)

    with pytest.raises(
        ValueError,
        match="authority_seal_provider_responses_invalid",
    ):
        _seal(
            connection,
            fixture,
            provider_responses=(tampered, *fixture.provider_responses[1:]),
        )

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert not any(
        "INSERT INTO waje_runtime.restricted_provider_responses" in statement
        for statement, _ in connection.statements
    )


def test_foreign_owner_or_run_provider_response_row_rolls_back_the_seal() -> None:
    fixture = _fixture()
    rows = [_provider_response_row(item) for item in fixture.provider_responses]
    rows[0] = _provider_response_row(
        fixture.provider_responses[0],
        owner_ref="owner:foreign",
        run_attempt_id="run-foreign",
    )
    connection = _Connection(
        fixture,
        closure_overrides={"provider_responses": rows},
    )

    with pytest.raises(
        ValueError,
        match="authority_seal_provider_response_identity_conflict",
    ):
        _seal(connection, fixture)

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert any(
        "INSERT INTO waje_runtime.restricted_provider_responses" in statement
        for statement, _ in connection.statements
    )


def test_extra_stored_phase4_response_rolls_back_all_atomic_inserts() -> None:
    fixture = _fixture()
    base = next(
        item
        for item in fixture.provider_responses
        if item.purpose == "recommendation_proposal"
    )
    extra = RestrictedProviderResponse.create(
        attempt_id="recommendation-proposal:stored-extra",
        purpose=base.purpose,
        provider_ref=base.provider_ref,
        model_ref=base.model_ref,
        input_ref=base.input_ref,
        input_digest=base.input_digest,
        attempt_number=2,
        content='{"recommendation_proposals":[]}',
    )
    rows = [
        *(_provider_response_row(item) for item in fixture.provider_responses),
        _provider_response_row(extra),
    ]
    connection = _Connection(
        fixture,
        closure_overrides={"provider_responses": rows},
    )

    with pytest.raises(
        ValueError,
        match="authority_seal_provider_response_set_conflict",
    ):
        _seal(connection, fixture)

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert any(
        "INSERT INTO waje_runtime.restricted_provider_responses" in statement
        for statement, _ in connection.statements
    )
    assert not any(
        "INSERT INTO waje_runtime.authority_bundles" in statement
        for statement, _ in connection.statements
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("parent_transition_id", "transition:execute-capability-dag"),
        ("next_transition", "unexpected_node"),
        ("provider_ref", "foreign-semantic-authority"),
        ("model_ref", "foreign-semantic-authority.v1"),
    ),
)
def test_settlement_transition_contract_tampering_rolls_back_without_checkpoint(
    field: str,
    value: str,
) -> None:
    fixture = _fixture()
    tampered = replace(fixture.settlement_transition, **{field: value})
    connection = _Connection(fixture)

    with pytest.raises(
        ValueError,
        match="authority_seal_settlement_transition_invalid",
    ):
        _seal(connection, fixture, settlement_transition=tampered)

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert not any(
        "INSERT INTO waje_runtime.workflow_transition_attempts" in statement
        for statement, _ in connection.statements
    )


def test_later_closure_failure_rolls_back_provider_and_transition_checkpoint() -> None:
    fixture = _fixture()
    connection = _Connection(fixture, closure_overrides={"evidence": []})

    with pytest.raises(
        ValueError,
        match="authority_seal_execution_evidence_conflict",
    ):
        _seal(connection, fixture)

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert any(
        "INSERT INTO waje_runtime.restricted_provider_responses" in statement
        for statement, _ in connection.statements
    )
    assert not any(
        "INSERT INTO waje_runtime.workflow_transition_attempts" in statement
        for statement, _ in connection.statements
    )


def test_cancelled_or_superseded_lifecycle_cannot_seal() -> None:
    fixture = _fixture()
    for field in ("cancellation_state", "supersession_state"):
        lifecycle = fixture.lifecycle.transition(
            **(
                {field: "cancelled"}
                if field == "cancellation_state"
                else {field: "superseded"}
            )
        )
        connection = _Connection(replace(fixture, lifecycle=lifecycle))
        with pytest.raises(ValueError, match="authority_seal_lifecycle_not_active"):
            _seal(connection, replace(fixture, lifecycle=lifecycle))
        assert connection.commits == 0
        assert connection.rollbacks == 1


def test_non_idle_lifecycle_cannot_enter_or_replay_authority_seal() -> None:
    fixture = _fixture()
    non_idle = fixture.lifecycle.transition(retry_state="running")
    pending_connection = _Connection(replace(fixture, lifecycle=non_idle))

    with pytest.raises(ValueError, match="authority_seal_lifecycle_not_ready"):
        _seal(pending_connection, replace(fixture, lifecycle=non_idle))

    assert pending_connection.commits == 0
    assert pending_connection.rollbacks == 1

    replay_connection = _Connection(
        replace(fixture, lifecycle=non_idle),
        existing_bundle_payload=fixture.bundle.to_dict(),
    )
    with pytest.raises(ValueError, match="authority_seal_lifecycle_not_ready"):
        _seal(replay_connection, replace(fixture, lifecycle=non_idle))

    assert replay_connection.commits == 0
    assert replay_connection.rollbacks == 1


def test_immutable_child_conflict_rolls_back_the_complete_seal() -> None:
    fixture = _fixture()
    connection = _Connection(fixture, empty_insert_table="claim_graphs")

    with pytest.raises(
        ValueError,
        match="authority_seal_immutable_conflict:claim_graph",
    ):
        _seal(connection, fixture)

    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_lifecycle_insert_conflict_rolls_back_the_complete_seal() -> None:
    fixture = _fixture()
    connection = _Connection(
        fixture,
        empty_insert_table="run_lifecycle_state_revisions",
    )

    with pytest.raises(
        ValueError,
        match="authority_seal_immutable_conflict:authority_seal_lifecycle",
    ):
        _seal(connection, fixture)

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert any(
        "INSERT INTO waje_runtime.authority_bundles" in statement
        for statement, _ in connection.statements
    )


def test_database_error_is_rolled_back_and_propagated_unchanged() -> None:
    fixture = _fixture()
    error = RuntimeError("database connection disappeared")
    connection = _Connection(fixture, execute_error=error)

    with pytest.raises(RuntimeError) as captured:
        _seal(connection, fixture)

    assert captured.value is error
    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_phase46_schema_has_the_seal_tables_and_one_bundle_per_run() -> None:
    schema = (ROOT / "tools/runtime/conversation-runtime.sql").read_text(
        encoding="utf-8"
    )
    phase46 = schema[schema.index("-- vNext Phase 4-6 sealed authority") :]
    assert (
        "CREATE TABLE IF NOT EXISTS waje_runtime.claim_settlement_checkpoints"
        in phase46
    )
    assert (
        "CREATE TABLE IF NOT EXISTS "
        "waje_runtime.claim_obligation_settlement_bases" in phase46
    )
    assert "CREATE TABLE IF NOT EXISTS waje_runtime.authority_bundles" in phase46
    assert "idx_authority_bundles_one_sealed_per_run" in phase46
    assert "run_lifecycle_state_revisions" in phase46
    assert "authority_sealed" not in phase46
