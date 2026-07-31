"""Storage port for the Gate 1 authority contract."""

from __future__ import annotations

from datetime import datetime
from typing import ContextManager, Protocol

from waje_vnext.domain.async_runtime import (
    AuthoritySnapshot,
    JobLease,
    MailboxHead,
    MailboxMessage,
    MailboxMessageKind,
    OperationIdentity,
)
from waje_vnext.domain.authority import (
    AnalysisFrameRevision,
    AnswerVersion,
    CaseLifecycle,
    DecisionRecord,
    EvidenceRecord,
    InvestigationCase,
    InterpretationRecord,
    ReviewerObjection,
    WorkPlanRevision,
)
from waje_vnext.domain.context import ContextPacket
from waje_vnext.domain.controller import (
    ControllerLease,
    EffectAttemptRecord,
    PersistedAction,
    UserDecisionRequest,
)
from waje_vnext.domain.events import EventJournalEntry, JournalEventType
from waje_vnext.domain.measurement import (
    EvidenceValidityRecord,
    MeasurementResolutionOutcome,
    ObligationSatisfactionRecord,
    QuestionRevision,
    ResolvedEvidenceObligation,
    SettlementPreconditionReport,
)
from waje_vnext.domain.runtime_state import (
    ActionReceipt,
    CheckpointRecord,
    OutboxMessage,
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
from waje_vnext.domain.runtime_amendment import (
    DispatcherRecoveryCursor,
    DurableModelResult,
    FrameAdmissionProof,
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


class AuthorityStoreError(RuntimeError):
    pass


class AuthorityNotFound(AuthorityStoreError):
    pass


class AuthorityConflict(AuthorityStoreError):
    pass


class StaleHead(AuthorityStoreError):
    pass


class InvalidAuthorityTransition(AuthorityStoreError):
    pass


class LeaseConflict(AuthorityStoreError):
    pass


class LeaseFenceLost(AuthorityStoreError):
    pass


class AuthorityStore(Protocol):
    def atomic(self) -> ContextManager[None]: ...

    def open_case(
        self,
        *,
        case_id: str,
        thread_id: str,
        event_id: str,
        opened_at: datetime,
        operation: OperationIdentity | None = None,
    ) -> InvestigationCase: ...

    def get_case(self, case_id: str) -> InvestigationCase: ...

    def get_question(
        self,
        question_revision_id: str,
    ) -> QuestionRevision: ...

    def get_frame(self, frame_revision_id: str) -> AnalysisFrameRevision: ...

    def get_plan(self, plan_revision_id: str) -> WorkPlanRevision: ...

    def get_evidence(self, evidence_record_id: str) -> EvidenceRecord: ...

    def get_answer(self, answer_version_id: str) -> AnswerVersion: ...

    def list_evidence(self, case_id: str) -> tuple[EvidenceRecord, ...]: ...

    def list_decisions(self, case_id: str) -> tuple[DecisionRecord, ...]: ...

    def list_reviewer_objections(
        self,
        case_id: str,
    ) -> tuple[ReviewerObjection, ...]: ...

    def accept_question(
        self,
        question: QuestionRevision,
        *,
        expected_head_version: int,
        event_id: str,
        recorded_at: datetime,
        operation: OperationIdentity | None = None,
    ) -> InvestigationCase: ...

    def record_message_ingress(
        self,
        record: MessageIngressRecord,
    ) -> MessageIngressRecord: ...

    def list_message_ingress_records(
        self,
        case_id: str,
    ) -> tuple[MessageIngressRecord, ...]: ...

    def record_pending_user_message(
        self,
        record: PendingUserMessage,
    ) -> PendingUserMessage: ...

    def get_pending_user_message(
        self,
        pending_message_id: str,
    ) -> PendingUserMessage: ...

    def record_message_impact_binding(
        self,
        binding: MessageImpactBinding,
    ) -> MessageImpactBinding: ...

    def get_message_impact_binding(
        self,
        binding_id: str,
    ) -> MessageImpactBinding: ...

    def list_message_impact_bindings(
        self,
        case_id: str,
    ) -> tuple[MessageImpactBinding, ...]: ...

    def record_logical_model_job(
        self,
        record: LogicalModelJob,
    ) -> LogicalModelJob: ...

    def get_logical_model_job(
        self,
        logical_model_job_id: str,
    ) -> LogicalModelJob: ...

    def list_logical_model_jobs(
        self,
        case_id: str,
    ) -> tuple[LogicalModelJob, ...]: ...

    def record_provider_attempt_request(
        self,
        record: ProviderAttemptRequest,
    ) -> ProviderAttemptRequest: ...

    def get_provider_attempt_request(
        self,
        provider_attempt_id: str,
    ) -> ProviderAttemptRequest: ...

    def record_provider_attempt_receipt(
        self,
        record: ProviderAttemptReceipt,
    ) -> ProviderAttemptReceipt: ...

    def get_provider_attempt_receipt(
        self,
        provider_attempt_receipt_id: str,
    ) -> ProviderAttemptReceipt: ...

    def list_provider_attempt_receipts(
        self,
        logical_model_job_id: str,
    ) -> tuple[ProviderAttemptReceipt, ...]: ...

    def record_durable_model_result(
        self,
        record: DurableModelResult,
    ) -> DurableModelResult: ...

    def get_durable_model_result(
        self,
        logical_model_job_id: str,
    ) -> DurableModelResult | None: ...

    def record_obligation_schedule(
        self,
        record: ObligationScheduleRecord,
    ) -> ObligationScheduleRecord: ...

    def get_obligation_schedule(
        self,
        schedule_id: str,
    ) -> ObligationScheduleRecord: ...

    def record_obligation_dispatch(
        self,
        record: ObligationDispatchRecord,
    ) -> ObligationDispatchRecord: ...

    def list_obligation_dispatches(
        self,
        schedule_id: str,
    ) -> tuple[ObligationDispatchRecord, ...]: ...

    def record_obligation_completion(
        self,
        record: ObligationCompletionRecord,
    ) -> ObligationCompletionRecord: ...

    def list_obligation_completions(
        self,
        schedule_id: str,
    ) -> tuple[ObligationCompletionRecord, ...]: ...

    def record_obligation_schedule_checkpoint(
        self,
        record: ObligationScheduleCheckpoint,
    ) -> ObligationScheduleCheckpoint: ...

    def list_obligation_schedule_checkpoints(
        self,
        schedule_id: str,
    ) -> tuple[ObligationScheduleCheckpoint, ...]: ...

    def record_run_trace_manifest(
        self,
        record: RunTraceManifest,
    ) -> RunTraceManifest: ...

    def get_run_trace_manifest(
        self,
        trace_manifest_id: str,
    ) -> RunTraceManifest: ...

    def accept_frame(
        self,
        frame: AnalysisFrameRevision,
        *,
        frame_admission_proof_id: str,
        expected_head_version: int,
        event_id: str,
        recorded_at: datetime,
        operation: OperationIdentity | None = None,
    ) -> InvestigationCase: ...

    def record_frame_candidate(
        self,
        candidate: FrameCandidateRecord,
    ) -> FrameCandidateRecord: ...

    def get_frame_candidate(
        self,
        frame_candidate_id: str,
    ) -> FrameCandidateRecord: ...

    def get_active_frame_candidate(
        self,
        case_id: str,
    ) -> FrameCandidateRecord | None: ...

    def list_frame_candidates(
        self,
        case_id: str,
    ) -> tuple[FrameCandidateRecord, ...]: ...

    def supersede_active_frame_candidate(
        self,
        record: FrameCandidateSupersessionRecord,
    ) -> FrameCandidateSupersessionRecord: ...

    def list_frame_candidate_supersessions(
        self,
        case_id: str,
    ) -> tuple[FrameCandidateSupersessionRecord, ...]: ...

    def record_objection_closure(
        self,
        closure: ObjectionClosureRecord,
    ) -> ObjectionClosureRecord: ...

    def get_objection_closure(
        self,
        objection_closure_id: str,
    ) -> ObjectionClosureRecord: ...

    def record_frame_review(
        self,
        review: FrameReviewRecord,
    ) -> FrameReviewRecord: ...

    def get_frame_review(
        self,
        frame_review_id: str,
    ) -> FrameReviewRecord: ...

    def get_frame_review_for_candidate(
        self,
        frame_candidate_id: str,
    ) -> FrameReviewRecord | None: ...

    def list_frame_reviews(
        self,
        case_id: str,
    ) -> tuple[FrameReviewRecord, ...]: ...

    def record_frame_admission_proof(
        self,
        proof: FrameAdmissionProof,
    ) -> FrameAdmissionProof: ...

    def accept_plan(
        self,
        plan: WorkPlanRevision,
        *,
        expected_head_version: int,
        event_id: str,
        recorded_at: datetime,
        operation: OperationIdentity | None = None,
    ) -> InvestigationCase: ...

    def record_evidence(
        self,
        evidence: EvidenceRecord,
        *,
        expected_head_version: int,
        event_id: str,
        recorded_at: datetime,
    ) -> EvidenceRecord: ...

    def accept_answer(
        self,
        answer: AnswerVersion,
        *,
        expected_head_version: int,
        event_id: str,
        recorded_at: datetime,
        operation: OperationIdentity | None = None,
    ) -> InvestigationCase: ...

    def record_measurement_resolution(
        self,
        outcome: MeasurementResolutionOutcome,
        *,
        admission: MeasurementResolutionAdmission,
        expected_head_version: int,
        event_id: str,
    ) -> MeasurementResolutionOutcome: ...

    def get_measurement_resolution(
        self,
        resolution_outcome_id: str,
    ) -> MeasurementResolutionOutcome: ...

    def get_measurement_resolution_admission(
        self,
        resolution_outcome_id: str,
    ) -> MeasurementResolutionAdmission: ...

    def record_evidence_obligation(
        self,
        obligation: ResolvedEvidenceObligation,
        *,
        expected_head_version: int,
        event_id: str,
    ) -> ResolvedEvidenceObligation: ...

    def get_evidence_obligation(
        self,
        obligation_id: str,
    ) -> ResolvedEvidenceObligation: ...

    def record_evidence_validity(
        self,
        validity: EvidenceValidityRecord,
        *,
        event_id: str,
    ) -> EvidenceValidityRecord: ...

    def record_obligation_satisfaction(
        self,
        satisfaction: ObligationSatisfactionRecord,
        *,
        event_id: str,
    ) -> ObligationSatisfactionRecord: ...

    def record_settlement_precondition(
        self,
        report: SettlementPreconditionReport,
        *,
        expected_head_version: int,
        event_id: str,
    ) -> SettlementPreconditionReport: ...

    def transition_case_lifecycle(
        self,
        *,
        case_id: str,
        lifecycle: CaseLifecycle,
        expected_head_version: int,
        event_id: str,
        action_id: str,
        recorded_at: datetime,
        operation: OperationIdentity | None = None,
    ) -> InvestigationCase: ...

    def record_interpretation(
        self,
        interpretation: InterpretationRecord,
        *,
        event_id: str,
    ) -> InterpretationRecord: ...

    def record_decision(
        self,
        decision: DecisionRecord,
        *,
        event_id: str,
    ) -> DecisionRecord: ...

    def record_reviewer_objection(
        self,
        objection: ReviewerObjection,
        *,
        event_id: str,
    ) -> ReviewerObjection: ...

    def append_event(
        self,
        *,
        case_id: str,
        expected_next_cursor: int,
        event_id: str,
        event_type: JournalEventType,
        recorded_at: datetime,
        action_id: str | None,
        authority_ref: str | None,
        payload: dict[str, object],
        customer_projection: dict[str, object] | None,
        operation: OperationIdentity,
    ) -> EventJournalEntry: ...

    def append_mailbox_message(
        self,
        *,
        message_id: str,
        case_id: str,
        kind: MailboxMessageKind,
        operation: OperationIdentity,
        payload: dict[str, object],
        created_at: datetime,
    ) -> MailboxMessage: ...

    def get_mailbox_head(self, case_id: str) -> MailboxHead: ...

    def get_authority_snapshot(self, case_id: str) -> AuthoritySnapshot: ...

    def list_mailbox_messages(
        self,
        case_id: str,
        *,
        after_sequence: int = 0,
    ) -> tuple[MailboxMessage, ...]: ...

    def list_events(
        self,
        case_id: str,
        *,
        after_cursor: int = 0,
    ) -> tuple[EventJournalEntry, ...]: ...

    def record_action(
        self,
        action: PersistedAction,
    ) -> PersistedAction: ...

    def get_action(self, action_id: str) -> PersistedAction: ...

    def record_context_packet(
        self,
        packet: ContextPacket,
    ) -> ContextPacket: ...

    def get_context_packet(self, packet_id: str) -> ContextPacket: ...

    def record_action_receipt(
        self,
        receipt: ActionReceipt,
    ) -> ActionReceipt: ...

    def get_action_receipt(
        self,
        case_id: str,
        idempotency_key: str,
    ) -> ActionReceipt | None: ...

    def record_checkpoint(
        self,
        checkpoint: CheckpointRecord,
    ) -> CheckpointRecord: ...

    def latest_checkpoint(self, case_id: str) -> CheckpointRecord | None: ...

    def enqueue_outbox(self, message: OutboxMessage) -> OutboxMessage: ...

    def get_outbox_message(self, message_id: str) -> OutboxMessage: ...

    def list_outbox_messages(
        self,
        *,
        case_id: str | None = None,
    ) -> tuple[OutboxMessage, ...]: ...

    def list_pending_outbox_messages(
        self,
        *,
        case_id: str | None = None,
    ) -> tuple[OutboxMessage, ...]: ...

    def record_job_disposition(
        self,
        disposition: JobDispositionRecord,
    ) -> JobDispositionRecord: ...

    def get_job_disposition(
        self,
        outbox_message_id: str,
    ) -> JobDispositionRecord | None: ...

    def list_job_dispositions(
        self,
        case_id: str,
    ) -> tuple[JobDispositionRecord, ...]: ...

    def advance_dispatcher_recovery_cursor(
        self,
        cursor: DispatcherRecoveryCursor,
    ) -> DispatcherRecoveryCursor: ...

    def get_dispatcher_recovery_cursor(
        self,
        dispatcher_id: str,
    ) -> DispatcherRecoveryCursor | None: ...

    def acquire_job_lease(
        self,
        *,
        outbox_message_id: str,
        owner_id: str,
        now: datetime,
        expires_at: datetime,
    ) -> JobLease: ...

    def heartbeat_job_lease(
        self,
        lease: JobLease,
        *,
        heartbeat_at: datetime,
        expires_at: datetime,
    ) -> JobLease: ...

    def assert_job_lease(
        self,
        lease: JobLease,
        *,
        checked_at: datetime,
    ) -> JobLease: ...

    def release_job_lease(self, lease: JobLease) -> None: ...

    def record_decision_request(
        self,
        request: UserDecisionRequest,
    ) -> UserDecisionRequest: ...

    def get_decision_request(
        self,
        request_id: str,
    ) -> UserDecisionRequest: ...

    def record_effect_attempt(
        self,
        attempt: EffectAttemptRecord,
    ) -> EffectAttemptRecord: ...

    def list_effect_attempts(
        self,
        outbox_message_id: str,
    ) -> tuple[EffectAttemptRecord, ...]: ...

    def acquire_lease(
        self,
        *,
        case_id: str,
        run_id: str,
        owner_id: str,
        now: datetime,
        expires_at: datetime,
    ) -> ControllerLease: ...

    def release_lease(
        self,
        lease: ControllerLease,
    ) -> None: ...
