"""JSON codecs for persisted vNext domain records."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from waje_vnext.domain.authority import (
    AnalysisFrameRevision,
    AnswerClaim,
    AnswerStatus,
    AnswerVersion,
    ClaimVerifierStatus,
    DecisionOption,
    DecisionRecord,
    EvidenceRecord,
    EvidenceStrength,
    EvidenceType,
    InterpretationRecord,
    ResultHandle,
    ReviewerObjection,
    ReviewerObjectionStatus,
    ReviewerSeverity,
    WorkPlanRevision,
    WorkTask,
)
from waje_vnext.domain.canonical import to_jsonable


def encode_record(record: object) -> dict[str, Any]:
    encoded = to_jsonable(record)
    if not isinstance(encoded, dict):
        raise TypeError("persisted record must encode as a JSON object")
    return encoded


def decode_frame(payload: Mapping[str, Any]) -> AnalysisFrameRevision:
    return AnalysisFrameRevision(
        frame_revision_id=payload["frame_revision_id"],
        case_id=payload["case_id"],
        revision_number=payload["revision_number"],
        prior_frame_revision_id=payload["prior_frame_revision_id"],
        created_by_action_id=payload["created_by_action_id"],
        created_at=_datetime(payload["created_at"]),
        revision_reason=payload["revision_reason"],
        estimand=payload["estimand"],
        observation_unit=payload["observation_unit"],
        numerator=payload["numerator"],
        denominator=payload["denominator"],
        exposure=payload["exposure"],
        comparison=payload["comparison"],
        assumptions=tuple(payload["assumptions"]),
        alternatives=tuple(payload["alternatives"]),
        falsification_conditions=tuple(payload["falsification_conditions"]),
        reversal_conditions=tuple(payload["reversal_conditions"]),
        success_conditions=tuple(payload["success_conditions"]),
        stop_conditions=tuple(payload["stop_conditions"]),
        decision_record_ids=tuple(payload["decision_record_ids"]),
        semantic_contract_refs=tuple(payload["semantic_contract_refs"]),
    )


def decode_plan(payload: Mapping[str, Any]) -> WorkPlanRevision:
    return WorkPlanRevision(
        plan_revision_id=payload["plan_revision_id"],
        case_id=payload["case_id"],
        frame_revision_id=payload["frame_revision_id"],
        revision_number=payload["revision_number"],
        prior_plan_revision_id=payload["prior_plan_revision_id"],
        created_by_action_id=payload["created_by_action_id"],
        created_at=_datetime(payload["created_at"]),
        revision_reason=payload["revision_reason"],
        tasks=tuple(
            WorkTask(
                task_id=task["task_id"],
                business_purpose=task["business_purpose"],
                capability_intent=task["capability_intent"],
                target_claim_ids=tuple(task["target_claim_ids"]),
                depends_on_task_ids=tuple(task["depends_on_task_ids"]),
                success_conditions=tuple(task["success_conditions"]),
                stop_conditions=tuple(task["stop_conditions"]),
            )
            for task in payload["tasks"]
        ),
    )


def decode_evidence(payload: Mapping[str, Any]) -> EvidenceRecord:
    handle_payload = payload["result_handle"]
    handle = (
        None
        if handle_payload is None
        else ResultHandle(
            handle_id=handle_payload["handle_id"],
            content_sha256=handle_payload["content_sha256"],
            schema_ref=handle_payload["schema_ref"],
            row_count=handle_payload["row_count"],
            storage_ref=handle_payload["storage_ref"],
        )
    )
    return EvidenceRecord(
        evidence_record_id=payload["evidence_record_id"],
        case_id=payload["case_id"],
        frame_revision_id=payload["frame_revision_id"],
        plan_revision_id=payload["plan_revision_id"],
        task_id=payload["task_id"],
        capability_name=payload["capability_name"],
        query_spec_ref=payload["query_spec_ref"],
        semantic_contract_refs=tuple(payload["semantic_contract_refs"]),
        snapshot_release_ref=payload["snapshot_release_ref"],
        grain=payload["grain"],
        evidence_type=EvidenceType(payload["evidence_type"]),
        strength=EvidenceStrength(payload["strength"]),
        business_summary=payload["business_summary"],
        limitations=tuple(payload["limitations"]),
        provenance=payload["provenance"],
        payload_sha256=payload["payload_sha256"],
        inline_payload=payload["inline_payload"],
        result_handle=handle,
        created_at=_datetime(payload["created_at"]),
    )


def decode_answer(payload: Mapping[str, Any]) -> AnswerVersion:
    return AnswerVersion(
        answer_version_id=payload["answer_version_id"],
        case_id=payload["case_id"],
        frame_revision_id=payload["frame_revision_id"],
        plan_revision_id=payload["plan_revision_id"],
        version_number=payload["version_number"],
        prior_answer_version_id=payload["prior_answer_version_id"],
        status=AnswerStatus(payload["status"]),
        claims=tuple(
            AnswerClaim(
                claim_id=claim["claim_id"],
                statement=claim["statement"],
                applicability=claim["applicability"],
                evidence_record_ids=tuple(claim["evidence_record_ids"]),
                boundary_ref=claim["boundary_ref"],
                limitations=tuple(claim["limitations"]),
                verifier_status=ClaimVerifierStatus(claim["verifier_status"]),
                reviewer_objection_ids=tuple(claim["reviewer_objection_ids"]),
            )
            for claim in payload["claims"]
        ),
        narrative_markdown=payload["narrative_markdown"],
        verifier_policy_version=payload["verifier_policy_version"],
        unresolved_blocking_objection_ids=tuple(
            payload["unresolved_blocking_objection_ids"]
        ),
        settlement_fingerprint=payload["settlement_fingerprint"],
        created_by_action_id=payload["created_by_action_id"],
        created_at=_datetime(payload["created_at"]),
    )


def decode_interpretation(payload: Mapping[str, Any]) -> InterpretationRecord:
    return InterpretationRecord(
        interpretation_id=payload["interpretation_id"],
        case_id=payload["case_id"],
        frame_revision_id=payload["frame_revision_id"],
        evidence_record_ids=tuple(payload["evidence_record_ids"]),
        interpretation=payload["interpretation"],
        created_by_action_id=payload["created_by_action_id"],
        created_at=_datetime(payload["created_at"]),
    )


def decode_decision(payload: Mapping[str, Any]) -> DecisionRecord:
    return DecisionRecord(
        decision_record_id=payload["decision_record_id"],
        case_id=payload["case_id"],
        question=payload["question"],
        options=tuple(
            DecisionOption(
                option_id=option["option_id"],
                label=option["label"],
                impact=option["impact"],
            )
            for option in payload["options"]
        ),
        selected_option_id=payload["selected_option_id"],
        source=payload["source"],
        created_at=_datetime(payload["created_at"]),
    )


def decode_objection(payload: Mapping[str, Any]) -> ReviewerObjection:
    return ReviewerObjection(
        objection_id=payload["objection_id"],
        objection_key=payload["objection_key"],
        revision_number=payload["revision_number"],
        prior_objection_id=payload["prior_objection_id"],
        case_id=payload["case_id"],
        answer_version_id=payload["answer_version_id"],
        claim_id=payload["claim_id"],
        severity=ReviewerSeverity(payload["severity"]),
        status=ReviewerObjectionStatus(payload["status"]),
        risk_type=payload["risk_type"],
        evidence_gap=payload["evidence_gap"],
        requested_action=payload["requested_action"],
        disposition_note=payload["disposition_note"],
        created_at=_datetime(payload["created_at"]),
        resolved_at=(
            None
            if payload["resolved_at"] is None
            else _datetime(payload["resolved_at"])
        ),
    )


def _datetime(value: str | datetime) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)
