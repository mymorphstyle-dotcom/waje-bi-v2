"""JSON codecs for persisted vNext domain records."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from waje_vnext.domain.action_codec import decode_agent_action_proposal
from waje_vnext.domain.actions import ActionEnvelope, ActionKind
from waje_vnext.domain.async_runtime import AsyncJobKind, OperationIdentity
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
from waje_vnext.domain.context import (
    ContextDecisionItem,
    ContextEvidenceItem,
    ContextEventItem,
    ContextPacket,
    ContextReviewerObjectionItem,
    ContextUserMessageItem,
)
from waje_vnext.domain.controller import (
    ControllerPhase,
    ControllerState,
    EffectAttemptRecord,
    EffectAttemptStatus,
    PersistedAction,
    UserDecisionRequest,
)
from waje_vnext.domain.runtime_state import (
    ActionReceipt,
    CheckpointRecord,
    OutboxMessage,
)
from waje_vnext.domain.measurement import (
    EvidenceValidityRecord,
    MeasurementResolutionOutcome,
    ObligationSatisfactionRecord,
    QuestionRevision,
    ResolvedEvidenceObligation,
    SettlementPreconditionReport,
)
from waje_vnext.domain.typed_decode import decode_typed_dataclass


def encode_record(record: object) -> dict[str, Any]:
    encoded = to_jsonable(record)
    if not isinstance(encoded, dict):
        raise TypeError("persisted record must encode as a JSON object")
    return encoded


def decode_question(payload: Mapping[str, Any]) -> QuestionRevision:
    return decode_typed_dataclass(QuestionRevision, payload)


def decode_measurement_resolution(
    payload: Mapping[str, Any],
) -> MeasurementResolutionOutcome:
    return decode_typed_dataclass(MeasurementResolutionOutcome, payload)


def decode_evidence_obligation(
    payload: Mapping[str, Any],
) -> ResolvedEvidenceObligation:
    return decode_typed_dataclass(ResolvedEvidenceObligation, payload)


def decode_evidence_validity(
    payload: Mapping[str, Any],
) -> EvidenceValidityRecord:
    return decode_typed_dataclass(EvidenceValidityRecord, payload)


def decode_obligation_satisfaction(
    payload: Mapping[str, Any],
) -> ObligationSatisfactionRecord:
    return decode_typed_dataclass(ObligationSatisfactionRecord, payload)


def decode_settlement_precondition(
    payload: Mapping[str, Any],
) -> SettlementPreconditionReport:
    return decode_typed_dataclass(SettlementPreconditionReport, payload)


def decode_frame(payload: Mapping[str, Any]) -> AnalysisFrameRevision:
    return decode_typed_dataclass(AnalysisFrameRevision, payload)


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
        freeform_response=payload["freeform_response"],
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


def decode_persisted_action(
    payload: Mapping[str, Any],
) -> PersistedAction:
    action_payload = payload["action"]
    proposal = decode_agent_action_proposal(
        {
            "kind": action_payload["kind"],
            "payload": action_payload["payload"],
        }
    )
    action = ActionEnvelope(
        action_id=action_payload["action_id"],
        case_id=action_payload["case_id"],
        kind=ActionKind(action_payload["kind"]),
        expected_head_version=action_payload["expected_head_version"],
        idempotency_key=action_payload["idempotency_key"],
        operation=OperationIdentity(**action_payload["operation"]),
        issued_at=_datetime(action_payload["issued_at"]),
        payload=proposal.payload,
    )
    return PersistedAction(
        action=action,
        proposal_sha256=payload["proposal_sha256"],
        recorded_at=_datetime(payload["recorded_at"]),
    )


def decode_context_packet(payload: Mapping[str, Any]) -> ContextPacket:
    return ContextPacket(
        packet_id=payload["packet_id"],
        case_id=payload["case_id"],
        head_version=payload["head_version"],
        accepted_question_revision_id=(
            payload["accepted_question_revision_id"]
        ),
        accepted_frame_revision_id=payload["accepted_frame_revision_id"],
        accepted_plan_revision_id=payload["accepted_plan_revision_id"],
        accepted_answer_version_id=payload["accepted_answer_version_id"],
        accepted_question_payload=payload["accepted_question_payload"],
        accepted_frame_payload=payload["accepted_frame_payload"],
        accepted_plan_payload=payload["accepted_plan_payload"],
        accepted_answer_payload=payload["accepted_answer_payload"],
        user_messages=tuple(
            ContextUserMessageItem(
                message_id=item["message_id"],
                sequence=item["sequence"],
                authority_epoch=item["authority_epoch"],
                kind=item["kind"],
                content=item["content"],
            )
            for item in payload["user_messages"]
        ),
        relevant_event_cursor_start=payload["relevant_event_cursor_start"],
        relevant_event_cursor_end=payload["relevant_event_cursor_end"],
        recent_events=tuple(
            ContextEventItem(
                cursor=event["cursor"],
                event_type=event["event_type"],
                authority_ref=event["authority_ref"],
                business_projection=event["business_projection"],
            )
            for event in payload["recent_events"]
        ),
        evidence_index=tuple(
            ContextEvidenceItem(
                evidence_record_id=item["evidence_record_id"],
                evidence_type=item["evidence_type"],
                strength=item["strength"],
                business_summary=item["business_summary"],
                limitation_count=item["limitation_count"],
                frame_revision_id=item["frame_revision_id"],
                plan_revision_id=item["plan_revision_id"],
                task_id=item["task_id"],
                snapshot_release_ref=item["snapshot_release_ref"],
            )
            for item in payload["evidence_index"]
        ),
        decision_index=tuple(
            ContextDecisionItem(
                decision_record_id=item["decision_record_id"],
                question=item["question"],
                selected_option_id=item["selected_option_id"],
                freeform_response=item["freeform_response"],
                source=item["source"],
            )
            for item in payload["decision_index"]
        ),
        reviewer_objection_index=tuple(
            ContextReviewerObjectionItem(
                objection_id=item["objection_id"],
                objection_key=item["objection_key"],
                revision_number=item["revision_number"],
                answer_version_id=item["answer_version_id"],
                claim_id=item["claim_id"],
                severity=item["severity"],
                status=item["status"],
                risk_type=item["risk_type"],
                evidence_gap=item["evidence_gap"],
                requested_action=item["requested_action"],
                disposition_note=item["disposition_note"],
            )
            for item in payload["reviewer_objection_index"]
        ),
        built_at=_datetime(payload["built_at"]),
        content_sha256=payload["content_sha256"],
    )


def decode_action_receipt(payload: Mapping[str, Any]) -> ActionReceipt:
    return ActionReceipt(
        case_id=payload["case_id"],
        idempotency_key=payload["idempotency_key"],
        action_id=payload["action_id"],
        request_sha256=payload["request_sha256"],
        result_schema_ref=payload["result_schema_ref"],
        result_payload=payload["result_payload"],
        result_sha256=payload["result_sha256"],
        event_cursor=payload["event_cursor"],
        recorded_at=_datetime(payload["recorded_at"]),
    )


def decode_checkpoint(payload: Mapping[str, Any]) -> CheckpointRecord:
    return CheckpointRecord(
        checkpoint_id=payload["checkpoint_id"],
        case_id=payload["case_id"],
        head_version=payload["head_version"],
        event_cursor=payload["event_cursor"],
        context_packet_id=payload["context_packet_id"],
        context_sha256=payload["context_sha256"],
        state_schema_ref=payload["state_schema_ref"],
        state_payload=payload["state_payload"],
        state_sha256=payload["state_sha256"],
        created_at=_datetime(payload["created_at"]),
    )


def decode_outbox_message(payload: Mapping[str, Any]) -> OutboxMessage:
    return OutboxMessage(
        outbox_message_id=payload["outbox_message_id"],
        case_id=payload["case_id"],
        source_event_cursor=payload["source_event_cursor"],
        action_id=payload["action_id"],
        job_kind=AsyncJobKind(payload["job_kind"]),
        operation=OperationIdentity(**payload["operation"]),
        expected_head_version=payload["expected_head_version"],
        expected_authority_epoch=payload["expected_authority_epoch"],
        idempotency_key=payload["idempotency_key"],
        destination=payload["destination"],
        contract_ref=payload["contract_ref"],
        payload=payload["payload"],
        payload_sha256=payload["payload_sha256"],
        created_at=_datetime(payload["created_at"]),
    )


def decode_controller_state(
    payload: Mapping[str, Any],
) -> ControllerState:
    return ControllerState(
        run_id=payload["run_id"],
        case_id=payload["case_id"],
        phase=ControllerPhase(payload["phase"]),
        step_number=payload["step_number"],
        head_version=payload["head_version"],
        authority_epoch=payload["authority_epoch"],
        mailbox_cursor=payload["mailbox_cursor"],
        last_event_cursor=payload["last_event_cursor"],
        context_packet_id=payload["context_packet_id"],
        latest_user_message=payload["latest_user_message"],
        pending_action_id=payload["pending_action_id"],
        pending_job_ids=tuple(payload["pending_job_ids"]),
        pending_decision_request_id=payload["pending_decision_request_id"],
        accepted_answer_version_id=payload["accepted_answer_version_id"],
        consecutive_rejections=payload["consecutive_rejections"],
        updated_at=_datetime(payload["updated_at"]),
    )


def decode_decision_request(
    payload: Mapping[str, Any],
) -> UserDecisionRequest:
    return UserDecisionRequest(
        decision_request_id=payload["decision_request_id"],
        case_id=payload["case_id"],
        action_id=payload["action_id"],
        question=payload["question"],
        options=tuple(
            DecisionOption(
                option_id=option["option_id"],
                label=option["label"],
                impact=option["impact"],
            )
            for option in payload["options"]
        ),
        recommended_option_id=payload["recommended_option_id"],
        allow_freeform=payload["allow_freeform"],
        requested_at=_datetime(payload["requested_at"]),
    )


def decode_effect_attempt(
    payload: Mapping[str, Any],
) -> EffectAttemptRecord:
    return EffectAttemptRecord(
        effect_attempt_id=payload["effect_attempt_id"],
        outbox_message_id=payload["outbox_message_id"],
        case_id=payload["case_id"],
        attempt_number=payload["attempt_number"],
        prior_attempt_id=payload["prior_attempt_id"],
        status=EffectAttemptStatus(payload["status"]),
        result_payload=payload["result_payload"],
        result_sha256=payload["result_sha256"],
        error_code=payload["error_code"],
        error_message=payload["error_message"],
        started_at=_datetime(payload["started_at"]),
        completed_at=_datetime(payload["completed_at"]),
    )


def _datetime(value: str | datetime) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)
