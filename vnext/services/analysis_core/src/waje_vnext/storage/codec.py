"""JSON codecs for persisted vNext domain records."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from waje_vnext.domain.action_codec import decode_agent_action_proposal
from waje_vnext.domain.actions import ActionEnvelope, ActionKind
from waje_vnext.domain.async_runtime import (
    AsyncJobKind,
    AuthoritySnapshot,
    OperationIdentity,
)
from waje_vnext.domain.authority import (
    AnalysisFrameRevision,
    DecisionOption,
    DecisionRecord,
    InterpretationRecord,
    ReviewerObjection,
    ReviewerObjectionStatus,
    ReviewerSeverity,
    WorkPlanRevision,
)
from waje_vnext.domain.answering import (
    AnalysisCheckDisposition,
    AnswerVersion,
    ClaimPrecheckRecord,
    ProvisionalAnswerCandidate,
    SettlementPreconditionReport,
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
from waje_vnext.domain.evidence import (
    CapabilityResultEnvelope,
    CapabilityResultReceipt,
    EvidenceAdmissionRecord,
    EvidenceRecord,
    EvidenceUseBinding,
    EvidenceValidityRecord,
    ObligationSatisfactionRecord,
)
from waje_vnext.domain.runtime_state import (
    ActionReceipt,
    CheckpointRecord,
    OutboxMessage,
)
from waje_vnext.domain.runtime_amendment import (
    FrameAdmissionProof,
    DurableModelResult,
    FrameCandidateRecord,
    FrameCandidateSupersessionRecord,
    FrameReviewRecord,
    JobDispositionRecord,
    LogicalModelJob,
    MessageImpactBinding,
    MessageIngressRecord,
    ObjectionClosureRecord,
    PendingUserMessage,
    ProviderAttemptReceipt,
    ProviderAttemptRequest,
    RunTraceManifest,
)
from waje_vnext.domain.measurement import (
    MeasurementResolutionOutcome,
    QuestionRevision,
    ResolvedEvidenceObligation,
)
from waje_vnext.domain.obligation_scheduler import (
    ObligationCompletionRecord,
    ObligationDispatchRecord,
    ObligationScheduleCheckpoint,
    ObligationScheduleRecord,
)
from waje_vnext.domain.measurement_resolver import (
    MeasurementResolutionAdmission,
)
from waje_vnext.domain.planning import (
    ConformanceExecutionSpec,
    LogicalExecutionAttempt,
    PlanAdoptionRecord,
    QueryBindingEnvelope,
)
from waje_vnext.domain.typed_decode import decode_typed_dataclass
from waje_vnext.domain.workflow import (
    WorkflowApplicationReceipt,
    WorkflowProjectionHead,
    WorkflowSnapshot,
)


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


def decode_measurement_resolution_admission(
    payload: Mapping[str, Any],
) -> MeasurementResolutionAdmission:
    return decode_typed_dataclass(
        MeasurementResolutionAdmission,
        payload,
    )


def decode_evidence_obligation(
    payload: Mapping[str, Any],
) -> ResolvedEvidenceObligation:
    return decode_typed_dataclass(ResolvedEvidenceObligation, payload)


def decode_frame(payload: Mapping[str, Any]) -> AnalysisFrameRevision:
    return decode_typed_dataclass(AnalysisFrameRevision, payload)


def decode_frame_candidate(
    payload: Mapping[str, Any],
) -> FrameCandidateRecord:
    return decode_typed_dataclass(FrameCandidateRecord, payload)


def decode_frame_candidate_supersession(
    payload: Mapping[str, Any],
) -> FrameCandidateSupersessionRecord:
    return decode_typed_dataclass(
        FrameCandidateSupersessionRecord,
        payload,
    )


def decode_frame_review(
    payload: Mapping[str, Any],
) -> FrameReviewRecord:
    return decode_typed_dataclass(FrameReviewRecord, payload)


def decode_objection_closure(
    payload: Mapping[str, Any],
) -> ObjectionClosureRecord:
    return decode_typed_dataclass(ObjectionClosureRecord, payload)


def decode_frame_admission_proof(
    payload: Mapping[str, Any],
) -> FrameAdmissionProof:
    return decode_typed_dataclass(FrameAdmissionProof, payload)


def decode_job_disposition(
    payload: Mapping[str, Any],
) -> JobDispositionRecord:
    return decode_typed_dataclass(JobDispositionRecord, payload)


def decode_message_ingress_record(
    payload: Mapping[str, Any],
) -> MessageIngressRecord:
    return decode_typed_dataclass(MessageIngressRecord, payload)


def decode_pending_user_message(
    payload: Mapping[str, Any],
) -> PendingUserMessage:
    return decode_typed_dataclass(PendingUserMessage, payload)


def decode_message_impact_binding(
    payload: Mapping[str, Any],
) -> MessageImpactBinding:
    return decode_typed_dataclass(MessageImpactBinding, payload)


def decode_logical_model_job(
    payload: Mapping[str, Any],
) -> LogicalModelJob:
    return decode_typed_dataclass(LogicalModelJob, payload)


def decode_provider_attempt_request(
    payload: Mapping[str, Any],
) -> ProviderAttemptRequest:
    return decode_typed_dataclass(ProviderAttemptRequest, payload)


def decode_provider_attempt_receipt(
    payload: Mapping[str, Any],
) -> ProviderAttemptReceipt:
    return decode_typed_dataclass(ProviderAttemptReceipt, payload)


def decode_durable_model_result(
    payload: Mapping[str, Any],
) -> DurableModelResult:
    return decode_typed_dataclass(DurableModelResult, payload)


def decode_obligation_schedule(
    payload: Mapping[str, Any],
) -> ObligationScheduleRecord:
    return decode_typed_dataclass(ObligationScheduleRecord, payload)


def decode_obligation_dispatch(
    payload: Mapping[str, Any],
) -> ObligationDispatchRecord:
    return decode_typed_dataclass(ObligationDispatchRecord, payload)


def decode_obligation_completion_record(
    payload: Mapping[str, Any],
) -> ObligationCompletionRecord:
    return decode_typed_dataclass(ObligationCompletionRecord, payload)


def decode_obligation_schedule_checkpoint(
    payload: Mapping[str, Any],
) -> ObligationScheduleCheckpoint:
    return decode_typed_dataclass(
        ObligationScheduleCheckpoint,
        payload,
    )


def decode_run_trace_manifest(
    payload: Mapping[str, Any],
) -> RunTraceManifest:
    return decode_typed_dataclass(RunTraceManifest, payload)


def decode_plan(payload: Mapping[str, Any]) -> WorkPlanRevision:
    return decode_typed_dataclass(
        WorkPlanRevision,
        payload,
    )


def decode_plan_adoption(
    payload: Mapping[str, Any],
) -> PlanAdoptionRecord:
    return decode_typed_dataclass(PlanAdoptionRecord, payload)


def decode_query_binding(
    payload: Mapping[str, Any],
) -> QueryBindingEnvelope:
    return decode_typed_dataclass(QueryBindingEnvelope, payload)


def decode_conformance_execution_spec(
    payload: Mapping[str, Any],
) -> ConformanceExecutionSpec:
    return decode_typed_dataclass(
        ConformanceExecutionSpec,
        payload,
    )


def decode_logical_execution_attempt(
    payload: Mapping[str, Any],
) -> LogicalExecutionAttempt:
    return decode_typed_dataclass(
        LogicalExecutionAttempt,
        payload,
    )


def decode_evidence(
    payload: Mapping[str, Any],
) -> EvidenceRecord:
    return decode_typed_dataclass(EvidenceRecord, payload)


def decode_capability_result_envelope(
    payload: Mapping[str, Any],
) -> CapabilityResultEnvelope:
    return decode_typed_dataclass(CapabilityResultEnvelope, payload)


def decode_capability_result_receipt(
    payload: Mapping[str, Any],
) -> CapabilityResultReceipt:
    return decode_typed_dataclass(CapabilityResultReceipt, payload)


def decode_evidence_admission(
    payload: Mapping[str, Any],
) -> EvidenceAdmissionRecord:
    return decode_typed_dataclass(EvidenceAdmissionRecord, payload)


def decode_evidence_validity(
    payload: Mapping[str, Any],
) -> EvidenceValidityRecord:
    return decode_typed_dataclass(EvidenceValidityRecord, payload)


def decode_evidence_use_binding(
    payload: Mapping[str, Any],
) -> EvidenceUseBinding:
    return decode_typed_dataclass(EvidenceUseBinding, payload)


def decode_obligation_satisfaction(
    payload: Mapping[str, Any],
) -> ObligationSatisfactionRecord:
    return decode_typed_dataclass(
        ObligationSatisfactionRecord,
        payload,
    )


def decode_provisional_answer_candidate(
    payload: Mapping[str, Any],
) -> ProvisionalAnswerCandidate:
    return decode_typed_dataclass(ProvisionalAnswerCandidate, payload)


def decode_analysis_check_disposition(
    payload: Mapping[str, Any],
) -> AnalysisCheckDisposition:
    return decode_typed_dataclass(AnalysisCheckDisposition, payload)


def decode_claim_precheck(
    payload: Mapping[str, Any],
) -> ClaimPrecheckRecord:
    return decode_typed_dataclass(ClaimPrecheckRecord, payload)


def decode_answer(
    payload: Mapping[str, Any],
) -> AnswerVersion:
    return decode_typed_dataclass(AnswerVersion, payload)


def decode_settlement_precondition(
    payload: Mapping[str, Any],
) -> SettlementPreconditionReport:
    return decode_typed_dataclass(
        SettlementPreconditionReport,
        payload,
    )


def decode_workflow_snapshot(
    payload: Mapping[str, Any],
) -> WorkflowSnapshot:
    return decode_typed_dataclass(WorkflowSnapshot, payload)


def decode_workflow_application_receipt(
    payload: Mapping[str, Any],
) -> WorkflowApplicationReceipt:
    return decode_typed_dataclass(WorkflowApplicationReceipt, payload)


def decode_workflow_projection_head(
    payload: Mapping[str, Any],
) -> WorkflowProjectionHead:
    return decode_typed_dataclass(WorkflowProjectionHead, payload)


def decode_interpretation(payload: Mapping[str, Any]) -> InterpretationRecord:
    return InterpretationRecord(
        interpretation_id=payload["interpretation_id"],
        case_id=payload["case_id"],
        frame_revision_id=payload["frame_revision_id"],
        evidence_record_ids=tuple(payload["evidence_record_ids"]),
        evidence_admission_ids=tuple(
            payload["evidence_admission_ids"]
        ),
        evidence_validity_ids=tuple(
            payload["evidence_validity_ids"]
        ),
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
        accepted_message_binding_payload=payload[
            "accepted_message_binding_payload"
        ],
        active_frame_candidate_payload=payload[
            "active_frame_candidate_payload"
        ],
        latest_frame_review_payload=payload[
            "latest_frame_review_payload"
        ],
        available_measurement_resolution_payloads=tuple(
            payload["available_measurement_resolution_payloads"]
        ),
        available_evidence_obligation_payloads=tuple(
            payload["available_evidence_obligation_payloads"]
        ),
        accepted_plan_adoption_payload=payload[
            "accepted_plan_adoption_payload"
        ],
        accepted_query_binding_payloads=tuple(
            payload["accepted_query_binding_payloads"]
        ),
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
        authority_snapshot=AuthoritySnapshot(
            **payload["authority_snapshot"]
        ),
        authority_snapshot_sha256=payload[
            "authority_snapshot_sha256"
        ],
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
