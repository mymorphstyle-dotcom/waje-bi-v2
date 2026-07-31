from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

from gate3_5_business_worlds import (
    BusinessWorld,
    ClaimDisposition,
)
from gate3_5_runtime_fixtures import (
    build_evidence_runtime_world,
    land_evidence_runtime_world,
)
from test_gate3_2_obligation_scheduler import NOW
from waje_vnext.domain.answering import (
    AnswerCandidateStatus,
    ClaimEvidenceSupport,
    ClaimPrecheckStatus,
    EvidenceSelection,
    NarrativeBlockProposal,
    ProposedClaim,
    ProvisionalAnswerBundle,
    build_provisional_answer_candidate,
    compile_provisional_answer_bundle,
)
from waje_vnext.domain.canonical import content_sha256
from waje_vnext.domain.evidence import (
    EvidenceAdmissionProfile,
    EvidenceAdmissionStatus,
    EvidenceValidityStatus,
    ObligationSatisfactionStatus,
    build_evidence_use_binding,
)
from waje_vnext.domain.measurement import ClaimStrengthCeiling
from waje_vnext.domain.planning import ExecutionRealm
from waje_vnext.domain.workflow import (
    DeliveryState,
    ExecutionState,
    ObligationDispositionFact,
    ObligationState,
    PlanAcceptedFact,
    PublicationDispositionFact,
    PublicationState,
    TaskExecutionBatchFact,
    TaskExecutionUpdate,
    WorkflowObligationDefinition,
    WorkflowReadModel,
    WorkflowTaskDefinition,
    replay_workflow,
)


ClaimMode: TypeAlias = Literal["supported", "boundary", "blocked"]


@dataclass(frozen=True, slots=True)
class ClaimExecution:
    claim_id: str
    mode: ClaimMode
    disposition: ClaimDisposition
    first_precheck_status: ClaimPrecheckStatus
    appears_in_answer: bool


@dataclass(frozen=True, slots=True)
class BusinessWorldExecution:
    world_id: str
    evidence_admission_status: EvidenceAdmissionStatus
    evidence_validity_status: EvidenceValidityStatus
    obligation_status: ObligationSatisfactionStatus
    first_bundle_status: AnswerCandidateStatus
    accepted_bundle: ProvisionalAnswerBundle
    claims: tuple[ClaimExecution, ...]
    workflow: WorkflowReadModel


def _event_identity(world_id: str, cursor: int) -> dict[str, object]:
    event_id = f"event:{world_id}:{cursor}"
    return {
        "cursor": cursor,
        "source_event_id": event_id,
        "source_event_sha256": content_sha256(
            {"event_id": event_id, "cursor": cursor}
        ),
    }


def _relation_kinds_by_claim(
    world: BusinessWorld,
) -> dict[str, frozenset[str]]:
    relation_kinds = {claim.claim_id: set() for claim in world.claim_targets}
    for relation in world.evidence_relations:
        for claim_id in relation.claim_ids:
            relation_kinds[claim_id].add(relation.kind)
    return {
        claim_id: frozenset(kinds)
        for claim_id, kinds in relation_kinds.items()
    }


def _mode_by_claim(world: BusinessWorld) -> dict[str, ClaimMode]:
    relation_kinds = _relation_kinds_by_claim(world)
    modes: dict[str, ClaimMode] = {}
    for claim in world.claim_targets:
        kinds = relation_kinds[claim.claim_id]
        if kinds & {"contradicts", "invalidates"}:
            modes[claim.claim_id] = "blocked"
        elif "qualifies" in kinds:
            modes[claim.claim_id] = "boundary"
        elif "supports" in kinds:
            modes[claim.claim_id] = "supported"
        else:
            raise AssertionError(
                f"claim {claim.claim_id} has no executable evidence fact"
            )
    return modes


def _proposals(
    *,
    world: BusinessWorld,
    mode_by_claim: dict[str, ClaimMode],
    evidence_record_id: str,
    estimand_id: str,
    obligation_id: str,
    boundary_estimand_id: str | None,
    boundary_obligation_id: str | None,
    boundary_satisfaction_id: str | None,
    scope,
    included_claim_ids: frozenset[str],
) -> tuple[ProposedClaim, ...]:
    result: list[ProposedClaim] = []
    for claim in world.claim_targets:
        if claim.claim_id not in included_claim_ids:
            continue
        mode = mode_by_claim[claim.claim_id]
        is_boundary = mode == "boundary"
        if is_boundary and (
            boundary_estimand_id is None
            or boundary_obligation_id is None
            or boundary_satisfaction_id is None
        ):
            raise AssertionError("typed boundary claim lacks boundary authority")
        result.append(
            ProposedClaim(
                proposal_claim_key=claim.claim_id,
                statement=claim.business_meaning,
                target_estimand_id=(
                    boundary_estimand_id if is_boundary else estimand_id
                ),
                obligation_ids=(
                    (boundary_obligation_id,)
                    if is_boundary
                    else (obligation_id,)
                ),
                evidence_selections=(
                    ()
                    if is_boundary
                    else (
                        EvidenceSelection(
                            evidence_record_id=evidence_record_id,
                            role_ref="business-world-primary-evidence",
                        ),
                    )
                ),
                applicability_scope=scope,
                requested_strength=(
                    ClaimStrengthCeiling.BOUNDARY_ONLY
                    if is_boundary
                    else claim.strength_ceiling
                ),
                boundary_satisfaction_record_ids=(
                    (boundary_satisfaction_id,) if is_boundary else ()
                ),
                limitation_refs=(),
                contradiction_refs=(),
                falsification_refs=(),
                reversal_refs=(),
                depends_on_proposal_claim_keys=(),
            )
        )
    return tuple(result)


def _compile(
    *,
    world: BusinessWorld,
    runtime_world,
    admission_outcome,
    mode_by_claim: dict[str, ClaimMode],
    included_claim_ids: frozenset[str],
) -> ProvisionalAnswerBundle:
    evidence = runtime_world.envelope.evidence_record
    current_authority = runtime_world.store.get_authority_snapshot(
        runtime_world.schedule.case_id
    )
    adoption = runtime_world.store.get_plan_adoption(
        runtime_world.schedule.plan_revision_id
    )
    proposals = _proposals(
        world=world,
        mode_by_claim=mode_by_claim,
        evidence_record_id=evidence.evidence_record_id,
        estimand_id=evidence.estimand_id,
        obligation_id=runtime_world.obligation.obligation_id,
        boundary_estimand_id=(
            None
            if runtime_world.boundary_obligation is None
            else runtime_world.boundary_obligation.estimand_id
        ),
        boundary_obligation_id=(
            None
            if runtime_world.boundary_obligation is None
            else runtime_world.boundary_obligation.obligation_id
        ),
        boundary_satisfaction_id=(
            None
            if runtime_world.boundary_satisfaction is None
            else runtime_world.boundary_satisfaction.obligation_satisfaction_id
        ),
        scope=runtime_world.scope,
        included_claim_ids=included_claim_ids,
    )
    candidate = build_provisional_answer_candidate(
        case_id=runtime_world.schedule.case_id,
        current_authority=current_authority,
        plan_adoption=adoption,
        version_number=1,
        prior_answer_version_id=None,
        claims=proposals,
        narrative_blocks=tuple(
            NarrativeBlockProposal(
                block_key=f"finding:{proposal.proposal_claim_key}",
                markdown=proposal.statement,
                proposal_claim_keys=(proposal.proposal_claim_key,),
            )
            for proposal in proposals
        ),
        created_by_action_id=f"action:{world.world_id}:propose-answer",
        created_at=NOW,
    )
    supports_by_claim_key: dict[str, tuple[ClaimEvidenceSupport, ...]] = {}
    checks_by_claim_key = {}
    for proposal in proposals:
        mode = mode_by_claim[proposal.proposal_claim_key]
        if mode in {"boundary", "blocked"}:
            supports_by_claim_key[proposal.proposal_claim_key] = ()
            checks_by_claim_key[proposal.proposal_claim_key] = ()
            continue
        use = build_evidence_use_binding(
            evidence=evidence,
            admission=admission_outcome.admission,
            validity=admission_outcome.validity,
            binding=runtime_world.binding,
            answer_candidate_id=candidate.answer_candidate_id,
            proposal_claim_key=proposal.proposal_claim_key,
            claim_scope=proposal.applicability_scope,
            requested_claim_strength=proposal.requested_strength,
            bound_at=NOW,
        )
        supports_by_claim_key[proposal.proposal_claim_key] = (
            ClaimEvidenceSupport(
                evidence=evidence,
                admission=admission_outcome.admission,
                validity=admission_outcome.validity,
                query_binding=runtime_world.binding,
                use_binding=use,
            ),
        )
        checks_by_claim_key[proposal.proposal_claim_key] = ()
    return compile_provisional_answer_bundle(
        candidate=candidate,
        current_authority=current_authority,
        plan_adoption=adoption,
        supports_by_claim_key=supports_by_claim_key,
        satisfactions_by_claim_key={
            proposal.proposal_claim_key: (
                (runtime_world.boundary_satisfaction,)
                if mode_by_claim[proposal.proposal_claim_key] == "boundary"
                else (admission_outcome.satisfaction,)
            )
            for proposal in proposals
        },
        check_dispositions_by_claim_key=checks_by_claim_key,
        checked_at=NOW,
    )


def _workflow(
    *,
    world: BusinessWorld,
    runtime_world,
    answer_version_id: str,
) -> WorkflowReadModel:
    store = runtime_world.store
    case = store.get_case(runtime_world.schedule.case_id)
    question = store.get_question(case.accepted_question_revision_id or "")
    frame = store.get_frame(case.accepted_frame_revision_id or "")
    plan = store.get_plan(case.accepted_plan_revision_id or "")
    adoption = store.get_plan_adoption(plan.plan_revision_id)
    task_by_obligation = {
        obligation_id: task
        for task in plan.tasks
        for obligation_id in task.obligation_ids
    }
    facts = [
        PlanAcceptedFact(
            case_id=case.case_id,
            question_revision_id=question.question_revision_id,
            question_content_sha256=question.content_sha256,
            frame_revision_id=frame.frame_revision_id,
            frame_content_sha256=frame.content_sha256,
            plan_revision_id=plan.plan_revision_id,
            prior_plan_revision_id=plan.prior_plan_revision_id,
            plan_content_sha256=plan.content_sha256,
            plan_adoption_id=adoption.plan_adoption_id,
            plan_adoption_sha256=adoption.content_sha256,
            tasks=tuple(
                WorkflowTaskDefinition(
                    task_id=task.task_id,
                    business_label=task.business_purpose,
                )
                for task in plan.tasks
            ),
            obligations=tuple(
                WorkflowObligationDefinition(
                    obligation_id=obligation.obligation_id,
                    task_id=task.task_id,
                    business_label=task.business_purpose,
                )
                for obligation in runtime_world.obligations
                for task in (task_by_obligation[obligation.obligation_id],)
            ),
            **_event_identity(world.world_id, 1),
        ),
        TaskExecutionBatchFact(
            case_id=case.case_id,
            plan_revision_id=plan.plan_revision_id,
            updates=tuple(
                TaskExecutionUpdate(
                    task_id=task.task_id,
                    state=ExecutionState.SUCCEEDED,
                )
                for task in plan.tasks
            ),
            **_event_identity(world.world_id, 2),
        ),
    ]
    for obligation in runtime_world.obligations:
        facts.append(
            ObligationDispositionFact(
                case_id=case.case_id,
                plan_revision_id=plan.plan_revision_id,
                obligation_id=obligation.obligation_id,
                state=(
                    ObligationState.BOUNDARY
                    if runtime_world.boundary_obligation is not None
                    and obligation.obligation_id
                    == runtime_world.boundary_obligation.obligation_id
                    else ObligationState.SATISFIED
                ),
                **_event_identity(world.world_id, len(facts) + 1),
            )
        )
    facts.append(
        PublicationDispositionFact(
            case_id=case.case_id,
            state=PublicationState.PROVISIONAL,
            answer_version_id=answer_version_id,
            **_event_identity(world.world_id, len(facts) + 1),
        ),
    )
    return replay_workflow(
        case.case_id,
        tuple(facts),
        realm=ExecutionRealm.CONFORMANCE,
        evidence_profile=EvidenceAdmissionProfile.CONFORMANCE,
    )


def execute_business_world(world: BusinessWorld) -> BusinessWorldExecution:
    mode_by_claim = _mode_by_claim(world)
    runtime_world = build_evidence_runtime_world(
        f"case-{world.world_id}",
        evidence_strength=ClaimStrengthCeiling.CAUSAL,
        limitation_refs=(),
        mixed_boundary=("boundary" in mode_by_claim.values()),
    )
    receipt = land_evidence_runtime_world(
        runtime_world,
        received_at=NOW,
    )
    admission_outcome = runtime_world.runtime.admit_result(
        receipt_id=receipt.capability_result_receipt_id,
        admitted_at=NOW,
    )
    all_claim_ids = frozenset(mode_by_claim)
    first_bundle = _compile(
        world=world,
        runtime_world=runtime_world,
        admission_outcome=admission_outcome,
        mode_by_claim=mode_by_claim,
        included_claim_ids=all_claim_ids,
    )
    accepted_bundle = first_bundle
    if first_bundle.status is AnswerCandidateStatus.REJECTED:
        accepted_claim_ids = frozenset(
            claim_id
            for claim_id, mode in mode_by_claim.items()
            if mode != "blocked"
        )
        accepted_bundle = _compile(
            world=world,
            runtime_world=runtime_world,
            admission_outcome=admission_outcome,
            mode_by_claim=mode_by_claim,
            included_claim_ids=accepted_claim_ids,
        )
    if (
        accepted_bundle.status
        is not AnswerCandidateStatus.ACCEPTED_PROVISIONAL
        or accepted_bundle.answer is None
    ):
        raise AssertionError(f"{world.world_id} has no provisional answer")
    first_precheck_by_key = {
        item.proposal_claim_key: item for item in first_bundle.prechecks
    }
    accepted_claim_keys = {
        item.proposal_claim_key for item in accepted_bundle.answer.claims
    }
    claim_executions: list[ClaimExecution] = []
    for claim in world.claim_targets:
        mode = mode_by_claim[claim.claim_id]
        disposition: ClaimDisposition
        if mode == "supported":
            disposition = "supported_provisional"
        elif mode == "boundary":
            disposition = "typed_boundary"
        else:
            disposition = "unverifiable"
        claim_executions.append(
            ClaimExecution(
                claim_id=claim.claim_id,
                mode=mode,
                disposition=disposition,
                first_precheck_status=(
                    first_precheck_by_key[claim.claim_id].status
                ),
                appears_in_answer=claim.claim_id in accepted_claim_keys,
            )
        )
    workflow = _workflow(
        world=world,
        runtime_world=runtime_world,
        answer_version_id=accepted_bundle.answer.answer_version_id,
    )
    if workflow.snapshot.case.delivery_state is not DeliveryState.NOT_DELIVERED:
        raise AssertionError("Gate 3 business world attempted delivery")
    return BusinessWorldExecution(
        world_id=world.world_id,
        evidence_admission_status=admission_outcome.admission.status,
        evidence_validity_status=admission_outcome.validity.status,
        obligation_status=admission_outcome.satisfaction.status,
        first_bundle_status=first_bundle.status,
        accepted_bundle=accepted_bundle,
        claims=tuple(claim_executions),
        workflow=workflow,
    )
