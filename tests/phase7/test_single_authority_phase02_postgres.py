from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from bi_agent.conversation.postgres_store import PostgresConversationStore
from bi_agent.runtime.durable_call_journal import DurableCallSpec
from bi_agent.runtime.evidence_authority import EvidenceIntegrityError, canonical_digest
from bi_agent.runtime.plan_authority import (
    AnalysisAxis,
    AuthorityContext,
    ClaimObligation,
    EvidenceRequirement,
    PlanRevision,
    PlannerProposal,
    ProposalAdmissionRecord,
)
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)
from bi_agent.runtime.single_authority import DurableTransition, IntentRevision
from tests.support.temporal_authority import resolved_test_temporal_authority


ROOT = Path(__file__).resolve().parents[2]
QUESTION = "2026年6月1日付费金额为什么上涨？主要由哪些指标变化导致？"
SCHEMA_PROVIDER_ATTEMPT_REF = "provider-call-attempt:sha256:" + "a" * 64


def _accepted_provider_attempt_ref(
    store: PostgresConversationStore,
    *,
    run_id: str,
    intent_revision_id: str | None,
    call_kind: str,
    stage_name: str,
) -> str:
    input_payload = {
        "test_provider_call": stage_name,
        "run_attempt_id": run_id,
    }
    input_digest = canonical_digest(input_payload)
    spec = DurableCallSpec.create(
        run_attempt_id=run_id,
        intent_revision_id=intent_revision_id,
        plan_revision_id=None,
        task_id=None,
        stage_name=stage_name,
        call_kind=call_kind,
        operation_name=f"test_{stage_name}",
        input_ref="provider-call-input:sha256:" + input_digest,
        input_payload=input_payload,
    )
    claim = store.attempt_journal.claim(spec)
    if claim.replayed:
        return claim.attempt.attempt_ref
    completion = store.attempt_journal.succeed(
        claim.attempt,
        {
            "output": {"accepted": True},
            "audit": {"task": f"test_{stage_name}"},
        },
    )
    assert completion.acceptance is not None
    return completion.acceptance.accepted_attempt_ref


def _registry() -> RuntimeContractRegistry:
    return RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)


def _intent_revision(run_id: str) -> IntentRevision:
    metric_text = "付费金额"
    date_text = "2026年6月1日"
    return IntentRevision.create(
        run_attempt_id=run_id,
        original_user_text=QUESTION,
        goal_bindings=({"goal_id": "explain_change", "role": "primary"},),
        target_metric_refs=("paid_amount",),
        scope={"scope_type": "full_sample", "filters": []},
        time_spec={
            "kind": "date",
            "target": "2026-06-01",
        },
        comparison_spec={
            "kind": "fixed_window",
            "baseline_class": "prior_period",
            "baseline_start": "2026-05-31",
            "baseline_end": "2026-05-31",
            "aggregation": "sum_of_complete_days",
        },
        direction_premise="user_hypothesis_positive",
        requested_factor_refs=(),
        requested_analysis_axes=(
            "change_validation",
            "formula_tree",
            "dimension_localization",
            "time_context",
            "data_quality",
        ),
        desired_decisions=(
            {"decision_kind": "explain_change", "target_ref": "paid_amount"},
        ),
        ambiguity_slots=(),
        source_spans=(
            {
                "field": "target_metric_refs[0]",
                "start": QUESTION.index(metric_text),
                "end": QUESTION.index(metric_text) + len(metric_text),
                "text": metric_text,
            },
            {
                "field": "time_spec.target",
                "start": QUESTION.index(date_text),
                "end": QUESTION.index(date_text) + len(date_text),
                "text": date_text,
            },
        ),
        schema_version="intent-revision.v2",
        prompt_version="single-authority.phase02.postgres-test.v1",
        model_version="deterministic-contract-record",
        known_goal_ids={"explain_change"},
        known_metric_ids={"paid_amount"},
        known_analysis_axis_ids={
            "change_validation",
            "formula_tree",
            "dimension_localization",
            "time_context",
            "data_quality",
        },
        known_scope_types={"full_sample"},
    )


def _transition(
    *,
    node_name: str,
    run_id: str,
    intent_revision_id: str,
    input_payload: dict,
    output_payload: dict,
    next_transition: str,
    parent_transition_id: str | None = None,
    provider_ref: str = "deterministic_contract_test",
    model_ref: str = "typed_record",
) -> DurableTransition:
    return DurableTransition.create(
        node_name=node_name,
        parent_transition_id=parent_transition_id,
        run_attempt_id=run_id,
        intent_revision_id=intent_revision_id,
        decision_ledger_position=0,
        input_digest=canonical_digest(input_payload),
        output_digest=canonical_digest(output_payload),
        execution_attempt=1,
        provider_ref=provider_ref,
        model_ref=model_ref,
        status="succeeded",
        acceptance_state="accepted",
        next_transition=next_transition,
    )


def _authority_context(
    run_id: str,
    *,
    release_suffix: str = "v1",
) -> AuthorityContext:
    release_ref = f"release:paid-order:{release_suffix}"
    snapshot_ref = f"snapshot:paid-order:{release_suffix}"
    return AuthorityContext.create(
        run_attempt_id=run_id,
        actual_as_of="2026-06-02T00:00:00Z",
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
                "dataset_id": "payment_final_outcome",
                "availability": "unavailable",
                "release_ref": None,
                "snapshot_refs": (),
                "limitation_ref": "limitation:payment-attempt-contract",
            },
        ),
        contract_versions={
            "analysis_bindings": "clickhouse-analysis-bindings.v2",
            "semantic_contract": "semantic-layer.v2",
        },
    )


def _planner_proposal(
    run_id: str,
    intent_revision_id: str,
    authority_context: AuthorityContext,
    *,
    variant: str = "initial",
) -> PlannerProposal:
    issue_tree = (
        {
            "issue_id": "paid_amount_change",
            "parent_issue_id": None,
            "question": "付费金额相对基准变化多少，哪些因素共同解释变化？",
            "target_claim_kind": "comparative_change",
        },
    )
    auxiliary_axes = (
        {
            "proposal_item_id": f"axis:calendar_context:{variant}",
            "axis_id": "time_context",
            "rationale": "检验目标日期是否处于周期性波动中。",
            "supports_claim_kinds": ("comparative_change",),
        },
    )
    hypotheses = (
        {
            "proposal_item_id": f"hypothesis:calendar_pattern:{variant}",
            "statement": "周期性波动可能影响目标日期表现。",
            "target_claim_kind": "comparative_change",
            "requested_axis_ids": ("time_context",),
            "assumption_refs": (),
        },
    )
    priority_proposals = (
        {
            "proposal_item_id": f"priority:formula_tree:{variant}",
            "target_ref": "formula_tree",
            "rationale": "先核对公式因素贡献。",
        },
        {
            "proposal_item_id": f"priority:dimension_localization:{variant}",
            "target_ref": "dimension_localization",
            "rationale": "再定位业务分群贡献。",
        },
        {
            "proposal_item_id": f"priority:time_context:{variant}",
            "target_ref": "time_context",
            "rationale": "最后补充时间背景。",
        },
    )
    raw_response = _planner_raw_response(
        issue_tree=issue_tree,
        auxiliary_axes=auxiliary_axes,
        hypotheses=hypotheses,
        priority_proposals=priority_proposals,
        assumption_proposals=(),
    )
    return PlannerProposal.create(
        run_attempt_id=run_id,
        intent_revision_id=intent_revision_id,
        decision_refs=(),
        authority_context_ref=authority_context.authority_context_ref,
        issue_tree=issue_tree,
        auxiliary_axes=auxiliary_axes,
        hypotheses=hypotheses,
        priority_proposals=priority_proposals,
        assumption_proposals=(),
        raw_provider_response_ref=(
            "restricted-provider-response:sha256:"
            + sha256(raw_response.encode("utf-8")).hexdigest()
        ),
        schema_version="planner-proposal.v1",
        prompt_version="single-authority-plan-proposal.v1",
        model_version="deepseek-chat",
    )


def _planner_raw_response(
    *,
    issue_tree,
    auxiliary_axes,
    hypotheses,
    priority_proposals,
    assumption_proposals,
) -> str:
    return json.dumps(
        {
            "issue_tree": issue_tree,
            "auxiliary_axes": auxiliary_axes,
            "hypotheses": hypotheses,
            "priority_proposals": priority_proposals,
            "assumption_proposals": assumption_proposals,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _planner_audit(proposal: PlannerProposal) -> dict:
    structured_output = {
        "issue_tree": proposal.to_dict()["issue_tree"],
        "auxiliary_axes": proposal.to_dict()["auxiliary_axes"],
        "hypotheses": proposal.to_dict()["hypotheses"],
        "priority_proposals": proposal.to_dict()["priority_proposals"],
        "assumption_proposals": proposal.to_dict()["assumption_proposals"],
    }
    return {
        "task": "single_authority_plan_proposal",
        "provider": "phase02-postgres-test-provider",
        "model": proposal.model_version,
        "prompt_version": proposal.prompt_version,
        "response_id": "phase02-postgres-test-response",
        "raw_response_content": json.dumps(
            structured_output,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "structured_output": structured_output,
        "usage": {},
    }


def _proposal_admission(
    *,
    proposal: PlannerProposal,
    intent_revision_id: str,
    authority_context: AuthorityContext,
) -> ProposalAdmissionRecord:
    auxiliary_item_id = str(proposal.auxiliary_axes[0]["proposal_item_id"])
    hypothesis_item_id = str(proposal.hypotheses[0]["proposal_item_id"])
    priority_entries = tuple(
        {
            "proposal_item_ref": str(item["proposal_item_id"]),
            "item_kind": "priority",
            "status": "deferred",
            "reason_code": "not_selected_by_contract_fixture",
            "contract_refs": ["single-authority-plan-compiler.v1#priority_budget"],
            "normalized_execution_ref": None,
        }
        for item in proposal.priority_proposals
    )
    return ProposalAdmissionRecord.create(
        planner_proposal_ref=proposal.planner_proposal_id,
        intent_revision_id=intent_revision_id,
        decision_refs=(),
        authority_context_ref=authority_context.authority_context_ref,
        admission_entries=(
            {
                "proposal_item_ref": auxiliary_item_id,
                "item_kind": "analysis_axis",
                "status": "admitted",
                "reason_code": "supported_by_capability_contract",
                "contract_refs": [
                    "clickhouse-analysis-bindings.v2#analysis_axis_catalog.time_context"
                ],
                "normalized_execution_ref": "time_context",
            },
            {
                "proposal_item_ref": hypothesis_item_id,
                "item_kind": "hypothesis",
                "status": "admitted",
                "reason_code": "bounded_as_assumption",
                "contract_refs": ["intent-revision.v2#assumption_boundary"],
                "normalized_execution_ref": f"hypothesis:{hypothesis_item_id}",
            },
            *priority_entries,
        ),
        compiler_version="single-authority-plan-compiler.v1",
        contract_versions=dict(authority_context.contract_versions),
    )


def _plan_revision(
    *,
    run_id: str,
    intent_revision_id: str,
    authority_context: AuthorityContext,
    proposal: PlannerProposal,
    proposal_admission: ProposalAdmissionRecord,
    supersedes_plan_revision_id: str | None = None,
    budget_policy_ref: str = "ordinary",
) -> PlanRevision:
    registry = _registry()
    comparison = ClaimObligation.create(
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
    data_quality = ClaimObligation.create(
        claim_kind="contract_coverage_and_trust_boundary",
        role="user_required",
        subject={
            "target_metric_ref": "paid_amount",
            "scope": {"scope_type": "full_sample", "filters": []},
            "outcome_refs": ("outcome:data_contract_boundary",),
            "goal_refs": ("explain_change",),
        },
        evidence_requirement=EvidenceRequirement.create(
            operator="any_of",
            evidence_kinds=("insufficient",),
        ),
        success_policy={
            "policy": "verified_or_explicit_boundary",
            "minimum_claim_strength": "trust_boundary",
        },
    )

    def axis(
        axis_id: str,
        role: str,
        obligation_ids: tuple[str, ...],
        *,
        proposal_refs: tuple[str, ...] = (),
    ) -> AnalysisAxis:
        contract = registry.analysis_axis(axis_id)
        return AnalysisAxis.create(
            axis_id=axis_id,
            role=role,
            axis_kind=contract["axis_kind"],
            target_metric_refs=("paid_amount",),
            metric_refs=tuple(contract["metric_refs"]),
            dimension_refs=tuple(contract["dimension_refs"]),
            context_source_refs=tuple(contract["context_source_refs"]),
            capability_refs=tuple(contract["capability_refs"]),
            reconciliation_group=contract["reconciliation_group"],
            selection_policy=contract["selection_policy"],
            source_refs=tuple(contract["source_refs"]),
            goal_refs=("explain_change",),
            supports_obligation_ids=obligation_ids,
            proposal_refs=proposal_refs,
        )

    input_states = (
        {
            "input_ref": "dataset:paid_order_success",
            "availability": "claim_ready",
            "limitation_ref": None,
        },
        {
            "input_ref": "dataset:payment_final_outcome",
            "availability": "unavailable",
            "limitation_ref": "limitation:payment-attempt-contract",
        },
    )
    temporal_authority = resolved_test_temporal_authority(
        time_spec={"kind": "date", "target": "2026-06-01"},
        comparison_spec={
            "kind": "fixed_window",
            "baseline_class": "prior_period",
            "baseline_start": "2026-05-31",
            "baseline_end": "2026-05-31",
            "aggregation": "sum_of_complete_days",
        },
        require_physical_baseline=True,
    )
    return PlanRevision.create(
        run_attempt_id=run_id,
        supersedes_plan_revision_id=supersedes_plan_revision_id,
        intent_revision_id=intent_revision_id,
        decision_refs=(),
        authority_context_ref=authority_context.authority_context_ref,
        temporal_authority=temporal_authority,
        resolved_window_refs=temporal_authority.resolved_window_refs,
        context_window_specs=(),
        claim_obligations=(comparison, data_quality),
        analysis_axes=(
            axis("change_validation", "required", (comparison.obligation_id,)),
            axis("data_quality", "disclosure", (data_quality.obligation_id,)),
            axis(
                "time_context",
                "auxiliary",
                (comparison.obligation_id,),
                proposal_refs=(
                    str(proposal.auxiliary_axes[0]["proposal_item_id"]),
                    str(proposal.hypotheses[0]["proposal_item_id"]),
                ),
            ),
        ),
        capability_task_specs=(
            {
                "task_key": "data_quality",
                "capability_id": "data_quality_profile",
                "normalized_input_refs": ["dataset:paid_order_success"],
                "dependency_task_keys": (),
                "obligation_edges": (
                    {
                        "obligation_id": data_quality.obligation_id,
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
                        "missing_required_input": "report_contract_gap",
                        "incomplete_input": "report_limitation",
                    },
                    "integrity_failure": "fail_closed",
                    "input_states": input_states,
                },
            },
            {
                "task_key": "primary_comparison",
                "capability_id": "compare_periods",
                "normalized_input_refs": [
                    "metric:paid_amount",
                    "window:2026-06-01",
                    "window:2026-05-31",
                ],
                "dependency_task_keys": ["data_quality"],
                "obligation_edges": (
                    {
                        "obligation_id": comparison.obligation_id,
                        "required": True,
                    },
                ),
                "execution_rank": 2,
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
                        "incomplete_input": "degrade_claim",
                    },
                    "integrity_failure": "fail_closed",
                    "input_states": input_states,
                },
            },
            {
                "task_key": "time_context",
                "capability_id": "metric_timeseries",
                "normalized_input_refs": ["metric:paid_amount", "window:2026-06-01"],
                "dependency_task_keys": ["primary_comparison"],
                "obligation_edges": (
                    {
                        "obligation_id": comparison.obligation_id,
                        "required": False,
                    },
                ),
                "execution_rank": 3,
                "declared_budget_units": 1,
                "governor_inputs": {
                    "expected_information_gain": "hypothesis_testing",
                    "materiality": "analyst_auxiliary",
                    "actionability": "explanation_supporting",
                    "statistical_risk": "contract_bounded",
                },
                "execution_policy": {
                    "degradation_policy": {
                        "missing_required_input": "block_claim",
                        "incomplete_input": "degrade_claim",
                    },
                    "integrity_failure": "fail_closed",
                    "input_states": input_states,
                },
            },
        ),
        assumption_refs=(),
        budget_policy_ref=budget_policy_ref,
        contract_versions=dict(authority_context.contract_versions),
        planner_proposal_ref=proposal.planner_proposal_id,
        proposal_admission_ref=proposal_admission.proposal_admission_id,
    )


def _plan_transition(
    *,
    intent_revision: IntentRevision,
    authority_context: AuthorityContext,
    proposal: PlannerProposal,
    proposal_admission: ProposalAdmissionRecord,
    plan_revision: PlanRevision,
    parent_transition_id: str,
) -> tuple[DurableTransition, dict, dict]:
    planner_audit = _planner_audit(proposal)
    input_payload = {
        "intent_revision_id": intent_revision.intent_revision_id,
        "decision_refs": list(plan_revision.decision_refs),
        "authority_context_ref": authority_context.authority_context_ref,
        "planner_proposal_ref": proposal.planner_proposal_id,
        "proposal_admission_ref": proposal_admission.proposal_admission_id,
        "supersedes_plan_revision_id": plan_revision.supersedes_plan_revision_id,
        "plan_patch_ref": None,
    }
    output_payload = {
        "authority_context": authority_context.to_dict(),
        "planner_proposal": proposal.to_dict(),
        "proposal_admission_record": proposal_admission.to_dict(),
        "plan_revision": plan_revision.to_dict(),
        "planner_llm_audit": planner_audit,
    }
    transition = _transition(
        node_name="compile_authoritative_plan",
        run_id=intent_revision.run_attempt_id,
        intent_revision_id=intent_revision.intent_revision_id,
        input_payload=input_payload,
        output_payload=output_payload,
        parent_transition_id=parent_transition_id,
        next_transition="phase02_plan_bound",
        provider_ref=str(planner_audit["provider"]),
        model_ref=str(planner_audit["model"]),
    )
    return transition, input_payload, output_payload


class Phase02PostgresSchemaContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = (ROOT / "tools/runtime/conversation-runtime.sql").read_text(
            encoding="utf-8"
        )

    def test_phase02_authority_tables_are_append_only(self):
        self.assertIn("reject_append_only_authority_mutation", self.schema)
        for table in (
            "authority_contexts",
            "planner_proposals",
            "proposal_admission_records",
            "plan_revisions",
            "plan_revision_supersessions",
        ):
            self.assertIn(f"waje_runtime.{table}", self.schema)
            self.assertIn(f"{table}_append_only", self.schema)

    def test_plan_checkpoint_requires_restricted_planner_provider_audit(self):
        run_id = "phase02-provider-audit-contract"
        intent = _intent_revision(run_id)
        context = _authority_context(run_id)
        proposal = _planner_proposal(
            run_id,
            intent.intent_revision_id,
            context,
        )
        admission = _proposal_admission(
            proposal=proposal,
            intent_revision_id=intent.intent_revision_id,
            authority_context=context,
        )
        plan = _plan_revision(
            run_id=run_id,
            intent_revision_id=intent.intent_revision_id,
            authority_context=context,
            proposal=proposal,
            proposal_admission=admission,
        )
        transition, input_payload, output_payload = _plan_transition(
            intent_revision=intent,
            authority_context=context,
            proposal=proposal,
            proposal_admission=admission,
            plan_revision=plan,
            parent_transition_id="transition-intent-provider-audit",
        )
        output_without_audit = dict(output_payload)
        output_without_audit.pop("planner_llm_audit")
        connection = Mock()
        store = PostgresConversationStore(connection)

        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "^plan_transition_output_payload_mismatch$",
        ):
            store.save_plan_revision_transition(
                authority_context=context,
                planner_proposal=proposal,
                proposal_admission=admission,
                plan_revision=plan,
                transition=transition,
                input_payload=input_payload,
                output_payload=output_without_audit,
                accepted_attempt_refs=(SCHEMA_PROVIDER_ATTEMPT_REF,),
            )

        output_with_wrong_model = {
            **output_payload,
            "planner_llm_audit": {
                **output_payload["planner_llm_audit"],
                "model": "different-routed-model",
            },
        }
        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "^planner_provider_audit_invalid$",
        ):
            store.save_plan_revision_transition(
                authority_context=context,
                planner_proposal=proposal,
                proposal_admission=admission,
                plan_revision=plan,
                transition=transition,
                input_payload=input_payload,
                output_payload=output_with_wrong_model,
                accepted_attempt_refs=(SCHEMA_PROVIDER_ATTEMPT_REF,),
            )
        self.assertEqual(connection.method_calls, [])

    def test_plan_checkpoint_rebuilds_records_before_any_database_operation(self):
        run_id = "phase02-record-integrity-contract"
        intent = _intent_revision(run_id)
        context = _authority_context(run_id)
        proposal = _planner_proposal(run_id, intent.intent_revision_id, context)
        admission = _proposal_admission(
            proposal=proposal,
            intent_revision_id=intent.intent_revision_id,
            authority_context=context,
        )
        plan = _plan_revision(
            run_id=run_id,
            intent_revision_id=intent.intent_revision_id,
            authority_context=context,
            proposal=proposal,
            proposal_admission=admission,
        )
        transition, input_payload, output_payload = _plan_transition(
            intent_revision=intent,
            authority_context=context,
            proposal=proposal,
            proposal_admission=admission,
            plan_revision=plan,
            parent_transition_id="transition-intent-record-integrity",
        )
        forged_plan = replace(plan, content_digest="0" * 64)
        connection = Mock()
        store = PostgresConversationStore(connection)

        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "^plan_transition_record_integrity_invalid$",
        ):
            store.save_plan_revision_transition(
                authority_context=context,
                planner_proposal=proposal,
                proposal_admission=admission,
                plan_revision=forged_plan,
                transition=transition,
                input_payload=input_payload,
                output_payload=output_payload,
                accepted_attempt_refs=(SCHEMA_PROVIDER_ATTEMPT_REF,),
            )

        self.assertEqual(connection.method_calls, [])

    def test_plan_checkpoint_validates_admission_closure_before_database_write(self):
        run_id = "phase02-admission-closure-contract"
        intent = _intent_revision(run_id)
        context = _authority_context(run_id)
        proposal = _planner_proposal(run_id, intent.intent_revision_id, context)
        valid_admission = _proposal_admission(
            proposal=proposal,
            intent_revision_id=intent.intent_revision_id,
            authority_context=context,
        )
        incomplete_admission = ProposalAdmissionRecord.create(
            planner_proposal_ref=proposal.planner_proposal_id,
            intent_revision_id=intent.intent_revision_id,
            decision_refs=(),
            authority_context_ref=context.authority_context_ref,
            admission_entries=valid_admission.admission_entries[:-1],
            compiler_version=valid_admission.compiler_version,
            contract_versions=dict(valid_admission.contract_versions),
        )
        plan = _plan_revision(
            run_id=run_id,
            intent_revision_id=intent.intent_revision_id,
            authority_context=context,
            proposal=proposal,
            proposal_admission=incomplete_admission,
        )
        transition, input_payload, output_payload = _plan_transition(
            intent_revision=intent,
            authority_context=context,
            proposal=proposal,
            proposal_admission=incomplete_admission,
            plan_revision=plan,
            parent_transition_id="transition-intent-admission-closure",
        )
        connection = Mock()
        store = PostgresConversationStore(connection)

        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "^single_authority_plan_proposal_admission_closure_mismatch$",
        ):
            store.save_plan_revision_transition(
                authority_context=context,
                planner_proposal=proposal,
                proposal_admission=incomplete_admission,
                plan_revision=plan,
                transition=transition,
                input_payload=input_payload,
                output_payload=output_payload,
                accepted_attempt_refs=(SCHEMA_PROVIDER_ATTEMPT_REF,),
            )

        self.assertEqual(connection.method_calls, [])


class SingleAuthorityPhase02PostgresIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (os.getenv("WAJE_RUNTIME_DATABASE_URL") or os.getenv("DATABASE_URL")):
            raise unittest.SkipTest("runtime PostgreSQL is not configured")
        cls.store = PostgresConversationStore.from_env()
        cls.store.apply_schema()

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "store"):
            cls.store.connection.close()

    def setUp(self):
        suffix = uuid4().hex
        self.thread_id = f"phase02-contract-thread-{suffix}"
        self.store.create_thread(self.thread_id, owner_id="phase02-contract-user")

    def _accepted_intent(
        self,
        run_id: str,
    ) -> tuple[IntentRevision, DurableTransition]:
        self.store.upsert_run(run_id, thread_id=self.thread_id, status="running")
        revision = _intent_revision(run_id)
        input_payload = {"question": QUESTION, "contract_test": True}
        output_payload = {"intent_revision": revision.to_dict()}
        transition = _transition(
            node_name="bind_intent",
            run_id=run_id,
            intent_revision_id=revision.intent_revision_id,
            input_payload=input_payload,
            output_payload=output_payload,
            next_transition="compile_authoritative_plan",
        )
        self.store.save_intent_revision_transition(
            intent_revision=revision,
            transition=transition,
            input_payload=input_payload,
            output_payload=output_payload,
            accepted_attempt_refs=(
                _accepted_provider_attempt_ref(
                    self.store,
                    run_id=run_id,
                    intent_revision_id=None,
                    call_kind="intent_provider",
                    stage_name="bind_intent",
                ),
            ),
        )
        return revision, transition

    def _bundle(
        self,
        *,
        intent_revision: IntentRevision,
        parent_transition_id: str,
        authority_context: AuthorityContext | None = None,
        variant: str = "initial",
        supersedes_plan_revision_id: str | None = None,
        budget_policy_ref: str = "ordinary",
    ) -> tuple[
        AuthorityContext,
        PlannerProposal,
        ProposalAdmissionRecord,
        PlanRevision,
        DurableTransition,
        dict,
        dict,
    ]:
        context = authority_context or _authority_context(
            intent_revision.run_attempt_id
        )
        proposal = _planner_proposal(
            intent_revision.run_attempt_id,
            intent_revision.intent_revision_id,
            context,
            variant=variant,
        )
        proposal_admission = _proposal_admission(
            proposal=proposal,
            intent_revision_id=intent_revision.intent_revision_id,
            authority_context=context,
        )
        plan = _plan_revision(
            run_id=intent_revision.run_attempt_id,
            intent_revision_id=intent_revision.intent_revision_id,
            authority_context=context,
            proposal=proposal,
            proposal_admission=proposal_admission,
            supersedes_plan_revision_id=supersedes_plan_revision_id,
            budget_policy_ref=budget_policy_ref,
        )
        transition, input_payload, output_payload = _plan_transition(
            intent_revision=intent_revision,
            authority_context=context,
            proposal=proposal,
            proposal_admission=proposal_admission,
            plan_revision=plan,
            parent_transition_id=parent_transition_id,
        )
        return (
            context,
            proposal,
            proposal_admission,
            plan,
            transition,
            input_payload,
            output_payload,
        )

    def _save_bundle(self, bundle: tuple) -> dict:
        (
            context,
            proposal,
            proposal_admission,
            plan,
            transition,
            input_payload,
            output_payload,
        ) = bundle
        return self.store.save_plan_revision_transition(
            authority_context=context,
            planner_proposal=proposal,
            proposal_admission=proposal_admission,
            plan_revision=plan,
            transition=transition,
            input_payload=input_payload,
            output_payload=output_payload,
            accepted_attempt_refs=(
                _accepted_provider_attempt_ref(
                    self.store,
                    run_id=plan.run_attempt_id,
                    intent_revision_id=plan.intent_revision_id,
                    call_kind="planner_provider",
                    stage_name="compile_authoritative_plan",
                ),
            ),
        )

    def test_plan_acceptance_is_atomic_and_exact_replay_is_idempotent(self):
        run_id = f"phase02-contract-run-{uuid4().hex}"
        intent, intent_transition = self._accepted_intent(run_id)
        bundle = self._bundle(
            intent_revision=intent,
            parent_transition_id=intent_transition.transition_id,
        )

        with patch.object(
            self.store,
            "_save_transition_attempt_locked",
            side_effect=RuntimeError("injected_plan_transition_write_failure"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "injected_plan_transition_write_failure",
            ):
                self._save_bundle(bundle)

        self.assertIsNone(self.store.load_authority_context(run_id))
        self.assertIsNone(self.store.resolve_active_plan_revision(run_id))
        self.assertNotIn(
            "plan_result_refs",
            self.store.get_run_request(run_id),
        )
        rolled_back_counts = self.store._fetchone(
            """
            SELECT
              (SELECT count(*) FROM waje_runtime.authority_contexts
               WHERE run_attempt_id = %(run_id)s),
              (SELECT count(*) FROM waje_runtime.planner_proposals
               WHERE run_attempt_id = %(run_id)s),
              (SELECT count(*) FROM waje_runtime.proposal_admission_records a
               JOIN waje_runtime.planner_proposals p
                 ON p.planner_proposal_id = a.planner_proposal_ref
               WHERE p.run_attempt_id = %(run_id)s),
              (SELECT count(*) FROM waje_runtime.plan_revisions
               WHERE run_attempt_id = %(run_id)s)
            """,
            {"run_id": run_id},
        )
        self.assertEqual(tuple(rolled_back_counts), (0, 0, 0, 0))

        inserted = self._save_bundle(bundle)
        replayed = self._save_bundle(bundle)

        self.assertFalse(inserted["replayed"])
        self.assertTrue(replayed["replayed"])
        context, _, _, plan, _, _, _ = bundle
        self.assertEqual(
            self.store.load_authority_context(run_id).to_dict(),
            context.to_dict(),
        )
        self.assertEqual(
            self.store.resolve_active_plan_revision(run_id).to_dict(),
            plan.to_dict(),
        )
        self.assertEqual(
            self.store.get_run_request(run_id)["plan_result_refs"],
            {
                "schema_version": "single-authority-phase02.v2",
                "plan_patch_ref": None,
                "intent_revision_id": plan.intent_revision_id,
                "authority_context_ref": context.authority_context_ref,
                "planner_proposal_id": bundle[1].planner_proposal_id,
                "proposal_admission_id": bundle[2].proposal_admission_id,
                "plan_revision_id": plan.plan_revision_id,
                "accepted_transition_id": bundle[4].transition_id,
            },
        )
        persisted_counts = self.store._fetchone(
            """
            SELECT
              (SELECT count(*) FROM waje_runtime.authority_contexts
               WHERE run_attempt_id = %(run_id)s),
              (SELECT count(*) FROM waje_runtime.planner_proposals
               WHERE run_attempt_id = %(run_id)s),
              (SELECT count(*) FROM waje_runtime.proposal_admission_records
               WHERE planner_proposal_ref = %(proposal_id)s),
              (SELECT count(*) FROM waje_runtime.plan_revisions
               WHERE run_attempt_id = %(run_id)s),
              (SELECT count(*) FROM waje_runtime.workflow_transition_attempts
               WHERE run_attempt_id = %(run_id)s
                 AND node_name = 'compile_authoritative_plan'
                 AND acceptance_state = 'accepted')
            """,
            {
                "run_id": run_id,
                "proposal_id": bundle[1].planner_proposal_id,
            },
        )
        self.assertEqual(tuple(persisted_counts), (1, 1, 1, 1, 1))

    def test_plan_exact_replay_rejects_conflicting_stage_refs(self):
        run_id = f"phase02-contract-run-{uuid4().hex}"
        intent, intent_transition = self._accepted_intent(run_id)
        bundle = self._bundle(
            intent_revision=intent,
            parent_transition_id=intent_transition.transition_id,
        )
        self._save_bundle(bundle)
        request = self.store.get_run_request(run_id)
        request.pop("thread_id")
        request.pop("topic_id")
        request["plan_result_refs"] = {
            **request["plan_result_refs"],
            "plan_revision_id": "plan-revision:conflict",
        }
        self.store._execute(
            """
            UPDATE waje_runtime.analysis_runs
            SET request = %(request)s::jsonb
            WHERE run_id = %(run_id)s
            """,
            {"run_id": run_id, "request": json.dumps(request)},
        )

        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "^plan_result_refs_replay_conflict$",
        ):
            self._save_bundle(bundle)

    def test_parallel_or_unpatched_plan_cannot_replace_the_active_plan(self):
        run_id = f"phase02-contract-run-{uuid4().hex}"
        intent, intent_transition = self._accepted_intent(run_id)
        initial_bundle = self._bundle(
            intent_revision=intent,
            parent_transition_id=intent_transition.transition_id,
        )
        self._save_bundle(initial_bundle)
        context, _, _, initial_plan, initial_transition, _, _ = initial_bundle

        parallel_bundle = self._bundle(
            intent_revision=intent,
            parent_transition_id=initial_transition.transition_id,
            authority_context=context,
            variant="parallel",
            budget_policy_ref="deep_attribution",
        )
        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "active_plan_revision_conflict",
        ):
            self._save_bundle(parallel_bundle)

        unpatched_superseding_bundle = self._bundle(
            intent_revision=intent,
            parent_transition_id=initial_transition.transition_id,
            authority_context=context,
            variant="patch",
            supersedes_plan_revision_id=initial_plan.plan_revision_id,
            budget_policy_ref="deep_attribution",
        )
        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "^plan_transition_authority_mismatch$",
        ):
            self._save_bundle(unpatched_superseding_bundle)

        counts = self.store._fetchone(
            """
            SELECT
              (SELECT count(*) FROM waje_runtime.authority_contexts
               WHERE run_attempt_id = %(run_id)s),
              (SELECT count(*) FROM waje_runtime.plan_revisions
               WHERE run_attempt_id = %(run_id)s),
              (SELECT count(*) FROM waje_runtime.plan_revision_supersessions s
               JOIN waje_runtime.plan_revisions p
                 ON p.plan_revision_id = s.successor_plan_revision_id
               WHERE p.run_attempt_id = %(run_id)s),
              (SELECT count(*)
               FROM waje_runtime.plan_revisions p
               LEFT JOIN waje_runtime.plan_revision_supersessions s
                 ON s.superseded_plan_revision_id = p.plan_revision_id
               WHERE p.run_attempt_id = %(run_id)s
                 AND s.superseded_plan_revision_id IS NULL)
            """,
            {"run_id": run_id},
        )
        self.assertEqual(tuple(counts), (1, 1, 0, 1))

        self.assertEqual(
            self.store.load_authority_context(run_id).authority_context_ref,
            context.authority_context_ref,
        )
        self.assertEqual(
            self.store.resolve_active_plan_revision(run_id).plan_revision_id,
            initial_plan.plan_revision_id,
        )

    def test_concurrent_duplicate_compile_accepts_one_plan_digest(self):
        run_id = f"phase02-contract-run-{uuid4().hex}"
        intent, intent_transition = self._accepted_intent(run_id)
        bundle = self._bundle(
            intent_revision=intent,
            parent_transition_id=intent_transition.transition_id,
        )

        def persist_once() -> dict:
            worker = PostgresConversationStore.from_env()
            try:
                return worker.save_plan_revision_transition(
                    authority_context=bundle[0],
                    planner_proposal=bundle[1],
                    proposal_admission=bundle[2],
                    plan_revision=bundle[3],
                    transition=bundle[4],
                    input_payload=bundle[5],
                    output_payload=bundle[6],
                    accepted_attempt_refs=(
                        _accepted_provider_attempt_ref(
                            worker,
                            run_id=bundle[3].run_attempt_id,
                            intent_revision_id=bundle[3].intent_revision_id,
                            call_kind="planner_provider",
                            stage_name="compile_authoritative_plan",
                        ),
                    ),
                )
            finally:
                worker.connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: persist_once(), range(2)))

        self.assertEqual(
            sorted(result["replayed"] for result in results),
            [False, True],
        )
        plan = bundle[3]
        counts = self.store._fetchone(
            """
            SELECT
              (SELECT count(*) FROM waje_runtime.authority_contexts
               WHERE run_attempt_id = %(run_id)s),
              (SELECT count(*) FROM waje_runtime.planner_proposals
               WHERE run_attempt_id = %(run_id)s),
              (SELECT count(*) FROM waje_runtime.proposal_admission_records a
               JOIN waje_runtime.planner_proposals p
                 ON p.planner_proposal_id = a.planner_proposal_ref
               WHERE p.run_attempt_id = %(run_id)s),
              (SELECT count(*) FROM waje_runtime.plan_revisions
               WHERE run_attempt_id = %(run_id)s),
              (SELECT count(DISTINCT content_digest)
               FROM waje_runtime.plan_revisions
               WHERE run_attempt_id = %(run_id)s),
              (SELECT count(*) FROM waje_runtime.workflow_transition_attempts
               WHERE run_attempt_id = %(run_id)s
                 AND node_name = 'compile_authoritative_plan'
                 AND acceptance_state = 'accepted')
            """,
            {"run_id": run_id},
        )
        self.assertEqual(tuple(counts), (1, 1, 1, 1, 1, 1))
        self.assertEqual(
            self.store.resolve_active_plan_revision(run_id).content_digest,
            plan.content_digest,
        )

    def test_plan_transition_parent_must_match_current_accepted_head(self):
        run_id = f"phase02-contract-run-{uuid4().hex}"
        intent, _ = self._accepted_intent(run_id)
        bundle = self._bundle(
            intent_revision=intent,
            parent_transition_id="transition-stale-or-arbitrary",
        )

        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "^plan_transition_parent_not_current_head$",
        ):
            self._save_bundle(bundle)

        counts = self.store._fetchone(
            """
            SELECT
              (SELECT count(*) FROM waje_runtime.authority_contexts
               WHERE run_attempt_id = %(run_id)s),
              (SELECT count(*) FROM waje_runtime.planner_proposals
               WHERE run_attempt_id = %(run_id)s),
              (SELECT count(*) FROM waje_runtime.proposal_admission_records a
               JOIN waje_runtime.planner_proposals p
                 ON p.planner_proposal_id = a.planner_proposal_ref
               WHERE p.run_attempt_id = %(run_id)s),
              (SELECT count(*) FROM waje_runtime.plan_revisions
               WHERE run_attempt_id = %(run_id)s)
            """,
            {"run_id": run_id},
        )
        self.assertEqual(tuple(counts), (0, 0, 0, 0))


if __name__ == "__main__":
    unittest.main()
