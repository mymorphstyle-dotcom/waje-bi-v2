from __future__ import annotations

from bi_agent.runtime.authoritative_execution_result import (
    AuthoritativeExecutionResult,
)
from bi_agent.runtime.claim_coverage import (
    ClaimCoverageCheckpoint,
    ClaimCoverageEvaluation,
    ClaimEvidenceContractReview,
    ClaimEvidenceCoverageAssessment,
    ClaimObligationCoverage,
    PlanExpansionDecision,
    claim_coverage_transition_payloads,
)
from bi_agent.runtime.claim_settlement import (
    admissible_obligation_evidence_source_claim_kind,
    evidence_publication_ceiling,
    publication_ceiling_satisfies,
)
from bi_agent.runtime.evidence_authority import canonical_digest
from bi_agent.runtime.single_authority import DurableTransition


def resolved_test_claim_coverage_checkpoint(
    execution_result: AuthoritativeExecutionResult,
) -> ClaimCoverageCheckpoint:
    """Build the current mandatory claim-coverage checkpoint for typed fixtures."""

    execution = AuthoritativeExecutionResult.from_dict(execution_result.to_dict())
    plan = execution.plan_revision
    task_by_id = {item.task_id: item for item in plan.capability_tasks}
    obligation_by_id = {
        item.obligation_id: item for item in plan.claim_obligations
    }
    assessments_by_obligation: dict[
        str, list[ClaimEvidenceCoverageAssessment]
    ] = {item.obligation_id: [] for item in plan.claim_obligations}
    reviews: list[ClaimEvidenceContractReview] = []

    for _, outcome, entries, _ in execution.capability_outcome_bundles:
        task = task_by_id[outcome.task_id]
        for entry in entries:
            review = ClaimEvidenceContractReview.create(
                evidence_entry_ref=entry.entry_ref,
                settlement_outcome_ref=outcome.outcome_ref,
                task_id=task.task_id,
                capability_id=task.capability_id,
                contract_match_state="full",
                publication_disposition="direct",
                evidence_kind=entry.evidence_kind,
                capability_evidence_kinds=(entry.evidence_kind,),
                evidence_supported_claim_kinds=entry.supported_claim_kinds,
                capability_supported_claim_kinds=entry.supported_claim_kinds,
                effective_supported_claim_kinds=entry.supported_claim_kinds,
                evidence_maximum_claim_strength=entry.maximum_claim_strength,
                capability_maximum_claim_strength=entry.maximum_claim_strength,
                effective_maximum_claim_strength_by_claim={
                    claim_kind: entry.maximum_claim_strength
                    for claim_kind in entry.supported_claim_kinds
                },
                audit_codes=(),
            )
            reviews.append(review)
            for obligation_id in task.supports_obligation_ids:
                obligation = obligation_by_id[obligation_id]
                source_claim_kind = (
                    admissible_obligation_evidence_source_claim_kind(
                        obligation=obligation,
                        evidence_kind=entry.evidence_kind,
                        supported_claim_kinds=entry.supported_claim_kinds,
                        maximum_claim_strength=entry.maximum_claim_strength,
                    )
                )
                if source_claim_kind is None:
                    continue
                assessments_by_obligation[obligation_id].append(
                    ClaimEvidenceCoverageAssessment.create(
                        evidence_entry_ref=entry.entry_ref,
                        settlement_outcome_ref=outcome.outcome_ref,
                        binding_record_ref=entry.binding_record_ref,
                        evidence_kind=entry.evidence_kind,
                        evidence_strength=entry.evidence_strength,
                        maximum_claim_strength=entry.maximum_claim_strength,
                        publication_ceiling=evidence_publication_ceiling(
                            evidence_kind=entry.evidence_kind,
                            source_claim_kind=source_claim_kind,
                            maximum_claim_strength=entry.maximum_claim_strength,
                        ).to_dict(),
                        data_contract_state=entry.data_contract_state,
                        source_claim_kind=source_claim_kind,
                        obligation_claim_kind=obligation.claim_kind,
                        supported_claim_kinds=entry.supported_claim_kinds,
                        contract_review_ref=review.review_ref,
                        publication_disposition="direct",
                        observation_facts=entry.observation_facts,
                        scope=entry.scope,
                        window_refs=entry.window_refs,
                        dimension_path=entry.dimension_path,
                        limitation_refs=entry.limitation_refs,
                        result_refs=entry.result_refs,
                        completeness_report_refs=entry.completeness_report_refs,
                    )
                )

    obligation_coverages = []
    for obligation in plan.claim_obligations:
        assessments = tuple(
            assessments_by_obligation[obligation.obligation_id]
        )
        explicit_boundary = bool(assessments) and all(
            item.evidence_kind == "boundary"
            and bool(item.limitation_refs)
            and publication_ceiling_satisfies(
                item.publication_ceiling,
                required_strength=str(
                    obligation.success_policy["minimum_claim_strength"]
                ),
            )
            for item in assessments
        )
        obligation_coverages.append(
            ClaimObligationCoverage.create(
                obligation_id=obligation.obligation_id,
                claim_kind=obligation.claim_kind,
                role=obligation.role,
                subject=obligation.subject,
                success_policy=obligation.success_policy,
                status=(
                    "explicit_boundary"
                    if explicit_boundary
                    else "evidence_present"
                    if assessments
                    else "uncovered"
                ),
                evidence_assessments=assessments,
            )
        )

    evaluation = ClaimCoverageEvaluation.create(
        plan_revision=plan,
        execution_result=execution,
        evidence_contract_reviews=tuple(reviews),
        obligation_coverages=tuple(obligation_coverages),
        admissible_routes=(),
    )
    decision = PlanExpansionDecision.deterministic_seal(evaluation)
    transition_input, transition_output = claim_coverage_transition_payloads(
        evaluation=evaluation,
        decision=decision,
        plan_patch=None,
    )
    transition = DurableTransition.create(
        node_name="evaluate_claim_coverage",
        parent_transition_id=execution.transition_id,
        run_attempt_id=plan.run_attempt_id,
        intent_revision_id=plan.intent_revision_id,
        decision_ledger_position=(
            execution.durable_transition.decision_ledger_position
        ),
        input_digest=canonical_digest(transition_input),
        output_digest=canonical_digest(transition_output),
        execution_attempt=1,
        provider_ref="local_deterministic",
        model_ref="claim-coverage-contract.v1",
        status="succeeded",
        acceptance_state="accepted",
        next_transition="seal_authority_bundle",
        started_at="2026-07-18T08:00:01+00:00",
        finished_at="2026-07-18T08:00:02+00:00",
    )
    return ClaimCoverageCheckpoint.create(
        plan_revision=plan,
        execution_result=execution,
        evaluation=evaluation,
        decision=decision,
        plan_patch=None,
        transition=transition,
    )
