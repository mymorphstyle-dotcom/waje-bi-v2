from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

import pytest

from bi_agent.runtime.authoritative_execution_result import AuthoritativeExecutionResult
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
from bi_agent.runtime.claim_authority import (
    AuthorityBundle,
    ClaimAuthorityContractError,
    ClaimAuthorityNamespace,
    ClaimGraph,
    ObligationCoverage,
    RECOMMENDATION_COMMITMENT_CONTRACT_VERSION,
    RecommendationCommitment,
    RecommendationProposal,
    RecommendationRecord,
    SemanticVerificationAttempt,
    SemanticVerificationDecision,
)
from bi_agent.runtime.claim_settlement import (
    AuthorityBundleInputs,
    CandidateClaimProposal,
    CandidateEvidenceSupport,
    ClaimSettlement,
    ClaimSettlementCheckpoint,
    ClaimSettlementContractError,
    prepare_claim_settlement,
    settle_claim_checkpoint,
)
from bi_agent.runtime.claim_settlement import (
    _direct_claim_proposals as direct_claim_proposals,
)
from bi_agent.runtime.claim_settlement import _evidence_records as evidence_records
from bi_agent.runtime.claim_settlement import (
    _create_settlement as create_settlement,
)
from bi_agent.runtime.evidence_authority import canonical_digest
from bi_agent.runtime.narrative_materialization import (
    build_public_limitation_contexts,
)
from bi_agent.runtime.plan_authority import (
    AnalysisAxis,
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


@dataclass(frozen=True)
class _EvidenceSpec:
    evidence_kind: str
    maximum_claim_strength: str
    supported_claim_kinds: tuple[str, ...]
    observation_name: str
    observation_value: object
    evidence_strength: str = "qualified"
    dimension_path: tuple[str, ...] = ()
    limitation_refs: tuple[str, ...] = ()
    data_contract_state: str = "complete"
    binding: bool = True
    result_membership: bool = True
    completeness_membership: bool = True
    observation_fact: Mapping[str, object] | None = None
    scope: str | None = None
    window_refs: tuple[str, ...] | None = None


@dataclass(frozen=True)
class _TaskSpec:
    task_key: str
    capability_id: str
    obligation_names: tuple[str, ...]
    evidence: tuple[_EvidenceSpec, ...] = ()
    status: str = "succeeded"
    limitation_refs: tuple[str, ...] = ()
    failure_integrity_level: str | None = None


_RUNTIME_REGISTRY = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)


def _required_claim_strength(claim_kind: str) -> str:
    if claim_kind == "scenario":
        return "scenario"
    return _RUNTIME_REGISTRY.claim_publication_requirements[claim_kind]


def _event_temporal_authority():
    return resolved_test_temporal_authority(
        time_spec={"kind": "date", "target": "2026-06-19"},
        comparison_spec={
            "kind": "event_relative_window",
            "event_ref": "business-event:campaign-june-2026",
            "target_start": "2026-06-19",
            "target_end": "2026-06-19",
            "baseline_start": "2026-06-18",
            "baseline_end": "2026-06-18",
            "aggregation": "sum_of_complete_days",
        },
        require_physical_baseline=True,
    )


def _event_contract_fact(
    *,
    evidence_contract: str,
    authority: Any,
    event_ref: str | None = None,
) -> dict[str, object]:
    return {
        "evidence_contract": evidence_contract,
        "event_ref": event_ref or authority.event_ref,
        "temporal_authority_ref": authority.authority_ref,
        "causal_interpretation_allowed": False,
    }


def _execution_result(
    *,
    obligations: dict[str, tuple[str, str | tuple[str, ...]]],
    tasks: tuple[_TaskSpec, ...],
    run_attempt_id: str = "run-claim-settlement",
    temporal_authority: Any = None,
    composite_policies: Mapping[str, Mapping[str, Any]] | None = None,
    evidence_scope: str = "scope:full-sample",
) -> AuthoritativeExecutionResult:
    composite_policies = composite_policies or {}
    obligation_by_name = {
        name: ClaimObligation.create(
            claim_kind=claim_kind,
            role="user_required" if index == 0 else "analyst_auxiliary",
            subject=(
                {
                    "target_metric_ref": "paid_amount",
                    "scope": {"market": "all", "currency": "USD"},
                    "outcome_refs": (f"outcome:{name}",),
                    "goal_refs": ("goal:explain",),
                }
                if index == 0
                else {
                    "planner_proposal_ref": "planner-proposal:claim-settlement",
                    "proposal_item_ref": f"proposal-item:{name}",
                    "target_metric_refs": ("paid_amount",),
                    "scope": {"market": "all", "currency": "USD"},
                    "goal_refs": ("goal:explain",),
                }
            ),
            evidence_requirement=EvidenceRequirement.create(
                operator="any_of",
                evidence_kinds=(
                    (minimum_evidence,)
                    if isinstance(minimum_evidence, str)
                    else minimum_evidence
                ),
            ),
            success_policy={
                "policy": "verified_or_explicit_boundary",
                "minimum_claim_strength": _required_claim_strength(claim_kind),
                **(
                    {"composite_support_policy": composite_policies[name]}
                    if name in composite_policies
                    else {}
                ),
            },
        )
        for index, (name, (claim_kind, minimum_evidence)) in enumerate(
            obligations.items()
        )
    }
    all_obligation_ids = tuple(
        item.obligation_id for item in obligation_by_name.values()
    )
    axis = AnalysisAxis.create(
        axis_id="claim_settlement",
        role="required",
        axis_kind="authority_settlement",
        target_metric_refs=("paid_amount",),
        metric_refs=(),
        dimension_refs=("region", "device"),
        context_source_refs=(),
        capability_refs=tuple(dict.fromkeys(item.capability_id for item in tasks)),
        reconciliation_group="paid_amount",
        selection_policy="retain_all_qualified_evidence",
        source_refs=("contract:test",),
        goal_refs=("goal:explain",),
        supports_obligation_ids=all_obligation_ids,
    )
    temporal_authority = temporal_authority or resolved_test_temporal_authority(
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
        run_attempt_id=run_attempt_id,
        supersedes_plan_revision_id=None,
        intent_revision_id="intent-claim-settlement",
        decision_refs=("decision:baseline",),
        authority_context_ref="authority-context:claim-settlement",
        planner_proposal_ref="planner-proposal:claim-settlement",
        proposal_admission_ref="proposal-admission:claim-settlement",
        temporal_authority=temporal_authority,
        resolved_window_refs=temporal_authority.resolved_window_refs,
        context_window_specs=(),
        claim_obligations=tuple(obligation_by_name.values()),
        analysis_axes=(axis,),
        capability_task_specs=tuple(
            {
                "task_key": item.task_key,
                "capability_id": item.capability_id,
                "normalized_input_refs": (
                    "authority-context:claim-settlement",
                    f"input:{item.task_key}",
                ),
                "dependency_task_keys": (),
                "obligation_edges": tuple(
                    {
                        "obligation_id": obligation_by_name[name].obligation_id,
                        "required": True,
                    }
                    for name in item.obligation_names
                ),
                "execution_rank": execution_rank,
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
            }
            for execution_rank, item in enumerate(tasks, start=1)
        ),
        assumption_refs=(),
        budget_policy_ref="budget-policy:claim-settlement",
        contract_versions={"runtime": "single-authority-phase03.v1"},
    )
    task_by_key = {item.task_key: item for item in plan.capability_tasks}
    bundles = []
    for spec in tasks:
        task = task_by_key[spec.task_key]
        attempt = CapabilityAttempt.create(plan, task)
        failure = (
            CapabilityFailure.create(
                layer="capability",
                kind="shared_release_authority_invalid",
                scope="run",
                affected_refs=(task.task_id,),
                integrity_level=spec.failure_integrity_level,
                retryability="replan_required",
                user_actionable=False,
                business_boundary="Shared release authority could not be verified.",
                technical_detail_ref=f"technical-detail:{spec.task_key}",
            )
            if spec.failure_integrity_level is not None
            else None
        )
        failure_record = (
            FailureRecord.create(attempt, failure) if failure is not None else None
        )
        evidence = tuple(
            CapabilityEvidence.create(
                evidence_ref=f"evidence:{spec.task_key}:{index}",
                binding_record_ref=(
                    f"binding:{spec.task_key}:{index}" if item.binding else None
                ),
                execution_state="available",
                evidence_kind=item.evidence_kind,
                data_contract_state=item.data_contract_state,
                supported_claim_kinds=item.supported_claim_kinds,
                evidence_strength=item.evidence_strength,
                maximum_claim_strength=item.maximum_claim_strength,
                observation_facts=(
                    (
                        dict(item.observation_fact)
                        if item.observation_fact is not None
                        else {
                            "name": item.observation_name,
                            "value": item.observation_value,
                        }
                    ),
                ),
                scope=item.scope or evidence_scope,
                window_refs=item.window_refs or plan.resolved_window_refs,
                dimension_path=item.dimension_path,
                limitation_refs=item.limitation_refs,
                result_refs=(f"result:{spec.task_key}:{index}",)
                if item.result_membership
                else (),
                completeness_report_refs=(f"completeness:{spec.task_key}:{index}",)
                if item.completeness_membership
                else (),
                hierarchy_qualified=bool(item.dimension_path),
            )
            for index, item in enumerate(spec.evidence)
        )
        output = CapabilityAdapterOutput.create(
            status=spec.status,
            output_payload={"task": spec.task_key},
            evidence=evidence,
            affected_obligation_ids=task.supports_obligation_ids,
            limitation_refs=spec.limitation_refs,
            retryability="never" if spec.status == "succeeded" else "replan_required",
            failure=failure,
        )
        outcome = CapabilityOutcome.create(
            attempt,
            task,
            output,
            failure_ref=(
                failure_record.failure_ref if failure_record is not None else None
            ),
            budget_units=1,
        )
        ledger = tuple(
            EvidenceLedgerEntry.create(plan, task, outcome, item) for item in evidence
        )
        bundles.append(
            (
                attempt,
                outcome,
                ledger,
                (failure_record,) if failure_record is not None else (),
            )
        )

    outcomes = tuple(item[1] for item in bundles)
    evidence_entries = tuple(entry for item in bundles for entry in item[2])
    failure_records = tuple(item for bundle in bundles for item in bundle[3])
    stop = ExplorationStopRecord.create(
        plan,
        outcomes,
        reason=(
            "shared_authority_failure"
            if any(
                item.integrity_level == "shared_authority" for item in failure_records
            )
            else "plan_exhausted"
        ),
        hard_budget_limit=None,
    )
    snapshot = ExecutionSnapshot.create(
        plan,
        stop,
        outcomes,
        evidence_entries,
        failure_records,
    )
    input_payload, output_payload = capability_execution_transition_payloads(
        plan, snapshot, stop
    )
    transition = DurableTransition.create(
        node_name="execute_capability_dag",
        parent_transition_id="transition:phase02-plan-bound",
        run_attempt_id=plan.run_attempt_id,
        intent_revision_id=plan.intent_revision_id,
        decision_ledger_position=1,
        input_digest=canonical_digest(input_payload),
        output_digest=canonical_digest(output_payload),
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
        capability_outcome_bundles=tuple(bundles),
        durable_transition=transition,
    )


def _ledger_by_evidence_ref(
    execution: AuthoritativeExecutionResult,
) -> dict[str, EvidenceLedgerEntry]:
    return {
        entry.evidence_ref: entry
        for bundle in execution.capability_outcome_bundles
        for entry in bundle[2]
    }


def _namespace(
    execution: AuthoritativeExecutionResult,
) -> ClaimAuthorityNamespace:
    return ClaimAuthorityNamespace.create(
        run_attempt_id=execution.run_attempt_id,
        intent_revision_id=execution.intent_revision_id,
        plan_revision_id=execution.plan_revision_id,
    )


def _settle(
    execution: AuthoritativeExecutionResult,
    *,
    candidate_proposals: tuple[CandidateClaimProposal, ...] = (),
    veto_claim_classes: tuple[str, ...] = (),
) -> ClaimSettlement:
    namespace = _namespace(execution)
    checkpoint = prepare_claim_settlement(
        execution,
        authority_namespace=namespace,
        candidate_proposals=candidate_proposals,
    )
    if not checkpoint.proposed_claims:
        return settle_claim_checkpoint(
            checkpoint,
            verification_attempt=None,
            verification_decisions=(),
        )
    attempt = checkpoint.verification_attempt(
        provider_ref="provider:deepseek",
        model_ref="deepseek-chat",
        input_digest=canonical_digest(checkpoint.to_dict()),
        attempt_number=1,
        raw_provider_response_ref=("restricted-provider-response:claim-settlement"),
        raw_provider_response_digest=canonical_digest(
            {"checkpoint_ref": checkpoint.checkpoint_ref, "response": "accepted"}
        ),
    )
    assert attempt.raw_provider_response_ref == (
        "restricted-provider-response:claim-settlement"
    )
    assert (
        SemanticVerificationAttempt.from_dict(
            attempt.to_dict(), authority_namespace=namespace
        )
        == attempt
    )
    decisions = tuple(
        SemanticVerificationDecision.create(
            authority_namespace=namespace,
            verification_attempt=attempt,
            subject_ref=claim.claim_ref,
            disposition=(
                "vetoed" if claim.claim_class in veto_claim_classes else "accepted"
            ),
            veto_basis=(
                "semantic_boundary_exceeded"
                if claim.claim_class in veto_claim_classes
                else None
            ),
            reason_code=(
                "semantic_support_scope_mismatch"
                if claim.claim_class in veto_claim_classes
                else None
            ),
            limitation_refs=(
                ("limitation:semantic-scope",)
                if claim.claim_class in veto_claim_classes
                else ()
            ),
        )
        for claim in checkpoint.proposed_claims
    )
    return settle_claim_checkpoint(
        checkpoint,
        verification_attempt=attempt,
        verification_decisions=decisions,
    )


def _event_candidate_execution(
    *,
    presence_event_ref: str | None = None,
    presence_scope: str | None = None,
    evidence_window_refs: tuple[str, ...] | None = None,
) -> AuthoritativeExecutionResult:
    authority = _event_temporal_authority()
    scope_ref = "scope:sha256:" + canonical_digest({"market": "all", "currency": "USD"})
    return _execution_result(
        obligations={
            "impact": ("business_object_candidate_impact", "observed"),
            "mechanism": ("candidate_mechanism", "observed"),
        },
        tasks=(
            _TaskSpec(
                "event-window",
                "event_window_compare",
                ("impact",),
                evidence=(
                    _EvidenceSpec(
                        "observed",
                        "directional",
                        ("business_object_candidate_impact",),
                        "event_window",
                        "comparison",
                        evidence_strength="directional",
                        observation_fact=_event_contract_fact(
                            evidence_contract=("event-window-metric-comparison.v1"),
                            authority=authority,
                        ),
                        window_refs=evidence_window_refs,
                    ),
                ),
            ),
            _TaskSpec(
                "event-presence",
                "event_evidence",
                ("mechanism",),
                evidence=(
                    _EvidenceSpec(
                        "observed",
                        "candidate_mechanism",
                        ("candidate_mechanism",),
                        "event_presence",
                        "present",
                        evidence_strength="low",
                        observation_fact=_event_contract_fact(
                            evidence_contract="event-presence.v1",
                            authority=authority,
                            event_ref=presence_event_ref,
                        ),
                        scope=presence_scope,
                        window_refs=evidence_window_refs,
                    ),
                ),
            ),
        ),
        temporal_authority=authority,
        composite_policies={
            "impact": _RUNTIME_REGISTRY.claim_composite_support_policy(
                "business_object_candidate_impact"
            )
        },
        evidence_scope=scope_ref,
    )


def _event_candidate_proposal(
    execution: AuthoritativeExecutionResult,
    *,
    include_presence: bool = True,
    causal_interpretation_allowed: bool = False,
) -> CandidateClaimProposal:
    namespace = _namespace(execution)
    ledger = _ledger_by_evidence_ref(execution)
    obligation = next(
        item
        for item in execution.plan_revision.claim_obligations
        if item.claim_kind == "business_object_candidate_impact"
    )
    support = [
        CandidateEvidenceSupport.create(
            authority_namespace=namespace,
            evidence_entry_ref=ledger["evidence:event-window:0"].entry_ref,
            source_claim_kind="business_object_candidate_impact",
        )
    ]
    if include_presence:
        support.append(
            CandidateEvidenceSupport.create(
                authority_namespace=namespace,
                evidence_entry_ref=ledger["evidence:event-presence:0"].entry_ref,
                source_claim_kind="candidate_mechanism",
            )
        )
    return CandidateClaimProposal.create(
        authority_namespace=namespace,
        proposal_item_ref="planner-proposal-item:event-impact",
        obligation_id=obligation.obligation_id,
        subject="campaign_window_candidate_impact",
        factual_payload={
            "summary": "campaign is a candidate influence during the window",
            "causal_interpretation_allowed": causal_interpretation_allowed,
        },
        evidence_support=tuple(support),
        limitation_refs=("limitation:no-counterfactual",),
    )


def test_event_candidate_impact_requires_the_matching_composite_authority() -> None:
    execution = _event_candidate_execution()
    proposal = _event_candidate_proposal(execution)

    settlement = _settle(execution, candidate_proposals=(proposal,))

    claim = next(
        item
        for item in settlement.accepted_claims
        if item.claim_class == "candidate_impact"
    )
    assert claim.publication_ceiling.strength == "candidate_driver"
    assert claim.factual_payload["event_ref"] == (
        execution.plan_revision.temporal_authority.event_ref
    )
    assert claim.factual_payload["temporal_authority_ref"] == (
        execution.plan_revision.temporal_authority.authority_ref
    )
    assert claim.factual_payload["causal_interpretation_allowed"] is False


def test_event_candidate_impact_rejects_missing_presence_support() -> None:
    execution = _event_candidate_execution()
    proposal = _event_candidate_proposal(execution, include_presence=False)

    with pytest.raises(
        ClaimSettlementContractError,
        match="candidate_claim_composite_support_missing_or_ambiguous",
    ):
        prepare_claim_settlement(
            execution,
            authority_namespace=_namespace(execution),
            candidate_proposals=(proposal,),
        )


@pytest.mark.parametrize(
    ("execution", "error"),
    (
        (
            lambda: _event_candidate_execution(
                presence_event_ref="business-event:other"
            ),
            "candidate_claim_composite_authority_mismatch",
        ),
        (
            lambda: _event_candidate_execution(presence_scope="scope:other"),
            "candidate_claim_composite_authority_mismatch",
        ),
        (
            lambda: _event_candidate_execution(
                evidence_window_refs=("window:wrong-target", "window:wrong-baseline")
            ),
            "claim_settlement_window_reference_closure_invalid",
        ),
    ),
)
def test_event_candidate_impact_rejects_authority_drift(
    execution,
    error: str,
) -> None:
    result = execution()
    proposal = _event_candidate_proposal(result)

    with pytest.raises(ClaimSettlementContractError, match=error):
        prepare_claim_settlement(
            result,
            authority_namespace=_namespace(result),
            candidate_proposals=(proposal,),
        )


def test_event_candidate_impact_rejects_causal_interpretation() -> None:
    execution = _event_candidate_execution()
    proposal = _event_candidate_proposal(
        execution,
        causal_interpretation_allowed=True,
    )

    with pytest.raises(
        ClaimSettlementContractError,
        match="candidate_claim_composite_causal_interpretation_forbidden",
    ):
        prepare_claim_settlement(
            execution,
            authority_namespace=_namespace(execution),
            candidate_proposals=(proposal,),
        )


def test_settlement_keeps_many_to_many_evidence_and_scopes_unavailability() -> None:
    execution = _execution_result(
        obligations={
            "change": ("comparative_change", "observed"),
            "segments": (
                "segment_contribution_or_mix_shift",
                "derived",
            ),
            "payment_success": (
                "formula_component_contribution",
                "derived",
            ),
        },
        tasks=(
            _TaskSpec(
                "compare_primary",
                "compare_periods",
                ("change",),
                evidence=(
                    _EvidenceSpec(
                        "observed",
                        "directional",
                        ("comparative_change",),
                        "absolute_change",
                        -120,
                    ),
                ),
            ),
            _TaskSpec(
                "compare_reconciliation",
                "market_health_compare",
                ("change",),
                evidence=(
                    _EvidenceSpec(
                        "observed",
                        "directional",
                        ("comparative_change",),
                        "relative_change",
                        -0.12,
                    ),
                ),
            ),
            _TaskSpec(
                "region",
                "candidate_dimension_screen",
                ("segments",),
                evidence=(
                    _EvidenceSpec(
                        "derived",
                        "candidate_driver",
                        ("segment_contribution_or_mix_shift",),
                        "region_delta",
                        -80,
                        dimension_path=("region", "city"),
                        limitation_refs=("limitation:region-sample",),
                    ),
                ),
            ),
            _TaskSpec(
                "device",
                "candidate_dimension_screen",
                ("segments",),
                evidence=(
                    _EvidenceSpec(
                        "derived",
                        "candidate_driver",
                        ("segment_contribution_or_mix_shift",),
                        "device_delta",
                        -40,
                        dimension_path=("device",),
                    ),
                ),
            ),
            _TaskSpec(
                "payment_success",
                "formula_decompose",
                ("payment_success",),
                status="unavailable",
                limitation_refs=("limitation:payment-success-missing",),
            ),
        ),
    )

    settlement = _settle(execution)
    coverage = {item.obligation_id: item for item in settlement.obligation_coverage}
    obligation_by_kind = {
        item.claim_kind: item for item in execution.plan_revision.claim_obligations
    }

    change = coverage[obligation_by_kind["comparative_change"].obligation_id]
    segment = coverage[
        obligation_by_kind["segment_contribution_or_mix_shift"].obligation_id
    ]
    payment = coverage[
        obligation_by_kind["formula_component_contribution"].obligation_id
    ]
    assert change.status == "satisfied"
    assert len(change.claim_refs) == 1
    assert segment.status == "satisfied"
    assert len(segment.claim_refs) == 2
    assert payment.status == "unavailable"
    assert payment.claim_refs == ()
    assert payment.limitation_refs == ("limitation:payment-success-missing",)

    claim_by_ref = {item.claim_ref: item for item in settlement.accepted_claims}
    change_claim = claim_by_ref[change.claim_refs[0]]
    assert len(change_claim.support_edge_refs) == 2
    assert "limitation:payment-success-missing" not in change_claim.limitation_refs
    claim_key_by_ref = {item.claim_key: item for item in settlement.accepted_claim_keys}
    segment_claims_by_path = {
        claim_key_by_ref[claim.claim_key].dimension_path: claim
        for claim in settlement.accepted_claims
        if claim_key_by_ref[claim.claim_key].claim_kind
        == "segment_contribution_or_mix_shift"
    }
    assert segment_claims_by_path[("region", "city")].limitation_refs == (
        "limitation:region-sample",
    )
    assert segment_claims_by_path[("device",)].limitation_refs == ()
    assert len(settlement.accepted_claims) == 3
    assert len(settlement.accepted_support_edges) == 4
    assert len(settlement.claim_graph.evidence_ceiling_by_ref) == 4

    authority_inputs = AuthorityBundleInputs.create(
        execution_result=execution,
        claim_settlement=settlement,
        recommendations=(),
    )
    bundle = authority_inputs.seal(
        bundle_revision=1,
        supersedes_bundle_ref=None,
        sealed_at="2026-07-21T00:00:00+00:00",
    )
    contexts = build_public_limitation_contexts(
        execution,
        bundle,
        settlement,
        (),
    )
    region_context = contexts["limitation:region-sample"]
    assert tuple(item["dimension_path"] for item in region_context["claims"]) == (
        ("region", "city"),
    )
    assert tuple(item["dimension_path"] for item in region_context["evidence"]) == (
        ("region", "city"),
    )
    assert "outcomes" not in region_context


def test_context_enrichment_evidence_stays_outside_claim_membership() -> None:
    execution = _execution_result(
        obligations={"change": ("comparative_change", "observed")},
        tasks=(
            _TaskSpec(
                "primary_change",
                "compare_periods",
                ("change",),
                evidence=(
                    _EvidenceSpec(
                        "observed",
                        "directional",
                        ("comparative_change",),
                        "absolute_change",
                        12,
                    ),
                ),
            ),
            _TaskSpec(
                "context_pattern",
                "compare_period_phases",
                (),
                evidence=(
                    _EvidenceSpec(
                        "statistical_association",
                        "recurring_pattern",
                        (),
                        "phase_pattern",
                        0.6,
                    ),
                ),
            ),
        ),
    )

    settlement = _settle(execution)
    context_entry = _ledger_by_evidence_ref(execution)["evidence:context_pattern:0"]

    assert len(settlement.accepted_claims) == 1
    assert context_entry.entry_ref not in {
        edge.source_ref for edge in settlement.accepted_support_edges
    }
    assert context_entry.entry_ref not in settlement.claim_graph.evidence_ceiling_by_ref
    assert all(
        context_entry.entry_ref not in item.non_claim_support_evidence_refs
        for item in settlement.checkpoint.obligation_basis
    )


def test_context_enrichment_cannot_advertise_unbound_claim_support() -> None:
    execution = _execution_result(
        obligations={"change": ("comparative_change", "observed")},
        tasks=(
            _TaskSpec(
                "primary_change",
                "compare_periods",
                ("change",),
                evidence=(
                    _EvidenceSpec(
                        "observed",
                        "directional",
                        ("comparative_change",),
                        "absolute_change",
                        12,
                    ),
                ),
            ),
            _TaskSpec(
                "unbound_claim",
                "compare_period_phases",
                (),
                evidence=(
                    _EvidenceSpec(
                        "statistical_association",
                        "recurring_pattern",
                        ("comparative_change",),
                        "phase_pattern",
                        0.6,
                    ),
                ),
            ),
        ),
    )

    with pytest.raises(
        ClaimSettlementContractError,
        match="claim_settlement_unbound_evidence_claim_support_invalid",
    ):
        prepare_claim_settlement(
            execution,
            authority_namespace=_namespace(execution),
        )


@pytest.mark.parametrize(
    (
        "capability_id",
        "claim_kind",
        "minimum_evidence",
        "evidence_kind",
        "maximum_claim_strength",
        "expected_claim_class",
    ),
    (
        (
            "segment_contribution",
            "segment_contribution_or_mix_shift",
            "derived",
            "derived",
            "candidate_driver",
            "dimension_localization",
        ),
        (
            "candidate_dimension_screen",
            "segment_contribution_or_mix_shift",
            "statistical_association",
            "statistical_association",
            "candidate_driver",
            "statistical_association",
        ),
        (
            "user_mix_contribution",
            "segment_contribution_or_mix_shift",
            "derived",
            "derived",
            "candidate_driver",
            "dimension_localization",
        ),
        (
            "formula_decompose",
            "comparative_change",
            "derived",
            "derived",
            "candidate_driver",
            "dimension_localization",
        ),
        (
            "segment_contribution",
            "segment_contribution_or_mix_shift",
            "derived",
            "derived",
            "quantified_contribution",
            "accounting_identity_contribution",
        ),
        (
            "compare_periods",
            "comparative_change",
            "statistical_association",
            "statistical_association",
            "directional",
            "statistical_association",
        ),
        (
            "outlier_scan",
            "comparative_change",
            "statistical_association",
            "statistical_association",
            "anomaly_candidate",
            "statistical_association",
        ),
        (
            "outlier_scan",
            "external_shock_candidate_or_anomaly",
            "statistical_association",
            "statistical_association",
            "anomaly_candidate",
            "statistical_association",
        ),
        (
            "outlier_contribution",
            "external_shock_candidate_or_anomaly",
            "derived",
            "derived",
            "candidate_driver",
            "dimension_localization",
        ),
        (
            "rolling_window_compare",
            "baseline_stability",
            "statistical_association",
            "statistical_association",
            "recurring_pattern",
            "statistical_association",
        ),
        (
            "metric_timeseries",
            "recurring_pattern_existence",
            "observed",
            "observed",
            "directional",
            "observed_fact",
        ),
        (
            "data_quality_profile",
            "contract_coverage_and_trust_boundary",
            "boundary",
            "boundary",
            "trust_boundary",
            "boundary",
        ),
        (
            "formula_decompose",
            "formula_component_contribution",
            "derived",
            "derived",
            "quantified_contribution",
            "accounting_identity_contribution",
        ),
        (
            "rolling_window_compare",
            "comparative_change",
            "statistical_association",
            "statistical_association",
            "candidate_driver",
            "statistical_association",
        ),
        (
            "compare_period_phases",
            "recurring_pattern_existence",
            "statistical_association",
            "statistical_association",
            "recurring_pattern",
            "statistical_association",
        ),
    ),
)
def test_live_binding_matrix_preserves_declared_epistemic_class(
    capability_id: str,
    claim_kind: str,
    minimum_evidence: str,
    evidence_kind: str,
    maximum_claim_strength: str,
    expected_claim_class: str,
) -> None:
    execution = _execution_result(
        obligations={"claim": (claim_kind, minimum_evidence)},
        tasks=(
            _TaskSpec(
                "claim",
                capability_id,
                ("claim",),
                evidence=(
                    _EvidenceSpec(
                        evidence_kind,
                        maximum_claim_strength,
                        (claim_kind,),
                        "live_binding_observation",
                        1,
                        dimension_path=(
                            ("user_mix",)
                            if capability_id == "user_mix_contribution"
                            else ()
                        ),
                    ),
                ),
            ),
        ),
    )

    settlement = _settle(execution)

    assert len(settlement.accepted_claims) == 1
    assert settlement.accepted_claims[0].claim_class == expected_claim_class
    assert settlement.accepted_claims[0].publication_ceiling.strength == (
        maximum_claim_strength
    )


def test_partial_evidence_is_local_basis_while_complete_evidence_supports_claim() -> (
    None
):
    execution = _execution_result(
        obligations={"payment_formula": ("formula_component_contribution", "derived")},
        tasks=(
            _TaskSpec(
                "complete_formula",
                "formula_decompose",
                ("payment_formula",),
                evidence=(
                    _EvidenceSpec(
                        "derived",
                        "accounting_contribution",
                        ("formula_component_contribution",),
                        "reconciled_formula_delta",
                        -12,
                    ),
                ),
            ),
            _TaskSpec(
                "partial_payment_process",
                "formula_decompose",
                ("payment_formula",),
                evidence=(
                    _EvidenceSpec(
                        "derived",
                        "accounting_contribution",
                        ("formula_component_contribution",),
                        "payment_process_delta",
                        -3,
                        data_contract_state="partial",
                        limitation_refs=(
                            "limitation:optional-payment-attempt-missing",
                        ),
                    ),
                ),
            ),
        ),
    )
    settlement = _settle(execution)
    partial_entry = _ledger_by_evidence_ref(execution)[
        "evidence:partial_payment_process:0"
    ]

    assert len(settlement.accepted_claims) == 1
    assert len(settlement.accepted_support_edges) == 1
    assert settlement.accepted_support_edges[0].source_ref != partial_entry.entry_ref
    assert settlement.obligation_coverage[0].status == "mixed"
    assert "limitation:optional-payment-attempt-missing" in (
        settlement.obligation_coverage[0].limitation_refs
    )
    assert any(
        ref.startswith("limitation:claim-strength-gap:sha256:")
        for ref in settlement.obligation_coverage[0].limitation_refs
    )
    assert settlement.checkpoint.obligation_basis[
        0
    ].non_claim_support_evidence_refs == (partial_entry.entry_ref,)


def test_epistemic_classes_form_distinct_order_invariant_claim_keys() -> None:
    tasks = (
        _TaskSpec(
            "derived_segment",
            "segment_contribution",
            ("segments",),
            evidence=(
                _EvidenceSpec(
                    "derived",
                    "candidate_driver",
                    ("segment_contribution_or_mix_shift",),
                    "derived_delta",
                    -8,
                    dimension_path=("region",),
                ),
            ),
        ),
        _TaskSpec(
            "statistical_segment",
            "candidate_dimension_screen",
            ("segments",),
            evidence=(
                _EvidenceSpec(
                    "statistical_association",
                    "candidate_driver",
                    ("segment_contribution_or_mix_shift",),
                    "association_score",
                    0.8,
                    dimension_path=("region",),
                ),
            ),
        ),
    )

    execution = _execution_result(
        obligations={
            "segments": (
                "segment_contribution_or_mix_shift",
                ("derived", "statistical_association"),
            )
        },
        tasks=tasks,
    )
    namespace = _namespace(execution)
    obligations = {
        item.obligation_id: item for item in execution.plan_revision.claim_obligations
    }
    evidence_by_ref, _ = evidence_records(execution, obligations=obligations)
    first_proposed = direct_claim_proposals(
        execution,
        authority_namespace=namespace,
        obligations=obligations,
        evidence_by_ref=evidence_by_ref,
    )
    reordered_proposed = direct_claim_proposals(
        execution,
        authority_namespace=namespace,
        obligations=obligations,
        evidence_by_ref=dict(reversed(tuple(evidence_by_ref.items()))),
    )
    first = _settle(execution)

    assert {item.claim_class for item in first.accepted_claims} == {
        "dimension_localization",
        "statistical_association",
    }
    assert len({item.claim_key for item in first.accepted_claims}) == 2
    assert first_proposed == reordered_proposed


def test_only_partial_evidence_forms_boundary_authority_without_verified_claim() -> (
    None
):
    execution = _execution_result(
        obligations={"payment_formula": ("formula_component_contribution", "derived")},
        tasks=(
            _TaskSpec(
                "partial_payment_process",
                "formula_decompose",
                ("payment_formula",),
                evidence=(
                    _EvidenceSpec(
                        "derived",
                        "accounting_contribution",
                        ("formula_component_contribution",),
                        "payment_process_delta",
                        -3,
                        data_contract_state="partial",
                        limitation_refs=(
                            "limitation:optional-payment-attempt-missing",
                        ),
                    ),
                ),
            ),
        ),
    )

    settlement = _settle(execution)

    assert settlement.accepted_claims == ()
    assert settlement.claim_graph.authority_mode == "boundary_only"
    assert settlement.obligation_coverage[0].status == "unavailable"
    assert settlement.obligation_coverage[0].claim_refs == ()
    assert settlement.obligation_coverage[0].limitation_refs == (
        "limitation:optional-payment-attempt-missing",
    )


def test_legal_context_evidence_without_minimum_authority_forms_typed_boundary() -> (
    None
):
    execution = _execution_result(
        obligations={"change": ("comparative_change", "observed")},
        tasks=(
            _TaskSpec(
                "rolling_context",
                "rolling_window_compare",
                ("change",),
                evidence=(
                    _EvidenceSpec(
                        "statistical_association",
                        "directional",
                        ("comparative_change",),
                        "rolling_direction",
                        -1,
                    ),
                ),
            ),
        ),
    )

    settlement = _settle(execution)

    assert settlement.claim_graph.authority_mode == "boundary_only"
    assert settlement.accepted_claims == ()
    assert settlement.obligation_coverage[0].status == "unavailable"
    assert (
        settlement.obligation_coverage[0]
        .limitation_refs[0]
        .startswith("limitation:minimum-evidence-unsatisfied:sha256:")
    )
    assert settlement.checkpoint.obligation_basis[0].non_claim_support_evidence_refs


@pytest.mark.parametrize(
    ("evidence_kind", "claim_kind", "strength"),
    (
        ("statistical_association", "segment_contribution_or_mix_shift", "medium"),
        ("derived", "comparative_change", "quantified_contribution"),
        ("observed", "comparative_change", "high"),
    ),
)
def test_unknown_or_cross_class_strength_contract_fails_closed(
    evidence_kind: str,
    claim_kind: str,
    strength: str,
) -> None:
    minimum = (
        "statistical_association"
        if evidence_kind == "statistical_association"
        else "observed"
    )
    execution = _execution_result(
        obligations={"claim": (claim_kind, minimum)},
        tasks=(
            _TaskSpec(
                "claim",
                "claim_capability",
                ("claim",),
                evidence=(
                    _EvidenceSpec(
                        evidence_kind,
                        strength,
                        (claim_kind,),
                        "value",
                        1,
                    ),
                ),
            ),
        ),
    )

    with pytest.raises(
        ClaimSettlementContractError,
        match="claim_settlement_ceiling_compatibility_missing",
    ):
        _settle(execution)


def test_candidate_proposal_remains_candidate_and_semantic_veto_only_removes_it() -> (
    None
):
    execution = _execution_result(
        obligations={
            "change": ("comparative_change", "observed"),
            "mechanism": ("candidate_mechanism", "observed"),
        },
        tasks=(
            _TaskSpec(
                "compare",
                "compare_periods",
                ("change",),
                evidence=(
                    _EvidenceSpec(
                        "observed",
                        "directional",
                        ("comparative_change",),
                        "absolute_change",
                        -120,
                    ),
                ),
            ),
            _TaskSpec(
                "event",
                "event_evidence",
                ("mechanism",),
                evidence=(
                    _EvidenceSpec(
                        "observed",
                        "candidate_mechanism",
                        ("candidate_mechanism",),
                        "event_overlap_days",
                        3,
                    ),
                ),
            ),
        ),
    )
    namespace = _namespace(execution)
    ledger = _ledger_by_evidence_ref(execution)
    mechanism_obligation = next(
        item
        for item in execution.plan_revision.claim_obligations
        if item.claim_kind == "candidate_mechanism"
    )
    proposal = CandidateClaimProposal.create(
        authority_namespace=namespace,
        proposal_item_ref="planner-proposal-item:promotion-overlap",
        obligation_id=mechanism_obligation.obligation_id,
        subject="promotion_window_overlap",
        factual_payload={
            "mechanism": "promotion timing may explain part of the movement",
            "certainty": "candidate",
        },
        evidence_support=(
            CandidateEvidenceSupport.create(
                authority_namespace=namespace,
                evidence_entry_ref=ledger["evidence:event:0"].entry_ref,
                source_claim_kind="candidate_mechanism",
            ),
        ),
        limitation_refs=("limitation:no-counterfactual",),
    )

    accepted = _settle(
        execution,
        candidate_proposals=(proposal,),
    )
    candidate = next(
        item
        for item in accepted.accepted_claims
        if item.claim_class == "candidate_mechanism"
    )
    assert candidate.publication_ceiling.claim_class == "candidate_mechanism"
    assert candidate.publication_ceiling.strength == "candidate_mechanism"
    assert all(
        item.claim_class != "observed_fact"
        for item in accepted.accepted_claims
        if item.claim_key == candidate.claim_key
    )

    proposed_candidate = next(
        item
        for item in accepted.checkpoint.proposed_claims
        if item.claim_class == "candidate_mechanism"
    )
    vetoed = _settle(
        execution,
        candidate_proposals=(proposal,),
        veto_claim_classes=("candidate_mechanism",),
    )
    assert all(
        item.claim_class != "candidate_mechanism" for item in vetoed.accepted_claims
    )
    assert proposed_candidate.claim_ref in vetoed.verifier_report.rejected_claim_refs
    assert proposed_candidate.claim_ref not in vetoed.claim_graph.claim_refs
    candidate_coverage = next(
        item
        for item in vetoed.obligation_coverage
        if item.obligation_id == mechanism_obligation.obligation_id
    )
    assert candidate_coverage.status == "unavailable"
    assert candidate_coverage.limitation_refs == ("limitation:semantic-scope",)


def test_scenario_boundary_and_observed_claims_keep_distinct_identities() -> None:
    execution = _execution_result(
        obligations={
            "observed": ("comparative_change", "observed"),
            "scenario": ("scenario", "scenario"),
            "boundary": (
                "contract_coverage_and_trust_boundary",
                "boundary",
            ),
        },
        tasks=(
            _TaskSpec(
                "observed",
                "compare_periods",
                ("observed",),
                evidence=(
                    _EvidenceSpec(
                        "observed",
                        "descriptive",
                        ("comparative_change",),
                        "target_value",
                        100,
                    ),
                ),
            ),
            _TaskSpec(
                "scenario",
                "scenario_projection",
                ("scenario",),
                evidence=(
                    _EvidenceSpec(
                        "scenario",
                        "scenario",
                        ("scenario",),
                        "assumed_payment_success",
                        1.0,
                    ),
                ),
            ),
            _TaskSpec(
                "boundary",
                "data_quality_profile",
                ("boundary",),
                evidence=(
                    _EvidenceSpec(
                        "boundary",
                        "trust_boundary",
                        ("contract_coverage_and_trust_boundary",),
                        "covered_row_count",
                        80,
                        data_contract_state="partial",
                        limitation_refs=("limitation:partial-contract",),
                    ),
                ),
            ),
        ),
    )

    settlement = _settle(execution)

    assert {item.claim_class for item in settlement.accepted_claims} == {
        "observed_fact",
        "scenario",
        "boundary",
    }
    assert len({item.claim_key for item in settlement.accepted_claims}) == 3
    assert {
        item.claim_class
        for item in settlement.claim_graph.evidence_ceiling_by_ref.values()
    } == {"observed_fact", "scenario", "boundary"}
    observed_obligation = next(
        item
        for item in execution.plan_revision.claim_obligations
        if item.claim_kind == "comparative_change"
    )
    observed_coverage = next(
        item
        for item in settlement.obligation_coverage
        if item.obligation_id == observed_obligation.obligation_id
    )
    assert observed_coverage.status == "mixed"
    assert observed_coverage.claim_refs
    assert any(
        ref.startswith("limitation:claim-strength-gap:sha256:")
        for ref in observed_coverage.limitation_refs
    )


@pytest.mark.parametrize(
    ("binding", "result_membership", "completeness_membership", "error"),
    (
        (False, True, True, "claim_settlement_binding_ref_missing"),
        (True, False, True, "claim_settlement_result_membership_missing"),
        (True, True, False, "claim_settlement_completeness_membership_missing"),
    ),
)
def test_evidence_authority_membership_is_fail_closed(
    binding: bool,
    result_membership: bool,
    completeness_membership: bool,
    error: str,
) -> None:
    execution = _execution_result(
        obligations={"change": ("comparative_change", "observed")},
        tasks=(
            _TaskSpec(
                "compare",
                "compare_periods",
                ("change",),
                evidence=(
                    _EvidenceSpec(
                        "observed",
                        "directional",
                        ("comparative_change",),
                        "absolute_change",
                        10,
                        binding=binding,
                        result_membership=result_membership,
                        completeness_membership=completeness_membership,
                    ),
                ),
            ),
        ),
    )

    with pytest.raises(ClaimSettlementContractError, match=error):
        _settle(execution)


def test_candidate_proposal_order_cannot_change_settlement_or_bundle_digest() -> None:
    execution = _execution_result(
        obligations={
            "change": ("comparative_change", "observed"),
            "mechanism": ("candidate_mechanism", "observed"),
        },
        tasks=(
            _TaskSpec(
                "change",
                "compare_periods",
                ("change",),
                evidence=(
                    _EvidenceSpec(
                        "observed",
                        "directional",
                        ("comparative_change",),
                        "absolute_change",
                        -1,
                    ),
                ),
            ),
            _TaskSpec(
                "event",
                "event_evidence",
                ("mechanism",),
                evidence=(
                    _EvidenceSpec(
                        "observed",
                        "candidate_mechanism",
                        ("candidate_mechanism",),
                        "event_count",
                        2,
                    ),
                ),
            ),
        ),
    )
    candidate_obligation = next(
        item
        for item in execution.plan_revision.claim_obligations
        if item.claim_kind == "candidate_mechanism"
    )
    namespace = _namespace(execution)
    event_entry = _ledger_by_evidence_ref(execution)["evidence:event:0"]
    support = (
        CandidateEvidenceSupport.create(
            authority_namespace=namespace,
            evidence_entry_ref=event_entry.entry_ref,
            source_claim_kind="candidate_mechanism",
        ),
    )
    first = CandidateClaimProposal.create(
        authority_namespace=namespace,
        proposal_item_ref="proposal-item:first",
        obligation_id=candidate_obligation.obligation_id,
        subject="first_candidate",
        factual_payload={"candidate": "first"},
        evidence_support=support,
        limitation_refs=(),
    )
    second = CandidateClaimProposal.create(
        authority_namespace=namespace,
        proposal_item_ref="proposal-item:second",
        obligation_id=candidate_obligation.obligation_id,
        subject="second_candidate",
        factual_payload={"candidate": "second"},
        evidence_support=support,
        limitation_refs=(),
    )

    forward = _settle(execution, candidate_proposals=(first, second))
    reversed_settlement = _settle(execution, candidate_proposals=(second, first))

    assert forward.content_digest == reversed_settlement.content_digest
    assert (
        forward.claim_graph.content_digest
        == reversed_settlement.claim_graph.content_digest
    )
    first_inputs = AuthorityBundleInputs.create(
        execution_result=execution,
        claim_settlement=forward,
        recommendations=(),
    )
    second_inputs = AuthorityBundleInputs.create(
        execution_result=execution,
        claim_settlement=reversed_settlement,
        recommendations=(),
    )
    first_bundle = first_inputs.seal(
        bundle_revision=1,
        supersedes_bundle_ref=None,
        sealed_at="2026-07-18T09:00:00Z",
    )
    second_bundle = second_inputs.seal(
        bundle_revision=1,
        supersedes_bundle_ref=None,
        sealed_at="2026-07-18T10:00:00Z",
    )
    assert first_bundle.bundle_digest == second_bundle.bundle_digest
    assert first_bundle.evidence_refs == tuple(
        forward.claim_graph.evidence_ceiling_by_ref
    )
    assert not (
        set(first_bundle.verified_claim_refs)
        & set(forward.verifier_report.rejected_claim_refs)
    )


def test_claim_key_survives_factual_content_revision() -> None:
    def execution(value: int) -> AuthoritativeExecutionResult:
        return _execution_result(
            obligations={"change": ("comparative_change", "observed")},
            tasks=(
                _TaskSpec(
                    "compare",
                    "compare_periods",
                    ("change",),
                    evidence=(
                        _EvidenceSpec(
                            "observed",
                            "directional",
                            ("comparative_change",),
                            "absolute_change",
                            value,
                        ),
                    ),
                ),
            ),
        )

    first = _settle(execution(10))
    revised = _settle(execution(12))

    assert first.accepted_claims[0].claim_key == revised.accepted_claims[0].claim_key
    assert first.accepted_claims[0].claim_ref != revised.accepted_claims[0].claim_ref


def test_checkpoint_and_settlement_roundtrip_are_non_lossy() -> None:
    execution = _execution_result(
        obligations={"change": ("comparative_change", "observed")},
        tasks=(
            _TaskSpec(
                "compare",
                "compare_periods",
                ("change",),
                evidence=(
                    _EvidenceSpec(
                        "observed",
                        "directional",
                        ("comparative_change",),
                        "absolute_change",
                        12,
                    ),
                ),
            ),
        ),
    )
    namespace = _namespace(execution)
    checkpoint = prepare_claim_settlement(
        execution,
        authority_namespace=namespace,
    )
    assert ClaimSettlementCheckpoint.from_dict(checkpoint.to_dict()) == checkpoint

    attempt = checkpoint.verification_attempt(
        provider_ref="provider:deepseek",
        model_ref="deepseek-chat",
        input_digest=canonical_digest(checkpoint.to_dict()),
        attempt_number=1,
        raw_provider_response_ref=("restricted-provider-response:claim-settlement"),
        raw_provider_response_digest=canonical_digest(
            {"checkpoint_ref": checkpoint.checkpoint_ref, "response": "accepted"}
        ),
    )
    decisions = tuple(
        SemanticVerificationDecision.create(
            authority_namespace=namespace,
            verification_attempt=attempt,
            subject_ref=claim.claim_ref,
            disposition="accepted",
            veto_basis=None,
            reason_code=None,
            limitation_refs=(),
        )
        for claim in checkpoint.proposed_claims
    )
    settlement = settle_claim_checkpoint(
        checkpoint,
        verification_attempt=attempt,
        verification_decisions=decisions,
    )
    assert ClaimSettlement.from_dict(settlement.to_dict()) == settlement

    forged = checkpoint.to_dict()
    assert (
        "evidence_observations" not in forged["proposed_claims"][0]["factual_payload"]
    )
    forged["proposed_claims"][0]["factual_payload"]["claim_kind"] = "forged"
    with pytest.raises((ClaimSettlementContractError, ValueError)):
        ClaimSettlementCheckpoint.from_dict(forged)


def test_claims_cannot_settle_without_explicit_semantic_attempt_and_decisions() -> None:
    execution = _execution_result(
        obligations={"change": ("comparative_change", "observed")},
        tasks=(
            _TaskSpec(
                "compare",
                "compare_periods",
                ("change",),
                evidence=(
                    _EvidenceSpec(
                        "observed",
                        "directional",
                        ("comparative_change",),
                        "absolute_change",
                        1,
                    ),
                ),
            ),
        ),
    )
    checkpoint = prepare_claim_settlement(
        execution,
        authority_namespace=_namespace(execution),
    )

    with pytest.raises(
        ClaimSettlementContractError,
        match="claim_settlement_verification_attempt_required",
    ):
        settle_claim_checkpoint(
            checkpoint,
            verification_attempt=None,
            verification_decisions=(),
        )


def test_all_unavailable_execution_settles_to_boundary_only_authority() -> None:
    execution = _execution_result(
        obligations={"payment_success": ("comparative_change", "observed")},
        tasks=(
            _TaskSpec(
                "payment_success",
                "compare_periods",
                ("payment_success",),
                status="unavailable",
                limitation_refs=("limitation:source-unavailable",),
            ),
        ),
    )

    settlement = _settle(execution)
    assert settlement.claim_graph.authority_mode == "boundary_only"
    assert settlement.accepted_claims == ()
    assert settlement.obligation_coverage[0].status == "unavailable"
    assert ClaimSettlement.from_dict(settlement.to_dict()) == settlement

    inputs = AuthorityBundleInputs.create(
        execution_result=execution,
        claim_settlement=settlement,
        recommendations=(),
    )
    bundle = inputs.seal(
        bundle_revision=1,
        supersedes_bundle_ref=None,
        sealed_at="2026-07-18T12:00:00Z",
    )
    assert bundle.verified_claim_refs == ()
    assert bundle.authority_mode == "boundary_only"
    assert bundle.obligation_coverage_refs == tuple(
        item.coverage_ref for item in settlement.obligation_coverage
    )
    assert bundle.claim_graph_ref == settlement.claim_graph.claim_graph_ref
    assert bundle.claim_settlement_ref == settlement.settlement_ref
    assert bundle.execution_result_ref == execution.authoritative_execution_result_ref


def test_all_semantically_vetoed_claims_settle_to_explicit_boundary_authority() -> None:
    execution = _execution_result(
        obligations={"change": ("comparative_change", "observed")},
        tasks=(
            _TaskSpec(
                "compare",
                "compare_periods",
                ("change",),
                evidence=(
                    _EvidenceSpec(
                        "observed",
                        "directional",
                        ("comparative_change",),
                        "absolute_change",
                        1,
                    ),
                ),
            ),
        ),
    )

    settlement = _settle(execution, veto_claim_classes=("observed_fact",))

    assert settlement.accepted_claims == ()
    assert settlement.claim_graph.authority_mode == "boundary_only"
    assert settlement.verifier_report.verification_mode == "semantic_verifier"
    assert settlement.verifier_report.rejected_claim_refs
    assert settlement.obligation_coverage[0].status == "unavailable"
    assert settlement.claim_graph.limitation_refs == ("limitation:semantic-scope",)
    assert ClaimSettlement.from_dict(settlement.to_dict()) == settlement


def test_all_vetoes_without_known_limitation_create_content_addressed_boundary() -> (
    None
):
    execution = _execution_result(
        obligations={"change": ("comparative_change", "observed")},
        tasks=(
            _TaskSpec(
                "compare",
                "compare_periods",
                ("change",),
                evidence=(
                    _EvidenceSpec(
                        "observed",
                        "directional",
                        ("comparative_change",),
                        "absolute_change",
                        1,
                    ),
                ),
            ),
        ),
    )
    namespace = _namespace(execution)
    checkpoint = prepare_claim_settlement(
        execution,
        authority_namespace=namespace,
    )
    attempt = checkpoint.verification_attempt(
        provider_ref="provider:deepseek",
        model_ref="deepseek-chat",
        input_digest=canonical_digest(checkpoint.to_dict()),
        attempt_number=1,
        raw_provider_response_ref="restricted-provider-response:all-vetoed",
        raw_provider_response_digest=canonical_digest({"response": "vetoed"}),
    )
    decisions = tuple(
        SemanticVerificationDecision.create(
            authority_namespace=namespace,
            verification_attempt=attempt,
            subject_ref=claim.claim_ref,
            disposition="vetoed",
            veto_basis="factual_support_invalid",
            reason_code="factual_mismatch",
            limitation_refs=(),
        )
        for claim in checkpoint.proposed_claims
    )

    settlement = settle_claim_checkpoint(
        checkpoint,
        verification_attempt=attempt,
        verification_decisions=decisions,
    )

    coverage = settlement.obligation_coverage[0]
    expected_body = {
        "obligation_id": coverage.obligation_id,
        "proposed_claim_refs": checkpoint.obligation_basis[0].proposed_claim_refs,
        "verifier_decisions": tuple(
            {
                "subject_ref": item.subject_ref,
                "verification_decision_ref": item.verification_decision_ref,
                "reason_code": item.reason_code,
            }
            for item in decisions
        ),
    }
    expected_limitation_ref = (
        "limitation:semantic-verifier-boundary:sha256:"
        + canonical_digest(expected_body)
    )
    assert coverage.status == "unavailable"
    assert coverage.claim_refs == ()
    assert coverage.limitation_refs == (expected_limitation_ref,)
    assert settlement.claim_graph.authority_mode == "boundary_only"

    inputs = AuthorityBundleInputs.create(
        execution_result=execution,
        claim_settlement=settlement,
        recommendations=(),
    )
    bundle = inputs.seal(
        bundle_revision=1,
        supersedes_bundle_ref=None,
        sealed_at="2026-07-18T12:00:00Z",
    )
    contexts = build_public_limitation_contexts(
        execution,
        bundle,
        settlement,
        (),
    )
    assert tuple(contexts) == (expected_limitation_ref,)
    assert contexts[expected_limitation_ref]["obligations"] == (
        {
            "obligation_id": coverage.obligation_id,
            "status": "unavailable",
            "claim_kind": "comparative_change",
            "role": "user_required",
        },
    )


def test_authority_bundle_rejects_unresolved_required_obligation() -> None:
    execution = _execution_result(
        obligations={"change": ("comparative_change", "observed")},
        tasks=(
            _TaskSpec(
                "compare",
                "compare_periods",
                ("change",),
                evidence=(
                    _EvidenceSpec(
                        "observed",
                        "directional",
                        ("comparative_change",),
                        "absolute_change",
                        1,
                    ),
                ),
            ),
        ),
    )
    namespace = _namespace(execution)
    checkpoint = prepare_claim_settlement(
        execution,
        authority_namespace=namespace,
    )
    attempt = checkpoint.verification_attempt(
        provider_ref="provider:deepseek",
        model_ref="deepseek-chat",
        input_digest=canonical_digest(checkpoint.to_dict()),
        attempt_number=1,
        raw_provider_response_ref="restricted-provider-response:all-vetoed",
        raw_provider_response_digest=canonical_digest({"response": "vetoed"}),
    )
    decisions = tuple(
        SemanticVerificationDecision.create(
            authority_namespace=namespace,
            verification_attempt=attempt,
            subject_ref=claim.claim_ref,
            disposition="vetoed",
            veto_basis="factual_support_invalid",
            reason_code="factual_mismatch",
            limitation_refs=(),
        )
        for claim in checkpoint.proposed_claims
    )
    settled = settle_claim_checkpoint(
        checkpoint,
        verification_attempt=attempt,
        verification_decisions=decisions,
    )
    unresolved = ObligationCoverage.create(
        authority_namespace=namespace,
        verifier_report=settled.verifier_report,
        obligation_id=settled.obligation_coverage[0].obligation_id,
        status="unresolved",
        claim_refs=(),
        limitation_refs=(),
    )
    unresolved_graph = ClaimGraph.create(
        authority_namespace=namespace,
        authority_mode="boundary_only",
        claim_keys=(),
        claims=(),
        support_edges=(),
        obligation_coverage=(unresolved,),
        verifier_report=settled.verifier_report,
        evidence_ceiling_by_ref={},
        assumption_refs=(),
        limitation_refs=(),
    )
    unresolved_settlement = create_settlement(
        checkpoint=checkpoint,
        accepted_claim_keys=(),
        accepted_claims=(),
        accepted_support_edges=(),
        obligation_coverage=(unresolved,),
        verifier_report=settled.verifier_report,
        claim_graph=unresolved_graph,
    )

    with pytest.raises(
        ClaimSettlementContractError,
        match=(
            "authority_bundle_inputs_required_obligation_publication_closure_invalid"
        ),
    ):
        AuthorityBundleInputs.create(
            execution_result=execution,
            claim_settlement=unresolved_settlement,
            recommendations=(),
        )


def test_authority_bundle_allows_unresolved_auxiliary_obligation() -> None:
    execution = _execution_result(
        obligations={
            "change": ("comparative_change", "observed"),
            "mechanism": ("candidate_mechanism", "observed"),
        },
        tasks=(
            _TaskSpec(
                "compare",
                "compare_periods",
                ("change",),
                evidence=(
                    _EvidenceSpec(
                        "observed",
                        "directional",
                        ("comparative_change",),
                        "absolute_change",
                        1,
                    ),
                ),
            ),
            _TaskSpec(
                "event",
                "event_evidence",
                ("mechanism",),
                evidence=(
                    _EvidenceSpec(
                        "observed",
                        "candidate_mechanism",
                        ("candidate_mechanism",),
                        "event_overlap_days",
                        3,
                    ),
                ),
            ),
        ),
    )
    settled = _settle(execution)
    namespace = settled.authority_namespace
    obligation_by_id = {
        item.obligation_id: item for item in execution.plan_revision.claim_obligations
    }
    auxiliary = next(
        item
        for item in settled.obligation_coverage
        if obligation_by_id[item.obligation_id].role == "analyst_auxiliary"
    )
    unresolved_auxiliary = ObligationCoverage.create(
        authority_namespace=namespace,
        verifier_report=settled.verifier_report,
        obligation_id=auxiliary.obligation_id,
        status="unresolved",
        claim_refs=(),
        limitation_refs=(),
    )
    coverage = tuple(
        unresolved_auxiliary if item.obligation_id == auxiliary.obligation_id else item
        for item in settled.obligation_coverage
    )
    limitation_refs = tuple(
        sorted(
            {
                *(
                    ref
                    for item in settled.accepted_claims
                    for ref in item.limitation_refs
                ),
                *(
                    ref
                    for item in settled.accepted_support_edges
                    for ref in item.limitation_refs
                ),
                *(ref for item in coverage for ref in item.limitation_refs),
            }
        )
    )
    graph = ClaimGraph.create(
        authority_namespace=namespace,
        authority_mode="claim_bearing",
        claim_keys=settled.accepted_claim_keys,
        claims=settled.accepted_claims,
        support_edges=settled.accepted_support_edges,
        obligation_coverage=coverage,
        verifier_report=settled.verifier_report,
        evidence_ceiling_by_ref=settled.claim_graph.evidence_ceiling_by_ref,
        assumption_refs=settled.claim_graph.assumption_refs,
        limitation_refs=limitation_refs,
    )
    settlement_with_open_auxiliary = create_settlement(
        checkpoint=settled.checkpoint,
        accepted_claim_keys=settled.accepted_claim_keys,
        accepted_claims=settled.accepted_claims,
        accepted_support_edges=settled.accepted_support_edges,
        obligation_coverage=coverage,
        verifier_report=settled.verifier_report,
        claim_graph=graph,
    )

    inputs = AuthorityBundleInputs.create(
        execution_result=execution,
        claim_settlement=settlement_with_open_auxiliary,
        recommendations=(),
    )
    assert inputs.claim_settlement == settlement_with_open_auxiliary
    bundle = inputs.seal(
        bundle_revision=1,
        supersedes_bundle_ref=None,
        sealed_at="2026-07-18T12:00:00Z",
    )
    assert bundle.required_obligation_ids == tuple(
        sorted(
            item.obligation_id
            for item in execution.plan_revision.claim_obligations
            if item.role == "user_required"
        )
    )
    assert auxiliary.obligation_id not in bundle.required_obligation_ids

    auxiliary_limitation_ref = "limitation:auxiliary-method-unavailable"
    limited_auxiliary = ObligationCoverage.create(
        authority_namespace=namespace,
        verifier_report=settled.verifier_report,
        obligation_id=auxiliary.obligation_id,
        status="unavailable",
        claim_refs=(),
        limitation_refs=(auxiliary_limitation_ref,),
    )
    limited_coverage = tuple(
        limited_auxiliary if item.obligation_id == auxiliary.obligation_id else item
        for item in settled.obligation_coverage
    )
    limited_graph_refs = tuple(
        sorted(
            {
                *(
                    ref
                    for item in settled.accepted_claims
                    for ref in item.limitation_refs
                ),
                *(
                    ref
                    for item in settled.accepted_support_edges
                    for ref in item.limitation_refs
                ),
                *(ref for item in limited_coverage for ref in item.limitation_refs),
            }
        )
    )
    limited_graph = ClaimGraph.create(
        authority_namespace=namespace,
        authority_mode="claim_bearing",
        claim_keys=settled.accepted_claim_keys,
        claims=settled.accepted_claims,
        support_edges=settled.accepted_support_edges,
        obligation_coverage=limited_coverage,
        verifier_report=settled.verifier_report,
        evidence_ceiling_by_ref=settled.claim_graph.evidence_ceiling_by_ref,
        assumption_refs=settled.claim_graph.assumption_refs,
        limitation_refs=limited_graph_refs,
    )
    settlement_with_limited_auxiliary = create_settlement(
        checkpoint=settled.checkpoint,
        accepted_claim_keys=settled.accepted_claim_keys,
        accepted_claims=settled.accepted_claims,
        accepted_support_edges=settled.accepted_support_edges,
        obligation_coverage=limited_coverage,
        verifier_report=settled.verifier_report,
        claim_graph=limited_graph,
    )
    limited_inputs = AuthorityBundleInputs.create(
        execution_result=execution,
        claim_settlement=settlement_with_limited_auxiliary,
        recommendations=(),
    )
    assert auxiliary_limitation_ref in limited_graph.limitation_refs
    assert auxiliary_limitation_ref not in limited_inputs.limitation_refs


def test_no_proposal_or_evidence_creates_minimum_evidence_boundary_at_checkpoint() -> (
    None
):
    execution = _execution_result(
        obligations={"change": ("comparative_change", "observed")},
        tasks=(
            _TaskSpec(
                "compare_without_evidence",
                "compare_periods",
                ("change",),
                status="unavailable",
            ),
        ),
    )
    namespace = _namespace(execution)
    checkpoint = prepare_claim_settlement(
        execution,
        authority_namespace=namespace,
    )

    basis = checkpoint.obligation_basis[0]
    obligation = execution.plan_revision.claim_obligations[0]
    expected_body = {
        "obligation_id": obligation.obligation_id,
        "evidence_requirement": obligation.evidence_requirement.to_dict(),
        "proposed_claim_refs": (),
        "non_claim_support_evidence_refs": (),
    }
    expected_limitation_ref = (
        "limitation:minimum-evidence-unsatisfied:sha256:"
        + canonical_digest(expected_body)
    )
    assert checkpoint.proposed_claims == ()
    assert basis.unavailable_limitation_refs == (expected_limitation_ref,)

    settlement = settle_claim_checkpoint(
        checkpoint,
        verification_attempt=None,
        verification_decisions=(),
    )
    assert settlement.claim_graph.authority_mode == "boundary_only"
    assert settlement.obligation_coverage[0].status == "unavailable"
    assert settlement.obligation_coverage[0].limitation_refs == (
        expected_limitation_ref,
    )


def test_shared_authority_failure_blocks_checkpoint_and_bundle_seal() -> None:
    failed_execution = _execution_result(
        obligations={"change": ("comparative_change", "observed")},
        tasks=(
            _TaskSpec(
                "compare",
                "compare_periods",
                ("change",),
                status="integrity_failed",
                limitation_refs=("limitation:shared-release-invalid",),
                failure_integrity_level="shared_authority",
            ),
        ),
    )

    with pytest.raises(
        ClaimSettlementContractError,
        match="claim_settlement_shared_authority_failure",
    ):
        prepare_claim_settlement(
            failed_execution,
            authority_namespace=_namespace(failed_execution),
        )


def test_bundle_manifest_seals_execution_settlement_graph_refs_and_digests() -> None:
    execution = _execution_result(
        obligations={"change": ("comparative_change", "observed")},
        tasks=(
            _TaskSpec(
                "compare",
                "compare_periods",
                ("change",),
                evidence=(
                    _EvidenceSpec(
                        "observed",
                        "directional",
                        ("comparative_change",),
                        "absolute_change",
                        5,
                    ),
                ),
            ),
        ),
    )
    settlement = _settle(execution)
    inputs = AuthorityBundleInputs.create(
        execution_result=execution,
        claim_settlement=settlement,
        recommendations=(),
    )
    assert AuthorityBundleInputs.from_dict(inputs.to_dict()) == inputs
    bundle = inputs.seal(
        bundle_revision=1,
        supersedes_bundle_ref=None,
        sealed_at="2026-07-18T12:00:00Z",
    )

    assert bundle.execution_result_ref == execution.authoritative_execution_result_ref
    assert bundle.execution_result_digest == execution.content_digest
    assert bundle.claim_settlement_ref == settlement.settlement_ref
    assert bundle.claim_settlement_digest == settlement.content_digest
    assert bundle.claim_graph_ref == settlement.claim_graph.claim_graph_ref
    assert bundle.claim_graph_digest == settlement.claim_graph.content_digest
    assert bundle.authority_mode == settlement.claim_graph.authority_mode
    assert bundle.required_obligation_ids == tuple(
        sorted(
            item.obligation_id
            for item in execution.plan_revision.claim_obligations
            if item.role == "user_required"
        )
    )
    assert bundle.obligation_coverage_refs == tuple(
        item.coverage_ref for item in settlement.obligation_coverage
    )
    assert (
        AuthorityBundle.from_dict(bundle.to_dict(), authority_inputs=inputs) == bundle
    )
    tampered_bundle = bundle.to_dict()
    tampered_bundle["required_obligation_ids"] = []
    with pytest.raises(
        ClaimAuthorityContractError,
        match="authority_bundle_integrity_invalid",
    ):
        AuthorityBundle.from_dict(tampered_bundle, authority_inputs=inputs)

    forged_inputs = replace(
        inputs,
        claim_graph=replace(
            inputs.claim_graph,
            limitation_refs=("limitation:forged",),
        ),
    )
    with pytest.raises(ValueError, match="authority_bundle_inputs_invalid"):
        forged_inputs.seal(
            bundle_revision=1,
            supersedes_bundle_ref=None,
            sealed_at="2026-07-18T12:00:00Z",
        )


def test_recommendation_requires_independent_semantic_attempt_and_decision() -> None:
    execution = _execution_result(
        obligations={"change": ("comparative_change", "observed")},
        tasks=(
            _TaskSpec(
                "compare",
                "compare_periods",
                ("change",),
                evidence=(
                    _EvidenceSpec(
                        "observed",
                        "directional",
                        ("comparative_change",),
                        "absolute_change",
                        8,
                    ),
                ),
            ),
        ),
    )
    namespace = _namespace(execution)
    settlement = _settle(execution)
    supporting_claim_ref = settlement.accepted_claims[0].claim_ref
    action = "Run a controlled experiment."
    expected_value = "Resolve whether the pattern is actionable."
    commitments = (
        RecommendationCommitment.create(
            authority_namespace=namespace,
            commitment_kind="action",
            text=action,
            supporting_claim_refs=(supporting_claim_ref,),
            diagnostic_mode=None,
            action_domain="business_operation",
            action_stage="experiment",
            expected_value_kind=None,
            expected_value_mode=None,
        ),
        RecommendationCommitment.create(
            authority_namespace=namespace,
            commitment_kind="expected_outcome",
            text=expected_value,
            supporting_claim_refs=(supporting_claim_ref,),
            diagnostic_mode=None,
            action_domain=None,
            action_stage=None,
            expected_value_kind="information_gain",
            expected_value_mode="expected_effect",
        ),
    )
    proposal = RecommendationProposal.create(
        authority_namespace=namespace,
        claim_settlement=settlement,
        supporting_claim_refs=(supporting_claim_ref,),
        assumption_refs=(),
        risk_refs=(),
        commitment_contract_version=RECOMMENDATION_COMMITMENT_CONTRACT_VERSION,
        commitments=commitments,
        action=action,
        applicable_conditions=("Budget remains approved.",),
        expected_decision_value=expected_value,
    )
    attempt = SemanticVerificationAttempt.create(
        authority_namespace=namespace,
        purpose="recommendation",
        authority_input_ref=settlement.claim_graph.claim_graph_ref,
        authority_input_digest=settlement.claim_graph.content_digest,
        subject_refs=(proposal.recommendation_proposal_ref,),
        provider_ref="provider:deepseek",
        model_ref="deepseek-chat",
        input_digest=canonical_digest(proposal.to_dict()),
        attempt_number=1,
        raw_provider_response_ref=("restricted-provider-response:recommendation"),
        raw_provider_response_digest=canonical_digest(
            {
                "proposal_ref": proposal.recommendation_proposal_ref,
                "response": "accepted",
            }
        ),
    )
    accepted_decision = SemanticVerificationDecision.create(
        authority_namespace=namespace,
        verification_attempt=attempt,
        subject_ref=proposal.recommendation_proposal_ref,
        disposition="accepted",
        veto_basis=None,
        reason_code=None,
        limitation_refs=(),
    )
    recommendation = RecommendationRecord.verify(
        authority_namespace=namespace,
        proposal=proposal,
        verification_attempt=attempt,
        verification_decision=accepted_decision,
        claim_settlement=settlement,
    )
    assert (
        RecommendationRecord.from_dict(
            recommendation.to_dict(),
            authority_namespace=namespace,
            claim_settlement=settlement,
        )
        == recommendation
    )

    inputs = AuthorityBundleInputs.create(
        execution_result=execution,
        claim_settlement=settlement,
        recommendations=(recommendation,),
    )
    assert inputs.recommendations == (recommendation,)

    veto_decision = SemanticVerificationDecision.create(
        authority_namespace=namespace,
        verification_attempt=attempt,
        subject_ref=proposal.recommendation_proposal_ref,
        disposition="vetoed",
        veto_basis="recommendation_support_invalid",
        reason_code="unsupported_action_scope",
        limitation_refs=(),
    )
    with pytest.raises(
        ValueError, match="recommendation_verification_decision_invalid"
    ):
        RecommendationRecord.verify(
            authority_namespace=namespace,
            proposal=proposal,
            verification_attempt=attempt,
            verification_decision=veto_decision,
            claim_settlement=settlement,
        )
