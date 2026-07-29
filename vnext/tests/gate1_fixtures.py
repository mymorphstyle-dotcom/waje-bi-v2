from __future__ import annotations

from datetime import UTC, datetime, timedelta

from waje_vnext.domain.authority import (
    AnalysisFrameRevision,
    AnswerClaim,
    AnswerStatus,
    AnswerVersion,
    ClaimVerifierStatus,
    EvidenceRecord,
    EvidenceStrength,
    EvidenceType,
    ReviewerObjection,
    ReviewerObjectionStatus,
    ReviewerSeverity,
    WorkPlanRevision,
    WorkTask,
    compute_answer_settlement_fingerprint,
)
from waje_vnext.domain.canonical import content_sha256


NOW = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)


def make_frame(
    *,
    revision_number: int = 1,
    frame_id: str | None = None,
    prior_id: str | None = None,
    action_id: str | None = None,
) -> AnalysisFrameRevision:
    return AnalysisFrameRevision(
        frame_revision_id=frame_id or "frame-{}".format(revision_number),
        case_id="case-1",
        revision_number=revision_number,
        prior_frame_revision_id=prior_id,
        created_by_action_id=action_id
        or "action-frame-{}".format(revision_number),
        created_at=NOW + timedelta(
            minutes=60 * (revision_number - 1) + 1
        ),
        revision_reason="Define the current business measurement",
        estimand="Average exposed-period paid amount minus comparison-period amount",
        observation_unit="calendar month",
        numerator="valid paid amount in the exposure window",
        denominator="complete observed months",
        exposure="contract-defined month-start window",
        comparison="contract-defined mid-month and month-end windows",
        assumptions=("Paid amount contract is valid",),
        alternatives=("Composition shift may explain the pattern",),
        falsification_conditions=("Pattern disappears in complete-month sensitivity",),
        reversal_conditions=("Comparison window exceeds exposure window",),
        success_conditions=("Exposure and comparison are measured at the same grain",),
        stop_conditions=("Required metric contract is missing",),
        semantic_contract_refs=("metric:paid_amount:v1",),
    )


def make_plan(
    *,
    frame_id: str = "frame-1",
    revision_number: int = 1,
    plan_id: str | None = None,
    prior_id: str | None = None,
    action_id: str | None = None,
) -> WorkPlanRevision:
    return WorkPlanRevision(
        plan_revision_id=plan_id or "plan-{}".format(revision_number),
        case_id="case-1",
        frame_revision_id=frame_id,
        revision_number=revision_number,
        prior_plan_revision_id=prior_id,
        created_by_action_id=action_id or "action-plan-{}".format(revision_number),
        created_at=NOW + timedelta(
            minutes=60 * (revision_number - 1) + 11
        ),
        revision_reason="Investigate the accepted frame",
        tasks=(
            WorkTask(
                task_id="task-pattern",
                business_purpose="Measure the recurring within-month pattern",
                capability_intent="periodic pattern comparison",
                target_claim_ids=("claim-pattern",),
                depends_on_task_ids=(),
                success_conditions=("Comparable windows are measured",),
                stop_conditions=("Coverage is insufficient",),
            ),
        ),
    )


def make_evidence(
    *,
    evidence_id: str = "evidence-1",
    frame_id: str = "frame-1",
    plan_id: str = "plan-1",
    payload: dict[str, object] | None = None,
) -> EvidenceRecord:
    inline_payload = payload or {
        "exposure_amount": 120.0,
        "comparison_amount": 100.0,
        "complete_months": 24,
    }
    return EvidenceRecord(
        evidence_record_id=evidence_id,
        case_id="case-1",
        frame_revision_id=frame_id,
        plan_revision_id=plan_id,
        task_id="task-pattern",
        capability_name="periodic_pattern_compare",
        query_spec_ref="query-spec-1",
        semantic_contract_refs=("metric:paid_amount:v1",),
        snapshot_release_ref="release-2026-07-29",
        grain="calendar_month",
        evidence_type=EvidenceType.DESCRIPTIVE,
        strength=EvidenceStrength.QUANTIFIED,
        business_summary="Month-start paid amount is higher in the measured sample",
        limitations=("Descriptive evidence does not identify a mechanism",),
        provenance={
            "query_spec_ref": "query-spec-1",
            "snapshot_release_ref": "release-2026-07-29",
        },
        payload_sha256=content_sha256(inline_payload),
        inline_payload=inline_payload,
        result_handle=None,
        created_at=NOW + timedelta(minutes=20),
    )


def make_answer(
    *,
    status: AnswerStatus = AnswerStatus.SETTLED,
    answer_id: str = "answer-1",
    frame_id: str = "frame-1",
    plan_id: str = "plan-1",
    evidence_id: str = "evidence-1",
    version_number: int = 1,
    prior_id: str | None = None,
    unresolved: tuple[str, ...] = (),
    verifier_status: ClaimVerifierStatus = ClaimVerifierStatus.ACCEPTED,
) -> AnswerVersion:
    claims = (
        AnswerClaim(
            claim_id="claim-pattern",
            statement="The measured exposure window is higher",
            applicability="Accepted frame and release-2026-07-29",
            evidence_record_ids=(evidence_id,),
            boundary_ref=None,
            limitations=("Mechanism remains unproven",),
            verifier_status=verifier_status,
            reviewer_objection_ids=(),
        ),
    )
    return AnswerVersion(
        answer_version_id=answer_id,
        case_id="case-1",
        frame_revision_id=frame_id,
        plan_revision_id=plan_id,
        version_number=version_number,
        prior_answer_version_id=prior_id,
        status=status,
        claims=claims,
        narrative_markdown="The measured month-start window is higher.",
        verifier_policy_version="answer-verifier.v1",
        unresolved_blocking_objection_ids=unresolved,
        settlement_fingerprint=(
            compute_answer_settlement_fingerprint(
                frame_revision_id=frame_id,
                plan_revision_id=plan_id,
                claims=claims,
                verifier_policy_version="answer-verifier.v1",
            )
            if status is AnswerStatus.SETTLED
            else None
        ),
        created_by_action_id="action-answer-1",
        created_at=NOW + timedelta(
            minutes=60 * (version_number - 1) + 30
        ),
    )


def make_objection(
    *,
    objection_id: str = "objection-1",
    revision_number: int = 1,
    prior_id: str | None = None,
    status: ReviewerObjectionStatus = ReviewerObjectionStatus.OPEN,
    answer_id: str = "answer-1",
) -> ReviewerObjection:
    resolved = status is not ReviewerObjectionStatus.OPEN
    created_at = NOW + timedelta(minutes=30 + revision_number)
    return ReviewerObjection(
        objection_id=objection_id,
        objection_key="claim-pattern:causal-overreach",
        revision_number=revision_number,
        prior_objection_id=prior_id,
        case_id="case-1",
        answer_version_id=answer_id,
        claim_id="claim-pattern",
        severity=ReviewerSeverity.BLOCKING,
        status=status,
        risk_type="claim_strength",
        evidence_gap="Descriptive evidence cannot support a causal mechanism",
        requested_action="Limit the claim to the measured association",
        disposition_note=(
            "Claim language now matches the descriptive evidence"
            if resolved
            else None
        ),
        created_at=created_at,
        resolved_at=(
            created_at + timedelta(minutes=1)
            if resolved
            else None
        ),
    )
