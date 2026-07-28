from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
import json
from typing import Any, Callable, Mapping, Sequence

import pytest

import bi_agent.runtime.semantic_authority_workflow as semantic_workflow
from bi_agent.runtime.authoritative_execution_result import AuthoritativeExecutionResult
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
from bi_agent.runtime.claim_settlement import ClaimSettlement, prepare_claim_settlement
from bi_agent.runtime.evidence_authority import canonical_digest
from bi_agent.runtime.llm_client import LLMResult
from bi_agent.runtime.plan_authority import (
    AnalysisAxis,
    ClaimObligation,
    EvidenceRequirement,
    PlanRevision,
)
from bi_agent.runtime.semantic_authority_workflow import (
    RestrictedExecutionProjection,
    SemanticAuthorityResult,
    SemanticAuthorityWorkflowError,
    _decode_semantic_projection,
    _encode_semantic_projection,
    run_semantic_authority_workflow as _run_semantic_authority_workflow,
)
from bi_agent.runtime.claim_coverage import ClaimCoverageCheckpoint
from bi_agent.runtime.single_authority import DurableTransition
from tests.support.claim_coverage import resolved_test_claim_coverage_checkpoint
from tests.support.temporal_authority import resolved_test_temporal_authority


@dataclass(frozen=True)
class _ExecutionSpec:
    claim_kind: str
    evidence_kinds: str | tuple[str, ...]
    obligation_role: str = "user_required"
    investigation_mode: str | None = None
    required_claim_strength: str | None = None
    status: str = "succeeded"
    evidence_kind: str = "observed"
    maximum_claim_strength: str = "directional"
    supported_claim_kinds: tuple[str, ...] = ("comparative_change",)
    limitation_refs: tuple[str, ...] = ("limitation:aggregate",)
    observation_value: Any = -12
    additional_evidence: tuple[Mapping[str, Any], ...] = ()


def _execution(spec: _ExecutionSpec) -> AuthoritativeExecutionResult:
    subject = (
        {
            "target_metric_ref": "paid_amount",
            "scope": {"market": "all"},
            "outcome_refs": ("outcome:explain_change",),
            "goal_refs": ("goal:explain",),
        }
        if spec.obligation_role == "user_required"
        else {
            "planner_proposal_ref": "planner-proposal:semantic-authority",
            "proposal_item_ref": "hypothesis:semantic-authority",
            "target_metric_refs": ("paid_amount",),
            "scope": {"market": "all"},
            "goal_refs": ("goal:explain",),
        }
    )
    success_policy = {
        "policy": "verified_or_explicit_boundary",
        "minimum_claim_strength": (
            spec.required_claim_strength or spec.maximum_claim_strength
        ),
    }
    if spec.investigation_mode is not None:
        success_policy.update(
            {
                "investigation_mode": spec.investigation_mode,
                "settlement_policy": "support_refute_or_explicit_boundary",
                "requested_axis_ids": ("semantic_authority",),
            }
        )
    obligation = ClaimObligation.create(
        claim_kind=spec.claim_kind,
        role=spec.obligation_role,
        subject=subject,
        evidence_requirement=EvidenceRequirement.create(
            operator="any_of",
            evidence_kinds=(
                (spec.evidence_kinds,)
                if isinstance(spec.evidence_kinds, str)
                else spec.evidence_kinds
            ),
        ),
        success_policy=success_policy,
    )
    axis = AnalysisAxis.create(
        axis_id="semantic_authority",
        role="required",
        axis_kind="authority_settlement",
        target_metric_refs=("paid_amount",),
        metric_refs=(),
        dimension_refs=(),
        context_source_refs=(),
        capability_refs=("semantic_authority_fixture",),
        reconciliation_group="paid_amount",
        selection_policy="retain_all_qualified_evidence",
        source_refs=("contract:test",),
        goal_refs=("goal:explain",),
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
    plan = PlanRevision.create(
        run_attempt_id="run-semantic-authority",
        supersedes_plan_revision_id=None,
        intent_revision_id="intent-semantic-authority",
        decision_refs=("decision:baseline",),
        authority_context_ref="authority-context:semantic-authority",
        planner_proposal_ref="planner-proposal:semantic-authority",
        proposal_admission_ref="proposal-admission:semantic-authority",
        temporal_authority=temporal_authority,
        resolved_window_refs=temporal_authority.resolved_window_refs,
        context_window_specs=(),
        claim_obligations=(obligation,),
        analysis_axes=(axis,),
        capability_task_specs=(
            {
                "task_key": "semantic_authority_fixture",
                "capability_id": "semantic_authority_fixture",
                "normalized_input_refs": (
                    "authority-context:semantic-authority",
                    "input:aggregate",
                ),
                "dependency_task_keys": (),
                "obligation_edges": (
                    {
                        "obligation_id": obligation.obligation_id,
                        "required": spec.obligation_role == "user_required",
                    },
                ),
                "execution_rank": 1,
                "declared_budget_units": 1,
                "governor_inputs": {
                    "expected_information_gain": (
                        "obligation_closing"
                        if spec.obligation_role == "user_required"
                        else "hypothesis_testing"
                    ),
                    "materiality": spec.obligation_role,
                    "actionability": (
                        "decision_supporting"
                        if spec.obligation_role == "user_required"
                        else "explanation_supporting"
                    ),
                    "statistical_risk": "contract_bounded",
                },
                "execution_policy": {
                    "degradation_policy": {"missing_required_input": "block_claim"},
                    "integrity_failure": "fail_closed",
                    "input_states": (),
                },
            },
        ),
        assumption_refs=("assumption:accepted",),
        budget_policy_ref="budget-policy:semantic-authority",
        contract_versions={"runtime": "single-authority-phase03.v1"},
    )
    task = plan.capability_tasks[0]
    attempt = CapabilityAttempt.create(plan, task)
    evidence_specs = (
        {
            "evidence_ref": "evidence:aggregate",
            "evidence_kind": spec.evidence_kind,
            "maximum_claim_strength": spec.maximum_claim_strength,
            "supported_claim_kinds": spec.supported_claim_kinds,
            "observation_value": spec.observation_value,
        },
        *spec.additional_evidence,
    )
    evidence = (
        tuple(
            CapabilityEvidence.create(
                evidence_ref=str(evidence_spec["evidence_ref"]),
                binding_record_ref="binding:aggregate",
                execution_state="available",
                evidence_kind=str(evidence_spec["evidence_kind"]),
                data_contract_state="complete",
                supported_claim_kinds=tuple(evidence_spec["supported_claim_kinds"]),
                evidence_strength="qualified",
                maximum_claim_strength=str(evidence_spec["maximum_claim_strength"]),
                observation_facts=(
                    {
                        "name": "absolute_change",
                        "value": evidence_spec["observation_value"],
                    },
                ),
                scope="scope:full-sample",
                window_refs=plan.resolved_window_refs,
                dimension_path=(),
                limitation_refs=spec.limitation_refs,
                result_refs=("result:aggregate",),
                completeness_report_refs=("completeness:aggregate",),
                hierarchy_qualified=False,
            )
            for evidence_spec in evidence_specs
        )
        if spec.status == "succeeded"
        else ()
    )
    output = CapabilityAdapterOutput.create(
        status=spec.status,
        output_payload={"aggregate_status": spec.status},
        evidence=evidence,
        affected_obligation_ids=(obligation.obligation_id,),
        limitation_refs=spec.limitation_refs,
        retryability="never",
        failure=None,
    )
    outcome = CapabilityOutcome.create(
        attempt,
        task,
        output,
        failure_ref=None,
        budget_units=1,
    )
    ledger = tuple(
        EvidenceLedgerEntry.create(plan, task, outcome, item) for item in evidence
    )
    stop = ExplorationStopRecord.create(
        plan,
        (outcome,),
        reason="plan_exhausted",
        hard_budget_limit=None,
    )
    snapshot = ExecutionSnapshot.create(plan, stop, (outcome,), ledger, ())
    transition_input, transition_output = capability_execution_transition_payloads(
        plan, snapshot, stop
    )
    transition = DurableTransition.create(
        node_name="execute_capability_dag",
        parent_transition_id="transition:phase02-plan-bound",
        run_attempt_id=plan.run_attempt_id,
        intent_revision_id=plan.intent_revision_id,
        decision_ledger_position=1,
        input_digest=canonical_digest(transition_input),
        output_digest=canonical_digest(transition_output),
        execution_attempt=1,
        provider_ref="provider:deterministic-runtime",
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
        capability_outcome_bundles=((attempt, outcome, ledger, ()),),
        durable_transition=transition,
    )


def _namespace(execution: AuthoritativeExecutionResult) -> ClaimAuthorityNamespace:
    return ClaimAuthorityNamespace.create(
        run_attempt_id=execution.run_attempt_id,
        intent_revision_id=execution.intent_revision_id,
        plan_revision_id=execution.plan_revision_id,
    )


def run_semantic_authority_workflow(
    execution_result: AuthoritativeExecutionResult,
    *,
    authority_namespace: ClaimAuthorityNamespace,
    llm_client: Any,
    claim_coverage_checkpoint: ClaimCoverageCheckpoint | None = None,
) -> SemanticAuthorityResult:
    return _run_semantic_authority_workflow(
        execution_result,
        authority_namespace=authority_namespace,
        claim_coverage_checkpoint=(
            claim_coverage_checkpoint
            or resolved_test_claim_coverage_checkpoint(execution_result)
        ),
        llm_client=llm_client,
    )


def _projection(
    execution_result: AuthoritativeExecutionResult,
) -> RestrictedExecutionProjection:
    return RestrictedExecutionProjection.create(
        execution_result,
        claim_coverage_checkpoint=resolved_test_claim_coverage_checkpoint(
            execution_result
        ),
    )


Responder = Callable[[Mapping[str, Any]], Mapping[str, Any]]


class _FakeLLM:
    def __init__(
        self,
        responders: Sequence[Responder],
        *,
        retry_audit_call: int | None = None,
    ) -> None:
        self.responders = list(responders)
        self.retry_audit_call = retry_audit_call
        self.calls: list[dict[str, Any]] = []

    def invoke_json(self, **kwargs: Any) -> LLMResult:
        call_index = len(self.calls)
        if call_index >= len(self.responders):
            raise AssertionError("unexpected_llm_call")
        call_input = json.loads(kwargs["messages"][1]["content"])
        output = dict(self.responders[call_index](call_input))
        validator = kwargs.get("output_validator")
        if validator is not None:
            validator(output)
        self.calls.append({**kwargs, "call_input": call_input, "output": output})
        raw = json.dumps(output, ensure_ascii=False, sort_keys=True)
        retry = self.retry_audit_call == call_index
        audit: dict[str, Any] = {
            "provider": "provider:test",
            "model": "model:semantic-authority",
            "prompt_version": kwargs["prompt_version"],
            "attempt_count": 2 if retry else 1,
            "response_id": f"response:{call_index}:final",
            "raw_response_content": raw,
            "messages": [dict(item) for item in kwargs["messages"]],
        }
        if retry:
            audit["attempt_failures"] = (
                {
                    "attempt": 1,
                    "response_id": f"response:{call_index}:failed",
                    "raw_response_content": '{"malformed":true}',
                },
            )
        return LLMResult(output=output, audit=audit)


class _NoCallLLM:
    def invoke_json(self, **_: Any) -> LLMResult:
        raise AssertionError("boundary_authority_must_not_call_llm")


def _accept_claims(call_input: Mapping[str, Any]) -> Mapping[str, Any]:
    return {
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


def _no_recommendations(_: Mapping[str, Any]) -> Mapping[str, Any]:
    return {"recommendation_proposals": []}


def test_internal_result_creation_does_not_rebuild_complete_provider_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = _execution(_ExecutionSpec("comparative_change", "observed"))

    def forbidden_replay(**_kwargs: Any) -> None:
        raise AssertionError(
            "typed_internal_semantic_result_must_not_rebuild_provider_inputs"
        )

    monkeypatch.setattr(
        semantic_workflow,
        "_validate_result_provider_closure",
        forbidden_replay,
    )

    result = run_semantic_authority_workflow(
        execution,
        authority_namespace=_namespace(execution),
        llm_client=_FakeLLM((_accept_claims, _no_recommendations)),
    )

    assert result.settlement.claim_graph.authority_mode == "claim_bearing"


def test_typed_recommendation_path_does_not_replay_complete_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = _execution(_ExecutionSpec("comparative_change", "observed"))

    def forbidden_replay(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("typed_internal_recommendation_must_not_replay_settlement")

    monkeypatch.setattr(
        ClaimSettlement,
        "from_dict",
        classmethod(forbidden_replay),
    )

    result = run_semantic_authority_workflow(
        execution,
        authority_namespace=_namespace(execution),
        llm_client=_FakeLLM(
            (_accept_claims, _one_recommendation, _accept_recommendation)
        ),
    )

    assert len(result.recommendations) == 1


def test_verified_evidence_semantic_calls_disable_thinking() -> None:
    execution = _execution(_ExecutionSpec("comparative_change", "observed"))
    client = _FakeLLM(
        (_accept_claims, _one_recommendation, _accept_recommendation)
    )
    client.supports_thinking_mode = True

    result = run_semantic_authority_workflow(
        execution,
        authority_namespace=_namespace(execution),
        llm_client=client,
    )

    assert len(result.recommendations) == 1
    assert [
        (call["task"], call["thinking"])
        for call in client.calls
    ] == [
        ("semantic_authority_claim_verification", "disabled"),
        ("semantic_authority_recommendation_proposal", "disabled"),
        ("semantic_authority_recommendation_verification", "disabled"),
    ]


def _recommendation_with_semantics(
    call_input: Mapping[str, Any],
    *,
    action: str,
    action_domain: str,
    action_stage: str,
    expected_value: str,
    expected_value_kind: str,
    expected_value_mode: str,
    diagnostic_premise: str | None = None,
    diagnostic_mode: str | None = None,
    assumption_refs: Sequence[str] = (),
) -> Mapping[str, Any]:
    claim_ref = call_input["payload"]["verified_claims"][0]["claim_ref"]
    commitments = [
        {
            "commitment_kind": "action",
            "text": action,
            "supporting_claim_refs": [claim_ref],
            "diagnostic_mode": None,
            "action_domain": action_domain,
            "action_stage": action_stage,
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
            "expected_value_kind": expected_value_kind,
            "expected_value_mode": expected_value_mode,
        },
    ]
    if diagnostic_premise is not None:
        commitments.append(
            {
                "commitment_kind": "diagnostic_premise",
                "text": diagnostic_premise,
                "supporting_claim_refs": [claim_ref],
                "diagnostic_mode": diagnostic_mode,
                "action_domain": None,
                "action_stage": None,
                "expected_value_kind": None,
                "expected_value_mode": None,
            }
        )
    return {
        "recommendation_proposals": [
            {
                "commitment_contract_version": "recommendation-commitments.v1",
                "commitments": commitments,
                "supporting_claim_refs": [claim_ref],
                "assumption_refs": list(assumption_refs),
                "risk_refs": ["limitation:aggregate"],
                "action": action,
                "applicable_conditions": ["The accepted window remains current."],
                "expected_decision_value": expected_value,
            }
        ]
    }


def _one_recommendation(call_input: Mapping[str, Any]) -> Mapping[str, Any]:
    return _recommendation_with_semantics(
        call_input,
        action="Inspect the accepted aggregate change.",
        action_domain="analysis",
        action_stage="investigate",
        expected_value="Prioritize the material change.",
        expected_value_kind="information_gain",
        expected_value_mode="expected_effect",
    )


def _recommendation_with_internal_condition(
    call_input: Mapping[str, Any],
) -> Mapping[str, Any]:
    output = deepcopy(_one_recommendation(call_input))
    output["recommendation_proposals"][0]["applicable_conditions"] = [
        call_input["payload"]["verified_claims"][0]["claim_ref"]
    ]
    return output


def _accept_recommendation(call_input: Mapping[str, Any]) -> Mapping[str, Any]:
    proposal = call_input["payload"]["recommendation_proposal"]
    return {
        "decision": {
            "subject_ref": proposal["recommendation_proposal_ref"],
            "disposition": "accepted",
            "veto_basis": None,
            "reason_code": None,
            "limitation_refs": [],
            "verified_commitment_refs": proposal["recommendation_commitment_refs"],
        }
    }


def test_projects_only_aggregate_evidence_and_returns_a_sealable_authority_input() -> (
    None
):
    execution = _execution(_ExecutionSpec("comparative_change", "observed"))
    client = _FakeLLM((_accept_claims, _one_recommendation, _accept_recommendation))

    result = run_semantic_authority_workflow(
        execution,
        authority_namespace=_namespace(execution),
        llm_client=client,
    )

    projection = result.projection.to_dict()
    serialized = json.dumps(projection, sort_keys=True)
    assert "sql" not in serialized.casefold()
    assert "raw_rows" not in serialized.casefold()
    assert "owner_ref" not in serialized.casefold()
    assert projection["aggregate_evidence"][0]["observation_facts"] == [
        {"name": "absolute_change", "value": -12}
    ]
    assert result.settlement.claim_graph.authority_mode == "claim_bearing"
    assert len(result.recommendations) == 1
    assert result.authority_bundle_inputs.recommendations == result.recommendations
    assert [item.purpose for item in result.provider_responses] == [
        "claim_verification",
        "recommendation_proposal",
        "recommendation_verification",
    ]


def test_internal_ref_in_public_recommendation_is_audited_and_cannot_block_answer() -> (
    None
):
    execution = _execution(_ExecutionSpec("comparative_change", "observed"))
    client = _FakeLLM(
        (
            _accept_claims,
            _recommendation_with_internal_condition,
            _accept_recommendation,
        )
    )

    result = run_semantic_authority_workflow(
        execution,
        authority_namespace=_namespace(execution),
        llm_client=client,
    )

    assert len(result.recommendation_proposals) == 1
    assert result.recommendations == ()
    assert result.authority_bundle_inputs.recommendations == ()
    decision = result.recommendation_verification_decisions[0]
    assert decision.disposition == "vetoed"
    assert decision.veto_basis == "contract_or_provenance_invalid"
    assert decision.reason_code == "public_recommendation_internal_ref_forbidden"
    assert json.loads(result.provider_responses[-1].content)["decision"][
        "disposition"
    ] == "accepted"


def test_recommendation_input_projects_typed_authorization_from_claim_ceiling() -> None:
    execution = _execution(
        _ExecutionSpec(
            "comparative_change",
            "statistical_association",
            evidence_kind="statistical_association",
            maximum_claim_strength="candidate_driver",
        )
    )
    client = _FakeLLM((_accept_claims, _no_recommendations))

    run_semantic_authority_workflow(
        execution,
        authority_namespace=_namespace(execution),
        llm_client=client,
    )

    payload = client.calls[1]["call_input"]["payload"]
    claim = payload["verified_claims"][0]
    authorization_by_ref = {
        item["recommendation_authorization_ref"]: item["authorization"]
        for item in payload["recommendation_authorization_catalog"]
    }
    authorization = authorization_by_ref[
        claim["recommendation_authorization_ref"]
    ]
    assert {
        "action_domain": "business_operation",
        "action_stage": "experiment",
    } in authorization["actions"]
    assert {
        "action_domain": "business_operation",
        "action_stage": "intervene",
    } not in authorization["actions"]
    assert {
        "expected_value_kind": "business_metric_effect",
        "expected_value_mode": "expected_effect",
    } not in authorization["expected_values"]


@pytest.mark.parametrize(
    "spec",
    (
        _ExecutionSpec(
            "comparative_change",
            "statistical_association",
            evidence_kind="statistical_association",
            maximum_claim_strength="candidate_driver",
        ),
        _ExecutionSpec(
            "formula_component_contribution",
            "derived",
            evidence_kind="derived",
            maximum_claim_strength="quantified_contribution",
            supported_claim_kinds=("formula_component_contribution",),
        ),
    ),
    ids=("candidate-driver", "accounting-contribution"),
)
def test_noncausal_claim_ceiling_omits_intervention_and_preserves_analysis(
    spec: _ExecutionSpec,
) -> None:
    execution = _execution(spec)

    def overreach(call_input: Mapping[str, Any]) -> Mapping[str, Any]:
        return _recommendation_with_semantics(
            call_input,
            action="Resolve the diagnosed issue in production.",
            action_domain="business_operation",
            action_stage="intervene",
            expected_value="Recover the projected revenue loss.",
            expected_value_kind="business_metric_effect",
            expected_value_mode="expected_effect",
        )

    result = run_semantic_authority_workflow(
        execution,
        authority_namespace=_namespace(execution),
        llm_client=_FakeLLM((_accept_claims, overreach)),
    )

    assert result.settlement.accepted_claims
    assert result.recommendation_proposals == ()
    assert result.recommendations == ()
    assert result.authority_bundle_inputs.recommendations == ()
    assert result.provider_audits[-1].payload["policy_rejections"] == (
        {
            "disposition": "rejected",
            "proposal_index": 0,
            "reason_code": "recommendation_commitment_claim_ceiling_exceeded",
        },
    )
    assert SemanticAuthorityResult.from_dict(result.to_dict()) == result


def test_noncausal_claim_can_authorize_an_experiment_with_hypothesis_value() -> None:
    execution = _execution(
        _ExecutionSpec(
            "formula_component_contribution",
            "derived",
            evidence_kind="derived",
            maximum_claim_strength="quantified_contribution",
            supported_claim_kinds=("formula_component_contribution",),
        )
    )

    def experiment(call_input: Mapping[str, Any]) -> Mapping[str, Any]:
        return _recommendation_with_semantics(
            call_input,
            action="Run a controlled test of the candidate lever.",
            action_domain="business_operation",
            action_stage="experiment",
            expected_value="Test the hypothesis that the lever affects revenue.",
            expected_value_kind="business_metric_effect",
            expected_value_mode="hypothesis",
        )

    result = run_semantic_authority_workflow(
        execution,
        authority_namespace=_namespace(execution),
        llm_client=_FakeLLM((_accept_claims, experiment, _accept_recommendation)),
    )

    assert len(result.recommendations) == 1
    action_commitment = next(
        item
        for item in result.recommendations[0].commitments
        if item.commitment_kind == "action"
    )
    assert action_commitment.action_stage == "experiment"


def test_diagnostic_premise_upgrade_is_omitted_without_losing_claims() -> None:
    execution = _execution(_ExecutionSpec("comparative_change", "observed"))

    def causal_premise(call_input: Mapping[str, Any]) -> Mapping[str, Any]:
        return _recommendation_with_semantics(
            call_input,
            action="Inspect the accepted aggregate change.",
            action_domain="analysis",
            action_stage="investigate",
            expected_value="Clarify the material change.",
            expected_value_kind="information_gain",
            expected_value_mode="expected_effect",
            diagnostic_premise="The observed factor caused the change.",
            diagnostic_mode="causal",
        )

    result = run_semantic_authority_workflow(
        execution,
        authority_namespace=_namespace(execution),
        llm_client=_FakeLLM((_accept_claims, causal_premise)),
    )

    assert result.settlement.accepted_claims
    assert result.recommendation_proposals == ()
    assert result.recommendations == ()
    assert result.provider_audits[-1].payload["policy_rejections"]


def test_scenario_claim_requires_conditional_value() -> None:
    execution = _execution(
        _ExecutionSpec(
            "scenario",
            "scenario",
            evidence_kind="scenario",
            maximum_claim_strength="scenario",
            supported_claim_kinds=("scenario",),
        )
    )

    def conditional_action(call_input: Mapping[str, Any]) -> Mapping[str, Any]:
        return _recommendation_with_semantics(
            call_input,
            action="Scale the operating plan under the stated scenario.",
            action_domain="business_operation",
            action_stage="scale",
            expected_value="Reach the modeled result if the scenario holds.",
            expected_value_kind="business_metric_effect",
            expected_value_mode="conditional_scenario",
        )

    result = run_semantic_authority_workflow(
        execution,
        authority_namespace=_namespace(execution),
        llm_client=_FakeLLM(
            (_accept_claims, conditional_action, _accept_recommendation)
        ),
    )
    assert len(result.recommendations) == 1

    def unconditional_effect(call_input: Mapping[str, Any]) -> Mapping[str, Any]:
        return _recommendation_with_semantics(
            call_input,
            action="Scale the operating plan under the stated scenario.",
            action_domain="business_operation",
            action_stage="scale",
            expected_value="Reach the modeled result if the scenario holds.",
            expected_value_kind="business_metric_effect",
            expected_value_mode="expected_effect",
        )

    rejected = run_semantic_authority_workflow(
        execution,
        authority_namespace=_namespace(execution),
        llm_client=_FakeLLM((_accept_claims, unconditional_effect)),
    )

    assert rejected.settlement.accepted_claims
    assert rejected.recommendation_proposals == ()
    assert rejected.recommendations == ()
    assert rejected.provider_audits[-1].payload["policy_rejections"]


def test_accepted_recommendation_decision_must_cover_every_commitment_ref() -> None:
    execution = _execution(_ExecutionSpec("comparative_change", "observed"))

    def incomplete_acceptance(call_input: Mapping[str, Any]) -> Mapping[str, Any]:
        proposal = call_input["payload"]["recommendation_proposal"]
        return {
            "decision": {
                "subject_ref": proposal["recommendation_proposal_ref"],
                "disposition": "accepted",
                "veto_basis": None,
                "reason_code": None,
                "limitation_refs": [],
                "verified_commitment_refs": proposal["recommendation_commitment_refs"][
                    :-1
                ],
            }
        }

    with pytest.raises(
        SemanticAuthorityWorkflowError,
        match="recommendation_verification_output_acceptance_invalid",
    ):
        run_semantic_authority_workflow(
            execution,
            authority_namespace=_namespace(execution),
            llm_client=_FakeLLM(
                (_accept_claims, _one_recommendation, incomplete_acceptance)
            ),
        )


def test_claim_verification_projects_repeated_records_once_with_lossless_encoding() -> (
    None
):
    members = [
        {
            "member": f"segment-{index}",
            "baseline_value": index * 10,
            "target_value": index * 11,
        }
        for index in range(40)
    ]
    execution = _execution(
        _ExecutionSpec(
            "comparative_change",
            "observed",
            observation_value={
                "finding_type": "segment_change",
                "members": members,
            },
        )
    )
    client = _FakeLLM((_accept_claims, _no_recommendations))

    result = run_semantic_authority_workflow(
        execution,
        authority_namespace=_namespace(execution),
        llm_client=client,
    )

    claim_input = client.calls[0]["call_input"]["payload"]
    projected_claim = claim_input["proposed_claims"][0]
    projected_evidence = claim_input["aggregate_evidence"][0]
    source_observations = result.projection.to_dict()["aggregate_evidence"][0][
        "observation_facts"
    ]
    serialized_claim = json.dumps(projected_claim, ensure_ascii=False)
    serialized_call = json.dumps(claim_input, ensure_ascii=False)

    assert "evidence_observations" not in serialized_claim
    assert projected_claim["evidence_entry_refs"] == [
        projected_evidence["evidence_entry_ref"]
    ]
    assert (
        _decode_semantic_projection(projected_evidence["observation_facts"]["value"])
        == source_observations
    )
    assert projected_evidence["observation_facts"]["source_digest"] == (
        canonical_digest(source_observations)
    )
    assert serialized_call.count("segment-0") == 1


def test_recommendation_proposal_reuses_verified_claim_graph_without_evidence_replay() -> None:
    members = [
        {
            "member": f"segment-{index}",
            "baseline_value": index * 10,
            "target_value": index * 11,
        }
        for index in range(40)
    ]
    execution = _execution(
        _ExecutionSpec(
            "comparative_change",
            "observed",
            observation_value={
                "finding_type": "segment_change",
                "members": members,
            },
        )
    )
    client = _FakeLLM((_accept_claims, _one_recommendation, _accept_recommendation))

    result = run_semantic_authority_workflow(
        execution,
        authority_namespace=_namespace(execution),
        llm_client=client,
    )

    assert len(result.recommendations) == 1
    for call_index, claims_field, output_key in (
        (0, "proposed_claims", "decisions"),
        (1, "verified_claims", "recommendation_proposals"),
        (2, "supporting_claims", "decision"),
    ):
        assert client.calls[call_index]["prompt_version"] == (
            "single-authority-phase04.v13"
        )
        assert output_key in client.calls[call_index]["messages"][0]["content"]
        payload = client.calls[call_index]["call_input"]["payload"]
        serialized = json.dumps(payload, ensure_ascii=False)
        assert serialized.count("segment-0") == 1
        assert "evidence_observations" not in json.dumps(
            payload[claims_field], ensure_ascii=False
        )
        if call_index == 1:
            assert "aggregate_evidence" not in payload
            evidence = payload["verified_evidence_context"][0]
            assert set(evidence) == {
                "evidence_entry_ref",
                "evidence_kind",
                "dimension_path",
                "window_refs",
                "limitation_refs",
                "observation_facts",
            }
            claim = payload[claims_field][0]
            assert "content_digest" not in claim
            assert "factual_payload_digest" not in claim
            assert "support_sources" not in claim
        else:
            evidence = payload["aggregate_evidence"][0]
            assert (
                _decode_semantic_projection(evidence["observation_facts"]["value"])
                == result.projection.to_dict()["aggregate_evidence"][0][
                    "observation_facts"
                ]
            )


def test_semantic_projection_round_trip_preserves_empty_records_and_reserved_tags() -> (
    None
):
    source = {
        "empty_records": [{}, {}],
        "domain_value": {
            "__waje_semantic_encoding__": "business-owned-value",
            "members": [{"member": "segment-a"}, {"member": "segment-b"}],
        },
    }

    assert _decode_semantic_projection(_encode_semantic_projection(source)) == source


def test_candidate_claim_is_one_typed_proposal_call_bound_to_known_refs() -> None:
    members = [
        {"member": f"segment-{index}", "share": index / 100} for index in range(40)
    ]
    execution = _execution(
        _ExecutionSpec(
            "candidate_mechanism",
            "observed",
            maximum_claim_strength="candidate_mechanism",
            supported_claim_kinds=("candidate_mechanism",),
            observation_value={"finding_type": "segment_mix", "members": members},
        )
    )

    def propose(call_input: Mapping[str, Any]) -> Mapping[str, Any]:
        projection = call_input["payload"]
        obligation = projection["obligations"][0]
        return {
            "candidate_claim_proposals": [
                {
                    "obligation_id": obligation["obligation_id"],
                    "subject": "Merchant mix is a candidate mechanism.",
                    "factual_payload": {
                        "candidate": "merchant_mix",
                        "interpretation": "Candidate only",
                    },
                    "assumption_refs": ["assumption:accepted"],
                    "limitation_refs": ["limitation:aggregate"],
                }
            ]
        }

    client = _FakeLLM((propose, _accept_claims, _no_recommendations))
    client.supports_thinking_mode = True
    result = run_semantic_authority_workflow(
        execution,
        authority_namespace=_namespace(execution),
        llm_client=client,
    )

    assert len(result.candidate_proposals) == 1
    assert result.settlement.accepted_claims[0].claim_class == "candidate_mechanism"
    assert [item.purpose for item in result.provider_responses] == [
        "candidate_claim_proposal",
        "claim_verification",
        "recommendation_proposal",
    ]
    assert "candidate_claim_proposals" in client.calls[0]["messages"][0]["content"]
    assert client.calls[0]["thinking"] == "disabled"
    assert (
        "factual_payload is a non-empty JSON object"
        in (client.calls[0]["messages"][0]["content"])
    )
    assert client.calls[0]["prompt_version"] == "single-authority-phase04.v13"
    assert set(client.calls[0]["call_input"]["payload"]) == {
        "obligations",
        "aggregate_evidence",
        "assumption_refs",
        "limitation_refs",
    }
    assert len(client.calls[0]["call_input"]["payload"]["obligations"]) == 1
    assert len(client.calls[0]["call_input"]["payload"]["aggregate_evidence"]) == 1
    assert result.candidate_proposals[0].proposal_item_ref.startswith(
        "candidate-proposal-item:sha256:"
    )
    candidate_evidence = client.calls[0]["call_input"]["payload"]["aggregate_evidence"][
        0
    ]
    assert (
        _decode_semantic_projection(candidate_evidence["observation_facts"]["value"])
        == result.projection.to_dict()["aggregate_evidence"][0]["observation_facts"]
    )


def test_candidate_hypothesis_composes_evidence_from_a_different_source_claim() -> (
    None
):
    execution = _execution(
        _ExecutionSpec(
            "candidate_mechanism",
            "derived",
            obligation_role="analyst_auxiliary",
            investigation_mode="hypothesis_test",
            required_claim_strength="candidate_mechanism",
            evidence_kind="derived",
            maximum_claim_strength="quantified_contribution",
            supported_claim_kinds=("formula_component_contribution",),
            observation_value={"paid_users_contribution": 0.72},
        )
    )

    def propose(call_input: Mapping[str, Any]) -> Mapping[str, Any]:
        obligation = call_input["payload"]["obligations"][0]
        return {
            "candidate_claim_proposals": [
                {
                    "obligation_id": obligation["obligation_id"],
                    "subject": "付费人数增长是候选解释。",
                    "factual_payload": {
                        "candidate": "paid_users_growth",
                        "interpretation": "candidate_only",
                    },
                    "assumption_refs": [],
                    "limitation_refs": ["limitation:aggregate"],
                }
            ]
        }

    client = _FakeLLM((propose, _accept_claims, _no_recommendations))
    result = run_semantic_authority_workflow(
        execution,
        authority_namespace=_namespace(execution),
        llm_client=client,
    )

    assert len(result.candidate_proposals) == 1
    assert result.candidate_proposals[0].evidence_support[
        0
    ].source_claim_kind == "formula_component_contribution"
    assert result.settlement.accepted_claims[0].claim_class == (
        "candidate_mechanism"
    )


def test_zero_candidate_proposals_do_not_trigger_a_local_claim_fallback() -> None:
    execution = _execution(
        _ExecutionSpec(
            "candidate_mechanism",
            "observed",
            maximum_claim_strength="candidate_mechanism",
            supported_claim_kinds=("candidate_mechanism",),
        )
    )
    client = _FakeLLM((lambda _: {"candidate_claim_proposals": []},))

    result = run_semantic_authority_workflow(
        execution,
        authority_namespace=_namespace(execution),
        llm_client=client,
    )

    assert len(client.calls) == 1
    assert result.candidate_proposals == ()
    assert result.settlement.claim_graph.authority_mode == "boundary_only"
    assert result.claim_verification_attempt is None
    assert result.recommendations == ()
    assert result.replay() == result


def test_candidate_provider_cannot_override_runtime_evidence_binding() -> None:
    execution = _execution(
        _ExecutionSpec(
            "candidate_mechanism",
            "observed",
            maximum_claim_strength="candidate_mechanism",
            supported_claim_kinds=("candidate_mechanism",),
        )
    )

    def propose(call_input: Mapping[str, Any]) -> Mapping[str, Any]:
        obligation = call_input["payload"]["obligations"][0]
        return {
            "candidate_claim_proposals": [
                {
                    "obligation_id": obligation["obligation_id"],
                    "subject": "Unknown evidence candidate.",
                    "factual_payload": {"candidate": "unknown"},
                    "evidence_support": [
                        {
                            "evidence_entry_ref": "evidence-ledger-entry:unknown",
                            "source_claim_kind": "candidate_mechanism",
                        }
                    ],
                    "assumption_refs": [],
                    "limitation_refs": [],
                }
            ]
        }

    with pytest.raises(
        SemanticAuthorityWorkflowError,
        match="candidate_claim_output_item_shape_invalid",
    ):
        run_semantic_authority_workflow(
            execution,
            authority_namespace=_namespace(execution),
            llm_client=_FakeLLM((propose,)),
        )


def test_claim_verifier_requires_one_decision_for_every_proposed_subject() -> None:
    execution = _execution(_ExecutionSpec("comparative_change", "observed"))
    client = _FakeLLM((lambda _: {"decisions": {}},))

    with pytest.raises(
        SemanticAuthorityWorkflowError,
        match="claim_verification_output_coverage_invalid",
    ):
        run_semantic_authority_workflow(
            execution,
            authority_namespace=_namespace(execution),
            llm_client=client,
        )

    assert len(client.calls) == 0


def test_claim_verification_input_exposes_only_claim_relative_references() -> None:
    execution = _execution(_ExecutionSpec("comparative_change", "observed"))
    projection = _projection(execution)
    checkpoint = prepare_claim_settlement(
        execution,
        authority_namespace=_namespace(execution),
        candidate_proposals=(),
    )

    call_input = semantic_workflow._claim_call_input(checkpoint, projection)

    assert "assumption_refs" not in call_input.payload
    assert "limitation_refs" not in call_input.payload
    obligation = call_input.payload["obligations"][0]
    assert obligation["success_policy"] == {
        "policy": "verified_or_explicit_boundary",
        "minimum_claim_strength": "directional",
    }
    assert obligation["required_claim_strength"] == "directional"


def test_any_of_evidence_requirement_is_claim_local_for_cross_kind_claims() -> None:
    execution = _execution(
        _ExecutionSpec(
            "comparative_change",
            ("observed", "statistical_association"),
            additional_evidence=(
                {
                    "evidence_ref": "evidence:association",
                    "evidence_kind": "statistical_association",
                    "maximum_claim_strength": "directional",
                    "supported_claim_kinds": ("comparative_change",),
                    "observation_value": -9,
                },
            ),
        )
    )
    projection = _projection(execution)
    checkpoint = prepare_claim_settlement(
        execution,
        authority_namespace=_namespace(execution),
        candidate_proposals=(),
    )

    call_input = semantic_workflow._claim_call_input(checkpoint, projection)
    obligation = call_input.payload["obligations"][0]
    proposed_claims = call_input.payload["proposed_claims"]

    assert len(proposed_claims) == 2
    assert obligation["evidence_requirement"] == {
        "operator": "any_of",
        "evidence_kinds": ("observed", "statistical_association"),
    }
    assert {tuple(item["bound_evidence_kinds"]) for item in proposed_claims} == {
        ("observed",),
        ("statistical_association",),
    }
    assert {item["evidence_requirement_status"] for item in proposed_claims} == {
        "satisfied"
    }

    with pytest.raises(
        SemanticAuthorityWorkflowError,
        match="claim_verification_output_evidence_requirement_veto_invalid",
    ):
        semantic_workflow._claim_verification_output_validator(
            {
                "decisions": {
                    item["claim_ref"]: {
                        "disposition": "vetoed",
                        "veto_basis": "evidence_requirement_unsatisfied",
                        "reason_code": "requires_every_listed_evidence_kind",
                        "limitation_refs": [],
                    }
                    for item in proposed_claims
                }
            },
            checkpoint=checkpoint,
            projection=projection,
        )

    prompt = semantic_workflow._PROMPTS["claim_verification"]
    assert "operator any_of" in prompt
    assert "at least one listed evidence_kind" in prompt
    assert "same obligation's other proposed claims" in prompt


def test_claim_verifier_rejects_limitation_outside_subject_closure() -> None:
    execution = _execution(_ExecutionSpec("comparative_change", "observed"))
    checkpoint = prepare_claim_settlement(
        execution,
        authority_namespace=_namespace(execution),
        candidate_proposals=(),
    )
    proposed = checkpoint.proposed_claims[0]
    output = {
        "decisions": {
            proposed.claim_ref: {
                "disposition": "vetoed",
                "veto_basis": "factual_support_invalid",
                "reason_code": "factual_mismatch",
                "limitation_refs": ["limitation:borrowed-from-other-claim"],
            }
        }
    }

    with pytest.raises(
        SemanticAuthorityWorkflowError,
        match="claim_verification_output_limitation_closure_invalid",
    ):
        semantic_workflow._claim_verification_output_validator(
            output,
            checkpoint=checkpoint,
            projection=_projection(execution),
        )


def test_claim_verifier_allows_reasoned_veto_without_borrowed_limitation() -> None:
    execution = _execution(_ExecutionSpec("comparative_change", "observed"))
    checkpoint = prepare_claim_settlement(
        execution,
        authority_namespace=_namespace(execution),
        candidate_proposals=(),
    )
    proposed = checkpoint.proposed_claims[0]

    semantic_workflow._claim_verification_output_validator(
        {
            "decisions": {
                proposed.claim_ref: {
                    "disposition": "vetoed",
                    "veto_basis": "factual_support_invalid",
                    "reason_code": "factual_mismatch",
                    "limitation_refs": [],
                }
            }
        },
        checkpoint=checkpoint,
        projection=_projection(execution),
    )


def test_claim_verification_prompt_is_class_relative() -> None:
    prompt = semantic_workflow._PROMPTS["claim_verification"]

    assert semantic_workflow._SEMANTIC_PROMPT_VERSION == "single-authority-phase04.v13"
    assert "declared claim_class and publication_ceiling" in prompt
    assert "success_policy" in prompt
    assert "required_claim_strength" in prompt
    assert "does not veto a claim within its declared boundary" in prompt
    assert "remain attached to the accepted claim" in prompt


def test_boundary_only_settlement_uses_local_authority_and_skips_every_llm_call() -> (
    None
):
    execution = _execution(
        _ExecutionSpec(
            "comparative_change",
            "observed",
            status="unavailable",
            limitation_refs=("limitation:source-unavailable",),
        )
    )

    result = run_semantic_authority_workflow(
        execution,
        authority_namespace=_namespace(execution),
        llm_client=_NoCallLLM(),
    )

    assert result.settlement.claim_graph.authority_mode == "boundary_only"
    assert result.claim_verification_attempt is None
    assert result.provider_responses == ()
    assert result.recommendations == ()
    assert SemanticAuthorityResult.from_dict(result.to_dict()) == result


def test_vetoed_recommendation_is_audited_and_omitted_without_replacement() -> None:
    execution = _execution(_ExecutionSpec("comparative_change", "observed"))

    def veto(call_input: Mapping[str, Any]) -> Mapping[str, Any]:
        proposal_ref = call_input["payload"]["recommendation_proposal"][
            "recommendation_proposal_ref"
        ]
        return {
            "decision": {
                "subject_ref": proposal_ref,
                "disposition": "vetoed",
                "veto_basis": "recommendation_support_invalid",
                "reason_code": "risk_not_resolved",
                "limitation_refs": [],
                "verified_commitment_refs": [],
            }
        }

    client = _FakeLLM((_accept_claims, _one_recommendation, veto))
    result = run_semantic_authority_workflow(
        execution,
        authority_namespace=_namespace(execution),
        llm_client=client,
    )

    assert len(result.recommendation_proposals) == 1
    assert len(result.recommendation_verification_attempts) == 1
    assert result.recommendation_verification_decisions[0].disposition == "vetoed"
    assert result.recommendations == ()
    assert result.authority_bundle_inputs.recommendations == ()
    assert len(client.calls) == 3


def test_all_provider_attempt_responses_are_preserved_with_final_attempt_identity() -> (
    None
):
    execution = _execution(_ExecutionSpec("comparative_change", "observed"))
    client = _FakeLLM(
        (_accept_claims, _no_recommendations),
        retry_audit_call=0,
    )

    result = run_semantic_authority_workflow(
        execution,
        authority_namespace=_namespace(execution),
        llm_client=client,
    )

    claim_responses = tuple(
        item
        for item in result.provider_responses
        if item.purpose == "claim_verification"
    )
    assert [item.attempt_number for item in claim_responses] == [1, 2]
    assert len({item.attempt_id for item in claim_responses}) == 2
    assert result.claim_verification_attempt is not None
    assert result.claim_verification_attempt.attempt_number == 2
    assert (
        result.claim_verification_attempt.raw_provider_response_ref
        == claim_responses[-1].response_ref
    )
    assert claim_responses[-1].provider_ref == "provider:test"
    assert claim_responses[-1].model_ref == "model:semantic-authority"
    assert SemanticAuthorityResult.from_dict(result.to_dict()) == result


def test_restricted_projection_rejects_forbidden_nested_row_material() -> None:
    execution = _execution(
        _ExecutionSpec(
            "comparative_change",
            "observed",
            observation_value={"raw_rows": [{"paid_amount": 12}]},
        )
    )

    with pytest.raises(
        SemanticAuthorityWorkflowError,
        match="restricted_aggregate_evidence_forbidden_field:raw_rows",
    ):
        _projection(execution)


def test_semantic_authority_checkpoint_round_trips_every_child_and_audit() -> None:
    execution = _execution(_ExecutionSpec("comparative_change", "observed"))
    result = run_semantic_authority_workflow(
        execution,
        authority_namespace=_namespace(execution),
        llm_client=_FakeLLM(
            (_accept_claims, _one_recommendation, _accept_recommendation)
        ),
    )

    replayed = SemanticAuthorityResult.from_dict(result.to_dict())

    assert replayed == result
    assert replayed.replay() == result
    assert len(replayed.provider_audits) == 3
    assert replayed.provider_audits[0].provider_response_refs == (
        replayed.provider_responses[0].response_ref,
    )
    with pytest.raises(TypeError):
        replayed.provider_audits[0].payload["model"] = "tampered"  # type: ignore[index]


def test_semantic_authority_checkpoint_rejects_nested_provider_audit_tampering() -> (
    None
):
    execution = _execution(_ExecutionSpec("comparative_change", "observed"))
    result = run_semantic_authority_workflow(
        execution,
        authority_namespace=_namespace(execution),
        llm_client=_FakeLLM((_accept_claims, _no_recommendations)),
    )
    tampered = deepcopy(result.to_dict())
    tampered["provider_audits"][0]["payload"]["model"] = "model:forged"

    with pytest.raises(
        SemanticAuthorityWorkflowError,
        match="restricted_provider_audit_model_invalid|restricted_provider_audit_integrity_invalid|semantic_authority_result_provider_audits_invalid",
    ):
        SemanticAuthorityResult.from_dict(tampered)


def test_accepted_checkpoint_hydrates_without_invoking_the_llm_again() -> None:
    execution = _execution(_ExecutionSpec("comparative_change", "observed"))
    client = _FakeLLM((_accept_claims, _no_recommendations))
    accepted = run_semantic_authority_workflow(
        execution,
        authority_namespace=_namespace(execution),
        llm_client=client,
    )
    call_count = len(client.calls)

    hydrated = SemanticAuthorityResult.from_dict(accepted.to_dict())

    assert hydrated == accepted
    assert len(client.calls) == call_count
    assert hydrated.authority_bundle_inputs == accepted.authority_bundle_inputs
