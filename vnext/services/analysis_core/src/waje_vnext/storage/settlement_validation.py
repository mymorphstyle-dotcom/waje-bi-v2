"""Storage-backed validation for settlement precondition inputs."""

from __future__ import annotations

from collections.abc import Iterable

from waje_vnext.domain.answering import (
    AnswerVersion,
    ClaimEvidenceSupport,
)
from waje_vnext.domain.authority import (
    ReviewerObjection,
    ReviewerObjectionStatus,
    ReviewerSeverity,
)
from waje_vnext.domain.canonical import content_sha256
from waje_vnext.domain.runtime_amendment import RunTraceManifest


def validate_settlement_request(
    *,
    answer: AnswerVersion,
    supports: tuple[ClaimEvidenceSupport, ...],
    trace_manifest: RunTraceManifest,
    trace_manifest_content_sha256: str,
    trace_complete: bool,
    objections: Iterable[ReviewerObjection],
    objection_disposition_refs: tuple[str, ...],
    unresolved_blocking_objection_refs: tuple[str, ...],
) -> bool:
    """Reject caller claims that differ from persisted trace/reviewer facts."""

    if (
        trace_manifest.case_id != answer.case_id
        or content_sha256(trace_manifest)
        != trace_manifest_content_sha256
    ):
        raise ValueError(
            "settlement trace manifest identity does not match persisted state"
        )

    latest_by_key: dict[str, ReviewerObjection] = {}
    for objection in objections:
        if objection.answer_version_id != answer.answer_version_id:
            continue
        prior = latest_by_key.get(objection.objection_key)
        if (
            prior is None
            or objection.revision_number > prior.revision_number
        ):
            latest_by_key[objection.objection_key] = objection
    latest = tuple(
        sorted(
            latest_by_key.values(),
            key=lambda item: item.objection_key,
        )
    )
    expected_dispositions = tuple(
        sorted(
            item.objection_id
            for item in latest
            if item.status is not ReviewerObjectionStatus.OPEN
        )
    )
    expected_unresolved = tuple(
        sorted(
            item.objection_id
            for item in latest
            if (
                item.status is ReviewerObjectionStatus.OPEN
                and item.severity is ReviewerSeverity.BLOCKING
            )
        )
    )
    if (
        tuple(sorted(objection_disposition_refs))
        != expected_dispositions
        or tuple(sorted(unresolved_blocking_objection_refs))
        != expected_unresolved
    ):
        raise ValueError(
            "settlement objection inputs do not match persisted reviewer heads"
        )

    answer_claim_ids = {
        claim.claim_id for claim in answer.claims
    }
    answer_obligation_ids = {
        obligation_id
        for claim in answer.claims
        for obligation_id in claim.obligation_ids
    }
    support_evidence_ids = {
        support.evidence.evidence_record_id
        for support in supports
    }
    derived_trace_complete = (
        answer.answer_version_id
        in trace_manifest.provisional_answer_version_ids
        and answer.plan_revision_id in trace_manifest.plan_revision_ids
        and answer_claim_ids.issubset(trace_manifest.claim_ids)
        and answer_obligation_ids.issubset(
            trace_manifest.obligation_ids
        )
        and support_evidence_ids.issubset(
            trace_manifest.evidence_record_ids
        )
        and all(
            support.evidence.case_id == answer.case_id
            and support.evidence.run_id == trace_manifest.run_id
            for support in supports
        )
    )
    if trace_complete is not derived_trace_complete:
        raise ValueError(
            "settlement trace completeness does not match persisted lineage"
        )
    return derived_trace_complete
