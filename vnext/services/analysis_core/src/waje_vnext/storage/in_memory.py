"""In-memory conformance adapter for the authority storage contract."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from threading import RLock
from typing import Any, Iterator, TypeVar

from waje_vnext.domain.actions import (
    ActionKind,
    AskUserPayload,
)
from waje_vnext.domain.async_runtime import (
    AsyncJobKind,
    AuthoritySnapshot,
    JobLease,
    MailboxHead,
    MailboxMessage,
    MailboxMessageKind,
    OperationIdentity,
)
from waje_vnext.domain.authority import (
    AnalysisFrameRevision,
    AnswerStatus,
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
from waje_vnext.domain.canonical import content_sha256
from waje_vnext.domain.controller import (
    ControllerLease,
    EffectAttemptRecord,
    EffectAttemptStatus,
    PersistedAction,
    UserDecisionRequest,
)
from waje_vnext.domain.events import EventJournalEntry, JournalEventType
from waje_vnext.domain.identity import (
    validate_frame_identities,
    validate_resolution_against_frame,
    validate_resolution_identities,
)
from waje_vnext.domain.measurement import (
    EvidenceValidityRecord,
    MeasurementResolutionOutcome,
    ObligationExecutionDisposition,
    ObligationSatisfactionRecord,
    QuestionRevision,
    ResolvedEvidenceObligation,
    ResolutionOutcomeKind,
    SettlementPreconditionReport,
)
from waje_vnext.domain.measurement_resolver import (
    MeasurementResolutionAdmission,
    TrustedResolutionInputVerifier,
    validate_executable_design,
)
from waje_vnext.domain.obligation_scheduler import (
    ObligationCompletionRecord,
    ObligationDispatchRecord,
    ObligationScheduleCheckpoint,
    ObligationScheduleRecord,
    ObligationTerminalStatus,
    same_obligation_business_authority,
    validate_persisted_obligation_completion,
)
from waje_vnext.domain.runtime_state import (
    ActionReceipt,
    CheckpointRecord,
    OutboxMessage,
)
from waje_vnext.domain.runtime_amendment import (
    DispatcherRecoveryCursor,
    DurableModelResult,
    FrameAdmissionProof,
    FrameCandidateRecord,
    FrameCandidateSupersessionRecord,
    FrameReviewDisposition,
    FrameReviewRecord,
    JobDisposition,
    JobDispositionRecord,
    LogicalModelJob,
    MessageImpactBinding,
    MessageIngressRecord,
    ObjectionClosureRecord,
    PendingUserMessage,
    ProviderAttemptDisposition,
    ProviderAttemptReceipt,
    ProviderAttemptRequest,
    RunTraceManifest,
    derive_changed_measurement_node_ids,
    measurement_paths_overlap,
)

from .ports import (
    AuthorityConflict,
    AuthorityNotFound,
    InvalidAuthorityTransition,
    LeaseConflict,
    LeaseFenceLost,
    StaleHead,
)
from .trace_validation import validate_run_trace_manifest_references


RecordT = TypeVar("RecordT")
_EFFECT_ACTION_KINDS = {
    ActionKind.INSPECT_SEMANTICS,
    ActionKind.RUN_PROBE,
    ActionKind.CALL_CAPABILITY,
    ActionKind.RUN_SENSITIVITY,
}
_ACTION_JOB_KINDS = {
    ActionKind.INSPECT_SEMANTICS: AsyncJobKind.SEMANTIC_INSPECTION,
    ActionKind.RUN_PROBE: AsyncJobKind.DATA_PROBE,
    ActionKind.CALL_CAPABILITY: AsyncJobKind.CAPABILITY,
    ActionKind.RUN_SENSITIVITY: AsyncJobKind.SENSITIVITY,
}


class InMemoryAuthorityStore:
    """Reference adapter used by contract tests and deterministic runtime tests."""

    def __init__(
        self,
        *,
        resolution_input_verifier: (
            TrustedResolutionInputVerifier | None
        ) = None,
    ) -> None:
        self._lock = RLock()
        self._resolution_input_verifier = resolution_input_verifier
        self._cases: dict[str, InvestigationCase] = {}
        self._questions: dict[str, QuestionRevision] = {}
        self._frames: dict[str, AnalysisFrameRevision] = {}
        self._plans: dict[str, WorkPlanRevision] = {}
        self._evidence: dict[str, EvidenceRecord] = {}
        self._answers: dict[str, AnswerVersion] = {}
        self._resolution_outcomes: dict[
            str,
            MeasurementResolutionOutcome,
        ] = {}
        self._resolution_admissions: dict[
            str,
            MeasurementResolutionAdmission,
        ] = {}
        self._evidence_obligations: dict[
            str,
            ResolvedEvidenceObligation,
        ] = {}
        self._evidence_validity: dict[str, EvidenceValidityRecord] = {}
        self._obligation_satisfaction: dict[
            str,
            ObligationSatisfactionRecord,
        ] = {}
        self._settlement_preconditions: dict[
            str,
            SettlementPreconditionReport,
        ] = {}
        self._interpretations: dict[str, InterpretationRecord] = {}
        self._decisions: dict[str, DecisionRecord] = {}
        self._objections: dict[str, ReviewerObjection] = {}
        self._actions: dict[str, PersistedAction] = {}
        self._action_idempotency_keys: dict[tuple[str, str], str] = {}
        self._contexts: dict[str, ContextPacket] = {}
        self._receipts: dict[tuple[str, str], ActionReceipt] = {}
        self._receipt_action_ids: dict[str, tuple[str, str]] = {}
        self._checkpoints: dict[str, CheckpointRecord] = {}
        self._checkpoint_event_keys: dict[tuple[str, int], str] = {}
        self._outbox: dict[str, OutboxMessage] = {}
        self._job_dispositions: dict[str, JobDispositionRecord] = {}
        self._dispatcher_recovery_cursors: dict[
            str,
            DispatcherRecoveryCursor,
        ] = {}
        self._message_ingress_records: dict[str, MessageIngressRecord] = {}
        self._pending_user_messages: dict[str, PendingUserMessage] = {}
        self._message_impact_bindings: dict[str, MessageImpactBinding] = {}
        self._logical_model_jobs: dict[str, LogicalModelJob] = {}
        self._provider_attempt_requests: dict[
            str,
            ProviderAttemptRequest,
        ] = {}
        self._provider_attempt_receipts: dict[
            str,
            ProviderAttemptReceipt,
        ] = {}
        self._durable_model_results: dict[str, DurableModelResult] = {}
        self._obligation_schedules: dict[
            str,
            ObligationScheduleRecord,
        ] = {}
        self._obligation_dispatch_records: dict[
            str,
            ObligationDispatchRecord,
        ] = {}
        self._obligation_completion_records: dict[
            str,
            ObligationCompletionRecord,
        ] = {}
        self._obligation_schedule_checkpoints: dict[
            str,
            ObligationScheduleCheckpoint,
        ] = {}
        self._run_trace_manifests: dict[str, RunTraceManifest] = {}
        self._decision_requests: dict[str, UserDecisionRequest] = {}
        self._decision_request_action_keys: dict[tuple[str, str], str] = {}
        self._effect_attempts: dict[str, EffectAttemptRecord] = {}
        self._leases: dict[str, ControllerLease] = {}
        self._lease_tokens: dict[str, int] = {}
        self._job_leases: dict[str, JobLease] = {}
        self._job_lease_tokens: dict[str, int] = {}
        self._frame_candidates: dict[str, FrameCandidateRecord] = {}
        self._active_frame_candidate_ids: dict[str, str] = {}
        self._frame_candidate_supersessions: dict[
            str,
            FrameCandidateSupersessionRecord,
        ] = {}
        self._frame_reviews: dict[str, FrameReviewRecord] = {}
        self._objection_closures: dict[str, ObjectionClosureRecord] = {}
        self._frame_admission_proofs: dict[str, FrameAdmissionProof] = {}
        self._frame_admission_proof_by_frame: dict[str, str] = {}
        self._events: dict[str, list[EventJournalEntry]] = {}
        self._events_by_id: dict[str, EventJournalEntry] = {}
        self._mailbox_heads: dict[str, MailboxHead] = {}
        self._mailbox_messages: dict[str, MailboxMessage] = {}

    @contextmanager
    def atomic(self) -> Iterator[None]:
        with self._lock:
            snapshot = self._snapshot()
            try:
                yield
            except BaseException:
                self._restore(snapshot)
                raise

    def open_case(
        self,
        *,
        case_id: str,
        thread_id: str,
        event_id: str,
        opened_at: datetime,
        operation: OperationIdentity | None = None,
    ) -> InvestigationCase:
        with self._lock:
            existing_event = self._events_by_id.get(event_id)
            if existing_event is not None:
                if (
                    existing_event.event_type is JournalEventType.CASE_OPENED
                    and existing_event.case_id == case_id
                    and existing_event.payload.get("thread_id") == thread_id
                ):
                    return self.get_case(case_id)
                raise AuthorityConflict("event ID already has different content")
            if case_id in self._cases:
                raise AuthorityConflict("case ID already exists")
            case = InvestigationCase(
                case_id=case_id,
                thread_id=thread_id,
                lifecycle=CaseLifecycle.OPEN,
                head_version=0,
                accepted_question_revision_id=None,
                accepted_frame_revision_id=None,
                accepted_plan_revision_id=None,
                accepted_answer_version_id=None,
                analysis_cycle_id="{}:cycle:0".format(case_id),
                opened_at=opened_at,
                updated_at=opened_at,
            )
            self._cases[case_id] = case
            self._events[case_id] = []
            self._mailbox_heads[case_id] = MailboxHead(
                case_id=case_id,
                last_sequence=0,
                authority_epoch=0,
                updated_at=opened_at,
            )
            self._append_event_locked(
                case_id=case_id,
                expected_next_cursor=1,
                event_id=event_id,
                event_type=JournalEventType.CASE_OPENED,
                recorded_at=opened_at,
                action_id=None,
                authority_ref=case_id,
                payload={"thread_id": thread_id},
                customer_projection={"state": "open"},
                operation=(
                    None
                    if operation is None
                    else _causal_event_operation(
                        causal_operation=operation,
                        event_id=event_id,
                        payload={"thread_id": thread_id},
                    )
                ),
            )
            return case

    def append_mailbox_message(
        self,
        *,
        message_id: str,
        case_id: str,
        kind: MailboxMessageKind,
        operation: OperationIdentity,
        payload: dict[str, object],
        created_at: datetime,
    ) -> MailboxMessage:
        with self._lock:
            self.get_case(case_id)
            existing = self._mailbox_messages.get(message_id)
            if existing is not None:
                if (
                    existing.case_id == case_id
                    and existing.kind is kind
                    and existing.operation == operation
                    and existing.payload == payload
                ):
                    return existing
                raise AuthorityConflict(
                    "mailbox message ID already has different content"
                )
            duplicate = next(
                (
                    candidate
                    for candidate in self._mailbox_messages.values()
                    if candidate.case_id == case_id
                    and candidate.operation.idempotency_key
                    == operation.idempotency_key
                ),
                None,
            )
            if duplicate is not None:
                if (
                    duplicate.kind is kind
                    and duplicate.operation == operation
                    and duplicate.payload == payload
                ):
                    return duplicate
                raise AuthorityConflict(
                    "mailbox idempotency key already has different content"
                )
            head = self._mailbox_heads[case_id]
            authority_epoch = head.authority_epoch + 1
            message = MailboxMessage(
                message_id=message_id,
                case_id=case_id,
                sequence=head.last_sequence + 1,
                authority_epoch=authority_epoch,
                kind=kind,
                operation=operation,
                payload=payload,
                created_at=created_at,
            )
            self._mailbox_messages[message_id] = message
            self._mailbox_heads[case_id] = MailboxHead(
                case_id=case_id,
                last_sequence=message.sequence,
                authority_epoch=authority_epoch,
                updated_at=created_at,
            )
            return message

    def get_mailbox_head(self, case_id: str) -> MailboxHead:
        with self._lock:
            self.get_case(case_id)
            return self._mailbox_heads[case_id]

    def get_authority_snapshot(self, case_id: str) -> AuthoritySnapshot:
        with self._lock:
            case = self.get_case(case_id)
            mailbox = self.get_mailbox_head(case_id)
            evidence_ids = {
                record.evidence_record_id
                for record in self._evidence.values()
                if record.case_id == case_id
            }
            obligation_ids = {
                record.obligation_id
                for record in self._evidence_obligations.values()
                if record.case_id == case_id
            }
            active_candidate = (
                None
                if case_id not in self._active_frame_candidate_ids
                else self._frame_candidates[
                    self._active_frame_candidate_ids[case_id]
                ]
            )
            return AuthoritySnapshot(
                case_id=case_id,
                head_version=case.head_version,
                mailbox_authority_epoch=mailbox.authority_epoch,
                accepted_question_revision_id=(
                    case.accepted_question_revision_id
                ),
                accepted_frame_revision_id=case.accepted_frame_revision_id,
                accepted_plan_revision_id=case.accepted_plan_revision_id,
                active_frame_candidate_generation=(
                    0
                    if active_candidate is None
                    else active_candidate.candidate_generation
                ),
                active_frame_candidate_sha256=(
                    None
                    if active_candidate is None
                    else active_candidate.proposed_frame_content_sha256
                ),
                obligation_state_version=(
                    len(obligation_ids)
                    + sum(
                        1
                        for item in self._obligation_satisfaction.values()
                        if item.obligation_id in obligation_ids
                    )
                ),
                evidence_admission_state_version=(
                    len(evidence_ids)
                    + sum(
                        1
                        for item in self._evidence_validity.values()
                        if item.evidence_record_id in evidence_ids
                    )
                ),
                contradiction_state_version=sum(
                    1
                    for item in self._objections.values()
                    if item.case_id == case_id
                ),
            )

    def list_mailbox_messages(
        self,
        case_id: str,
        *,
        after_sequence: int = 0,
    ) -> tuple[MailboxMessage, ...]:
        with self._lock:
            self.get_case(case_id)
            return tuple(
                sorted(
                    (
                        message
                        for message in self._mailbox_messages.values()
                        if message.case_id == case_id
                        and message.sequence > after_sequence
                    ),
                    key=lambda message: message.sequence,
                )
            )

    def record_message_ingress(
        self,
        record: MessageIngressRecord,
    ) -> MessageIngressRecord:
        with self._lock:
            message = _get(
                self._mailbox_messages,
                record.message_id,
                "mailbox message",
            )
            if (
                message.case_id != record.case_id
                or message.sequence != record.mailbox_sequence
                or message.authority_epoch != record.authority_epoch
                or message.operation != record.operation
                or message.operation.payload_sha256
                != record.message_payload_sha256
            ):
                raise InvalidAuthorityTransition(
                    "message ingress record does not bind mailbox authority"
                )
            _put_idempotent_immutable(
                self._message_ingress_records,
                record.ingress_record_id,
                record,
                "message ingress record",
            )
            return record

    def list_message_ingress_records(
        self,
        case_id: str,
    ) -> tuple[MessageIngressRecord, ...]:
        with self._lock:
            self.get_case(case_id)
            return tuple(
                sorted(
                    (
                        item
                        for item in self._message_ingress_records.values()
                        if item.case_id == case_id
                    ),
                    key=lambda item: item.mailbox_sequence,
                )
            )

    def record_pending_user_message(
        self,
        record: PendingUserMessage,
    ) -> PendingUserMessage:
        with self._lock:
            ingress = _get(
                self._message_ingress_records,
                record.ingress_record_id,
                "message ingress record",
            )
            if (
                ingress.case_id != record.case_id
                or ingress.message_id != record.message_id
                or ingress.authority_epoch != record.authority_epoch
                or ingress.operation.operation_id
                != record.source_operation_id
            ):
                raise InvalidAuthorityTransition(
                    "pending message does not bind ingress record"
                )
            _put_idempotent_immutable(
                self._pending_user_messages,
                record.pending_message_id,
                record,
                "pending user message",
            )
            return record

    def get_pending_user_message(
        self,
        pending_message_id: str,
    ) -> PendingUserMessage:
        with self._lock:
            return _get(
                self._pending_user_messages,
                pending_message_id,
                "pending user message",
            )

    def record_message_impact_binding(
        self,
        binding: MessageImpactBinding,
    ) -> MessageImpactBinding:
        with self._lock:
            pending = _get(
                self._pending_user_messages,
                binding.pending_message_id,
                "pending user message",
            )
            message = _get(
                self._mailbox_messages,
                pending.message_id,
                "mailbox message",
            )
            if (
                binding.case_id != pending.case_id
                or binding.message_id != pending.message_id
                or binding.authority_epoch != pending.authority_epoch
                or binding.source_payload_sha256
                != message.operation.payload_sha256
                or binding.logical_model_job_id
                != pending.binding_job_id
            ):
                raise InvalidAuthorityTransition(
                    "message impact binding is stale or misbound"
                )
            prior = next(
                (
                    item
                    for item in self._message_impact_bindings.values()
                    if item.pending_message_id
                    == binding.pending_message_id
                ),
                None,
            )
            if prior is not None and prior != binding:
                raise AuthorityConflict(
                    "pending message already has another binding"
                )
            _put_idempotent_immutable(
                self._message_impact_bindings,
                binding.binding_id,
                binding,
                "message impact binding",
            )
            return binding

    def get_message_impact_binding(
        self,
        binding_id: str,
    ) -> MessageImpactBinding:
        with self._lock:
            return _get(
                self._message_impact_bindings,
                binding_id,
                "message impact binding",
            )

    def list_message_impact_bindings(
        self,
        case_id: str,
    ) -> tuple[MessageImpactBinding, ...]:
        with self._lock:
            self.get_case(case_id)
            return tuple(
                sorted(
                    (
                        item
                        for item in self._message_impact_bindings.values()
                        if item.case_id == case_id
                    ),
                    key=lambda item: (
                        item.authority_epoch,
                        item.binding_id,
                    ),
                )
            )

    def record_logical_model_job(
        self,
        record: LogicalModelJob,
    ) -> LogicalModelJob:
        with self._lock:
            self.get_case(record.case_id)
            message = self.get_outbox_message(record.job_id)
            if (
                message.case_id != record.case_id
                or message.operation.operation_id
                != record.operation_id
                or message.authority_snapshot_sha256
                != record.authority_snapshot_sha256
            ):
                raise InvalidAuthorityTransition(
                    "logical model job does not bind its outbox authority"
                )
            prior = next(
                (
                    item
                    for item in self._logical_model_jobs.values()
                    if item.job_id == record.job_id
                ),
                None,
            )
            if prior is not None and prior != record:
                raise AuthorityConflict(
                    "outbox job already has another logical model job"
                )
            _put_idempotent_immutable(
                self._logical_model_jobs,
                record.logical_model_job_id,
                record,
                "logical model job",
            )
            return record

    def get_logical_model_job(
        self,
        logical_model_job_id: str,
    ) -> LogicalModelJob:
        with self._lock:
            return _get(
                self._logical_model_jobs,
                logical_model_job_id,
                "logical model job",
            )

    def list_logical_model_jobs(
        self,
        case_id: str,
    ) -> tuple[LogicalModelJob, ...]:
        with self._lock:
            self.get_case(case_id)
            return tuple(
                sorted(
                    (
                        item
                        for item in self._logical_model_jobs.values()
                        if item.case_id == case_id
                    ),
                    key=lambda item: (
                        item.created_at,
                        item.logical_model_job_id,
                    ),
                )
            )

    def record_provider_attempt_request(
        self,
        record: ProviderAttemptRequest,
    ) -> ProviderAttemptRequest:
        with self._lock:
            self.get_logical_model_job(record.logical_model_job_id)
            prior_attempts = tuple(
                item
                for item in self._provider_attempt_requests.values()
                if item.logical_model_job_id
                == record.logical_model_job_id
            )
            expected_number = len(prior_attempts) + 1
            if record.attempt_number != expected_number:
                existing = self._provider_attempt_requests.get(
                    record.provider_attempt_id
                )
                if existing == record:
                    return existing
                raise InvalidAuthorityTransition(
                    "provider attempt does not extend logical job history"
                )
            expected_prior = (
                None
                if not prior_attempts
                else max(
                    prior_attempts,
                    key=lambda item: item.attempt_number,
                ).provider_attempt_id
            )
            if record.prior_provider_attempt_id != expected_prior:
                raise InvalidAuthorityTransition(
                    "provider attempt prior identity is stale"
                )
            _put_idempotent_immutable(
                self._provider_attempt_requests,
                record.provider_attempt_id,
                record,
                "provider attempt request",
            )
            return record

    def record_provider_attempt_receipt(
        self,
        record: ProviderAttemptReceipt,
    ) -> ProviderAttemptReceipt:
        with self._lock:
            request = _get(
                self._provider_attempt_requests,
                record.provider_attempt_id,
                "provider attempt request",
            )
            if (
                request.logical_model_job_id
                != record.logical_model_job_id
            ):
                raise InvalidAuthorityTransition(
                    "provider receipt belongs to another logical job"
                )
            prior = next(
                (
                    item
                    for item in self._provider_attempt_receipts.values()
                    if item.provider_attempt_id
                    == record.provider_attempt_id
                ),
                None,
            )
            if prior is not None and prior != record:
                raise AuthorityConflict(
                    "provider attempt already has another receipt"
                )
            _put_idempotent_immutable(
                self._provider_attempt_receipts,
                record.provider_attempt_receipt_id,
                record,
                "provider attempt receipt",
            )
            return record

    def get_provider_attempt_request(
        self,
        provider_attempt_id: str,
    ) -> ProviderAttemptRequest:
        with self._lock:
            return _get(
                self._provider_attempt_requests,
                provider_attempt_id,
                "provider attempt request",
            )

    def get_provider_attempt_receipt(
        self,
        provider_attempt_receipt_id: str,
    ) -> ProviderAttemptReceipt:
        with self._lock:
            return _get(
                self._provider_attempt_receipts,
                provider_attempt_receipt_id,
                "provider attempt receipt",
            )

    def list_provider_attempt_receipts(
        self,
        logical_model_job_id: str,
    ) -> tuple[ProviderAttemptReceipt, ...]:
        with self._lock:
            self.get_logical_model_job(logical_model_job_id)
            request_by_id = self._provider_attempt_requests
            return tuple(
                sorted(
                    (
                        item
                        for item
                        in self._provider_attempt_receipts.values()
                        if item.logical_model_job_id
                        == logical_model_job_id
                    ),
                    key=lambda item: request_by_id[
                        item.provider_attempt_id
                    ].attempt_number,
                )
            )

    def record_durable_model_result(
        self,
        record: DurableModelResult,
    ) -> DurableModelResult:
        with self._lock:
            self.get_logical_model_job(record.logical_model_job_id)
            request = self.get_provider_attempt_request(
                record.provider_attempt_id
            )
            receipt = next(
                (
                    item
                    for item in self._provider_attempt_receipts.values()
                    if item.provider_attempt_id
                    == record.provider_attempt_id
                ),
                None,
            )
            if (
                request.logical_model_job_id
                != record.logical_model_job_id
                or receipt is None
                or receipt.disposition
                is not ProviderAttemptDisposition.SUCCEEDED
                or receipt.output_sha256 != record.output_sha256
            ):
                raise InvalidAuthorityTransition(
                    "durable model result lacks its successful attempt"
                )
            prior = self._durable_model_results.get(
                record.logical_model_job_id
            )
            if prior is not None:
                if prior == record:
                    return prior
                raise AuthorityConflict(
                    "logical model job already has a different result"
                )
            _put_idempotent_immutable(
                self._durable_model_results,
                record.logical_model_job_id,
                record,
                "durable model result",
            )
            return record

    def get_durable_model_result(
        self,
        logical_model_job_id: str,
    ) -> DurableModelResult | None:
        with self._lock:
            self.get_logical_model_job(logical_model_job_id)
            return self._durable_model_results.get(logical_model_job_id)

    def record_obligation_schedule(
        self,
        record: ObligationScheduleRecord,
    ) -> ObligationScheduleRecord:
        with self._lock:
            if self.get_authority_snapshot(record.case_id) != (
                record.authority_snapshot
            ):
                raise InvalidAuthorityTransition(
                    "obligation schedule authority is stale"
                )
            _put_idempotent_immutable(
                self._obligation_schedules,
                record.schedule_id,
                record,
                "obligation schedule",
            )
            return record

    def get_obligation_schedule(
        self,
        schedule_id: str,
    ) -> ObligationScheduleRecord:
        with self._lock:
            return _get(
                self._obligation_schedules,
                schedule_id,
                "obligation schedule",
            )

    def record_obligation_dispatch(
        self,
        record: ObligationDispatchRecord,
    ) -> ObligationDispatchRecord:
        with self._lock:
            schedule = self.get_obligation_schedule(record.schedule_id)
            obligation_ids = {
                item.obligation_id for item in schedule.obligations
            }
            message = self.get_outbox_message(
                record.outbox_message_id
            )
            if (
                record.dispatch.obligation_id not in obligation_ids
                or record.dispatch.authority_snapshot
                != schedule.authority_snapshot
                or message.outbox_message_id
                != record.outbox_message_id
                or message.job_kind is not AsyncJobKind.OBLIGATION
                or str(message.payload.get("schedule_id", ""))
                != record.schedule_id
                or str(message.payload.get("obligation_id", ""))
                != record.dispatch.obligation_id
            ):
                raise InvalidAuthorityTransition(
                    "obligation dispatch does not bind schedule outbox"
                )
            _put_idempotent_immutable(
                self._obligation_dispatch_records,
                record.dispatch_record_id,
                record,
                "obligation dispatch",
            )
            return record

    def list_obligation_dispatches(
        self,
        schedule_id: str,
    ) -> tuple[ObligationDispatchRecord, ...]:
        with self._lock:
            self.get_obligation_schedule(schedule_id)
            return tuple(
                sorted(
                    (
                        item
                        for item
                        in self._obligation_dispatch_records.values()
                        if item.schedule_id == schedule_id
                    ),
                    key=lambda item: item.dispatch.obligation_id,
                )
            )

    def record_obligation_completion(
        self,
        record: ObligationCompletionRecord,
    ) -> ObligationCompletionRecord:
        with self._lock:
            schedule = self.get_obligation_schedule(record.schedule_id)
            current = self.get_authority_snapshot(schedule.case_id)
            current_hash_matches = (
                current.content_sha256
                == record.admitted_authority_snapshot_sha256
            )
            superseded_under_drift = (
                record.completion.status
                is ObligationTerminalStatus.SUPERSEDED
                and not same_obligation_business_authority(
                    schedule.authority_snapshot,
                    current,
                )
            )
            if (
                record.completion.status
                is ObligationTerminalStatus.SUPERSEDED
                and not superseded_under_drift
            ):
                raise InvalidAuthorityTransition(
                    "obligation cannot be superseded without authority drift"
                )
            if not current_hash_matches or (
                not same_obligation_business_authority(
                    schedule.authority_snapshot,
                    current,
                )
                and not superseded_under_drift
            ):
                raise InvalidAuthorityTransition(
                    "obligation completion authority is stale"
                )
            obligation = next(
                (
                    item
                    for item in schedule.obligations
                    if item.obligation_id
                    == record.completion.obligation_id
                ),
                None,
            )
            dispatch = next(
                (
                    item
                    for item in self.list_obligation_dispatches(
                        record.schedule_id
                    )
                    if item.dispatch.dispatch_id
                    == record.completion.dispatch_id
                    and item.dispatch.obligation_id
                    == record.completion.obligation_id
                ),
                None,
            )
            prior = next(
                (
                    item
                    for item
                    in self._obligation_completion_records.values()
                    if item.schedule_id == record.schedule_id
                    and item.completion.obligation_id
                    == record.completion.obligation_id
                ),
                None,
            )
            if prior is not None:
                if prior == record:
                    return prior
                raise AuthorityConflict(
                    "obligation already has another completion"
                )
            prior_completions = tuple(
                item.completion
                for item in self._obligation_completion_records.values()
                if item.schedule_id == record.schedule_id
            )
            try:
                validate_persisted_obligation_completion(
                    schedule=schedule,
                    completion=record.completion,
                    prior_completions=prior_completions,
                    dispatch=(
                        None if dispatch is None else dispatch.dispatch
                    ),
                    current_authority=current,
                )
            except ValueError as error:
                raise InvalidAuthorityTransition(str(error)) from error
            _put_idempotent_immutable(
                self._obligation_completion_records,
                record.completion_record_id,
                record,
                "obligation completion",
            )
            return record

    def list_obligation_completions(
        self,
        schedule_id: str,
    ) -> tuple[ObligationCompletionRecord, ...]:
        with self._lock:
            self.get_obligation_schedule(schedule_id)
            return tuple(
                sorted(
                    (
                        item
                        for item
                        in self._obligation_completion_records.values()
                        if item.schedule_id == schedule_id
                    ),
                    key=lambda item: item.completion.obligation_id,
                )
            )

    def record_obligation_schedule_checkpoint(
        self,
        record: ObligationScheduleCheckpoint,
    ) -> ObligationScheduleCheckpoint:
        with self._lock:
            schedule = self.get_obligation_schedule(record.schedule_id)
            checkpoints = self.list_obligation_schedule_checkpoints(
                record.schedule_id
            )
            expected_number = len(checkpoints) + 1
            expected_prior = (
                None if not checkpoints else checkpoints[-1].checkpoint_id
            )
            dispatched_ids = {
                item.dispatch.obligation_id
                for item in self.list_obligation_dispatches(
                    record.schedule_id
                )
            }
            completed_ids = {
                item.completion.obligation_id
                for item in self.list_obligation_completions(
                    record.schedule_id
                )
            }
            expected_dispatched = tuple(
                sorted(dispatched_ids - completed_ids)
            )
            expected_completed = tuple(sorted(completed_ids))
            expected_pending = tuple(
                sorted(
                    {
                        item.obligation_id
                        for item in schedule.obligations
                    }
                    - dispatched_ids
                    - completed_ids
                )
            )
            if (
                record.checkpoint_number != expected_number
                or record.prior_checkpoint_id != expected_prior
                or record.schedule_sha256 != schedule.content_sha256
                or record.authority_snapshot_sha256
                != schedule.authority_snapshot_sha256
                or record.dispatched_obligation_ids
                != expected_dispatched
                or record.completed_obligation_ids
                != expected_completed
                or record.pending_obligation_ids != expected_pending
            ):
                raise InvalidAuthorityTransition(
                    "obligation checkpoint is not a state derivation"
                )
            _put_idempotent_immutable(
                self._obligation_schedule_checkpoints,
                record.checkpoint_id,
                record,
                "obligation schedule checkpoint",
            )
            return record

    def list_obligation_schedule_checkpoints(
        self,
        schedule_id: str,
    ) -> tuple[ObligationScheduleCheckpoint, ...]:
        with self._lock:
            self.get_obligation_schedule(schedule_id)
            return tuple(
                sorted(
                    (
                        item
                        for item
                        in self._obligation_schedule_checkpoints.values()
                        if item.schedule_id == schedule_id
                    ),
                    key=lambda item: item.checkpoint_number,
                )
            )

    def record_run_trace_manifest(
        self,
        record: RunTraceManifest,
    ) -> RunTraceManifest:
        with self._lock:
            self.get_case(record.case_id)
            validate_run_trace_manifest_references(self, record)
            _put_idempotent_immutable(
                self._run_trace_manifests,
                record.trace_manifest_id,
                record,
                "run trace manifest",
            )
            return record

    def get_run_trace_manifest(
        self,
        trace_manifest_id: str,
    ) -> RunTraceManifest:
        with self._lock:
            return _get(
                self._run_trace_manifests,
                trace_manifest_id,
                "run trace manifest",
            )

    def get_case(self, case_id: str) -> InvestigationCase:
        with self._lock:
            return _get(self._cases, case_id, "case")

    def get_frame(self, frame_revision_id: str) -> AnalysisFrameRevision:
        with self._lock:
            return _get(self._frames, frame_revision_id, "frame")

    def get_question(
        self,
        question_revision_id: str,
    ) -> QuestionRevision:
        with self._lock:
            return _get(
                self._questions,
                question_revision_id,
                "question",
            )

    def get_plan(self, plan_revision_id: str) -> WorkPlanRevision:
        with self._lock:
            return _get(self._plans, plan_revision_id, "plan")

    def get_evidence(self, evidence_record_id: str) -> EvidenceRecord:
        with self._lock:
            return _get(self._evidence, evidence_record_id, "evidence")

    def get_answer(self, answer_version_id: str) -> AnswerVersion:
        with self._lock:
            return _get(self._answers, answer_version_id, "answer")

    def list_evidence(self, case_id: str) -> tuple[EvidenceRecord, ...]:
        with self._lock:
            self.get_case(case_id)
            return tuple(
                sorted(
                    (
                        record
                        for record in self._evidence.values()
                        if record.case_id == case_id
                    ),
                    key=lambda record: (
                        record.created_at,
                        record.evidence_record_id,
                    ),
                )
            )

    def list_decisions(self, case_id: str) -> tuple[DecisionRecord, ...]:
        with self._lock:
            self.get_case(case_id)
            return tuple(
                sorted(
                    (
                        record
                        for record in self._decisions.values()
                        if record.case_id == case_id
                    ),
                    key=lambda record: (
                        record.created_at,
                        record.decision_record_id,
                    ),
                )
            )

    def list_reviewer_objections(
        self,
        case_id: str,
    ) -> tuple[ReviewerObjection, ...]:
        with self._lock:
            self.get_case(case_id)
            return tuple(
                sorted(
                    (
                        record
                        for record in self._objections.values()
                        if record.case_id == case_id
                    ),
                    key=lambda record: (
                        record.objection_key,
                        record.revision_number,
                    ),
                )
            )

    def accept_question(
        self,
        question: QuestionRevision,
        *,
        expected_head_version: int,
        event_id: str,
        recorded_at: datetime,
        operation: OperationIdentity | None = None,
    ) -> InvestigationCase:
        with self._lock:
            idempotent = self._idempotent_head_result(
                event_id,
                JournalEventType.QUESTION_ACCEPTED,
                question.question_revision_id,
                question.case_id,
            )
            if idempotent is not None:
                return idempotent
            case = self._cas_case(
                question.case_id,
                expected_head_version,
            )
            if question.acceptance_event_id != event_id:
                raise InvalidAuthorityTransition(
                    "question must bind its acceptance event"
                )
            current = (
                self._questions.get(
                    case.accepted_question_revision_id
                )
                if case.accepted_question_revision_id
                else None
            )
            expected_revision = (
                1 if current is None else current.revision_number + 1
            )
            expected_prior = (
                None
                if current is None
                else current.question_revision_id
            )
            if (
                question.revision_number != expected_revision
                or question.prior_question_revision_id != expected_prior
            ):
                raise InvalidAuthorityTransition(
                    "question revision does not extend the accepted question"
                )
            if question.accepted_head_version != case.head_version + 1:
                raise InvalidAuthorityTransition(
                    "question accepted_head_version is stale"
                )
            for source in question.source_messages:
                message = self._mailbox_messages.get(source.message_id)
                if message is None or message.case_id != question.case_id:
                    raise InvalidAuthorityTransition(
                        "question source message is unavailable"
                    )
                if (
                    message.sequence != source.sequence
                    or content_sha256(
                        str(message.payload.get("message", ""))
                    )
                    != source.content_sha256
                ):
                    raise InvalidAuthorityTransition(
                        "question source message does not match mailbox"
                    )
            _put_immutable(
                self._questions,
                question.question_revision_id,
                question,
                "question",
            )
            updated = replace(
                case,
                head_version=case.head_version + 1,
                accepted_question_revision_id=(
                    question.question_revision_id
                ),
                accepted_frame_revision_id=None,
                accepted_plan_revision_id=None,
                accepted_answer_version_id=None,
                analysis_cycle_id=question.analysis_cycle_id,
                updated_at=recorded_at,
            )
            self._cases[case.case_id] = updated
            self._append_authority_event_locked(
                case_id=case.case_id,
                event_id=event_id,
                event_type=JournalEventType.QUESTION_ACCEPTED,
                authority_ref=question.question_revision_id,
                action_id=None,
                recorded_at=recorded_at,
                payload={
                    "revision_number": question.revision_number,
                    "content_sha256": question.content_sha256,
                    "analysis_cycle_id": question.analysis_cycle_id,
                    "head_version": updated.head_version,
                },
                operation=operation,
            )
            return updated

    def accept_frame(
        self,
        frame: AnalysisFrameRevision,
        *,
        frame_admission_proof_id: str,
        expected_head_version: int,
        event_id: str,
        recorded_at: datetime,
        operation: OperationIdentity | None = None,
    ) -> InvestigationCase:
        with self._lock:
            idempotent = self._idempotent_head_result(
                event_id,
                JournalEventType.FRAME_ACCEPTED,
                frame.frame_revision_id,
                frame.case_id,
            )
            if idempotent is not None:
                return idempotent
            case = self._cas_case(frame.case_id, expected_head_version)
            proof = _get(
                self._frame_admission_proofs,
                frame_admission_proof_id,
                "frame admission proof",
            )
            if (
                proof.case_id != frame.case_id
                or proof.frame_revision_id != frame.frame_revision_id
                or proof.frame_content_sha256 != frame.content_sha256
            ):
                raise InvalidAuthorityTransition(
                    "frame admission proof does not bind this Frame"
                )
            if proof.authority_snapshot != self.get_authority_snapshot(
                frame.case_id
            ):
                raise InvalidAuthorityTransition(
                    "frame admission proof authority snapshot is stale"
                )
            if (
                frame.question_revision_id
                != case.accepted_question_revision_id
            ):
                raise InvalidAuthorityTransition(
                    "frame must bind the accepted question"
                )
            question = self.get_question(frame.question_revision_id)
            try:
                question.validate_spans(
                    frame.measurement_design.question_grounding.source_spans
                )
                validate_frame_identities(question, frame)
                findings = validate_executable_design(
                    frame.measurement_design
                )
                if findings:
                    raise ValueError(
                        "measurement design is not executable: {}".format(
                            ",".join(
                                sorted({item.code for item in findings})
                            )
                        )
                    )
            except ValueError as error:
                raise InvalidAuthorityTransition(str(error)) from error
            current = (
                self._frames.get(case.accepted_frame_revision_id)
                if case.accepted_frame_revision_id
                else None
            )
            expected_revision = 1 if current is None else current.revision_number + 1
            expected_prior = None if current is None else current.frame_revision_id
            if (
                frame.revision_number != expected_revision
                or frame.prior_frame_revision_id != expected_prior
            ):
                raise InvalidAuthorityTransition(
                    "frame revision does not extend the accepted frame"
                )
            for decision_id in (
                frame.measurement_design.question_grounding.decision_record_ids
            ):
                decision = _get(
                    self._decisions,
                    decision_id,
                    "decision",
                )
                if decision.case_id != frame.case_id:
                    raise InvalidAuthorityTransition(
                        "frame decision belongs to another case"
                    )
            _put_immutable(self._frames, frame.frame_revision_id, frame, "frame")
            updated = replace(
                case,
                head_version=case.head_version + 1,
                accepted_frame_revision_id=frame.frame_revision_id,
                accepted_plan_revision_id=None,
                accepted_answer_version_id=None,
                updated_at=recorded_at,
            )
            self._cases[case.case_id] = updated
            self._append_authority_event_locked(
                case_id=case.case_id,
                event_id=event_id,
                event_type=JournalEventType.FRAME_ACCEPTED,
                authority_ref=frame.frame_revision_id,
                action_id=frame.created_by_action_id,
                recorded_at=recorded_at,
                payload={
                    "revision_number": frame.revision_number,
                    "content_sha256": frame.content_sha256,
                    "head_version": updated.head_version,
                },
                operation=operation,
            )
            return updated

    def record_frame_candidate(
        self,
        candidate: FrameCandidateRecord,
    ) -> FrameCandidateRecord:
        with self._lock:
            case = self.get_case(candidate.case_id)
            if (
                candidate.question_revision_id
                != case.accepted_question_revision_id
                or candidate.proposed_frame.question_revision_id
                != case.accepted_question_revision_id
            ):
                raise InvalidAuthorityTransition(
                    "frame candidate must bind the accepted question"
                )
            existing = self._frame_candidates.get(
                candidate.frame_candidate_id
            )
            if existing is not None:
                if existing == candidate:
                    return existing
                raise AuthorityConflict(
                    "frame candidate ID has different content"
                )
            prior = (
                None
                if candidate.case_id
                not in self._active_frame_candidate_ids
                else self._frame_candidates[
                    self._active_frame_candidate_ids[candidate.case_id]
                ]
            )
            expected_generation = (
                1 if prior is None else prior.candidate_generation + 1
            )
            expected_prior = (
                None if prior is None else prior.frame_candidate_id
            )
            if (
                candidate.candidate_generation != expected_generation
                or candidate.prior_frame_candidate_id != expected_prior
            ):
                raise InvalidAuthorityTransition(
                    "frame candidate does not extend the active candidate"
                )
            if prior is not None:
                prior_review = next(
                    (
                        item
                        for item in self._frame_reviews.values()
                        if item.frame_candidate_id
                        == prior.frame_candidate_id
                    ),
                    None,
                )
                if prior_review is None:
                    raise InvalidAuthorityTransition(
                        "replacement candidate requires prior review"
                    )
                required_closures = {
                    item.objection_id
                    for item in prior_review.objections
                    if (
                        prior_review.disposition
                        is not FrameReviewDisposition.ACCEPT
                    )
                }
                if (
                    set(candidate.addressed_objection_ids)
                    != required_closures
                ):
                    raise InvalidAuthorityTransition(
                        "replacement candidate must address every prior objection"
                    )
            self._frame_candidates[candidate.frame_candidate_id] = candidate
            self._active_frame_candidate_ids[candidate.case_id] = (
                candidate.frame_candidate_id
            )
            return candidate

    def get_frame_candidate(
        self,
        frame_candidate_id: str,
    ) -> FrameCandidateRecord:
        with self._lock:
            return _get(
                self._frame_candidates,
                frame_candidate_id,
                "frame candidate",
            )

    def get_active_frame_candidate(
        self,
        case_id: str,
    ) -> FrameCandidateRecord | None:
        with self._lock:
            self.get_case(case_id)
            candidate_id = self._active_frame_candidate_ids.get(case_id)
            return (
                None
                if candidate_id is None
                else self._frame_candidates[candidate_id]
            )

    def list_frame_candidates(
        self,
        case_id: str,
    ) -> tuple[FrameCandidateRecord, ...]:
        with self._lock:
            self.get_case(case_id)
            return tuple(
                sorted(
                    (
                        item
                        for item in self._frame_candidates.values()
                        if item.case_id == case_id
                    ),
                    key=lambda item: item.candidate_generation,
                )
            )

    def supersede_active_frame_candidate(
        self,
        record: FrameCandidateSupersessionRecord,
    ) -> FrameCandidateSupersessionRecord:
        with self._lock:
            active_id = self._active_frame_candidate_ids.get(
                record.case_id
            )
            if active_id != record.frame_candidate_id:
                existing = self._frame_candidate_supersessions.get(
                    record.supersession_record_id
                )
                if existing == record:
                    return existing
                raise InvalidAuthorityTransition(
                    "frame candidate supersession does not target active head"
                )
            candidate = self.get_frame_candidate(
                record.frame_candidate_id
            )
            question = self.get_question(
                record.superseded_by_question_revision_id
            )
            if (
                question.case_id != record.case_id
                or candidate.question_revision_id
                == question.question_revision_id
                or record.authority_epoch
                != self.get_mailbox_head(record.case_id).authority_epoch
            ):
                raise InvalidAuthorityTransition(
                    "frame candidate supersession authority is invalid"
                )
            _put_idempotent_immutable(
                self._frame_candidate_supersessions,
                record.supersession_record_id,
                record,
                "frame candidate supersession",
            )
            del self._active_frame_candidate_ids[record.case_id]
            return record

    def list_frame_candidate_supersessions(
        self,
        case_id: str,
    ) -> tuple[FrameCandidateSupersessionRecord, ...]:
        with self._lock:
            self.get_case(case_id)
            return tuple(
                sorted(
                    (
                        item
                        for item
                        in self._frame_candidate_supersessions.values()
                        if item.case_id == case_id
                    ),
                    key=lambda item: (
                        item.created_at,
                        item.supersession_record_id,
                    ),
                )
            )

    def record_objection_closure(
        self,
        closure: ObjectionClosureRecord,
    ) -> ObjectionClosureRecord:
        with self._lock:
            source = _get(
                self._frame_candidates,
                closure.source_frame_candidate_id,
                "source frame candidate",
            )
            replacement = _get(
                self._frame_candidates,
                closure.replacement_frame_candidate_id,
                "replacement frame candidate",
            )
            if source.case_id != replacement.case_id:
                raise InvalidAuthorityTransition(
                    "objection closure crosses cases"
                )
            if (
                replacement.prior_frame_candidate_id
                != source.frame_candidate_id
            ):
                raise InvalidAuthorityTransition(
                    "objection closure must bind adjacent candidates"
                )
            if closure.objection_id not in (
                replacement.addressed_objection_ids
            ):
                raise InvalidAuthorityTransition(
                    "replacement candidate does not address objection"
                )
            review = _get(
                self._frame_reviews,
                closure.source_frame_review_id,
                "source frame review",
            )
            if review.frame_candidate_id != source.frame_candidate_id:
                raise InvalidAuthorityTransition(
                    "objection closure cites another candidate review"
                )
            objection = next(
                (
                    item
                    for item in review.objections
                    if item.objection_id == closure.objection_id
                ),
                None,
            )
            if (
                objection is None
                or closure.objection_content_sha256
                != content_sha256(objection)
            ):
                raise InvalidAuthorityTransition(
                    "objection closure does not bind an exact objection"
                )
            all_changed_node_ids = derive_changed_measurement_node_ids(
                source.proposed_frame.measurement_design,
                replacement.proposed_frame.measurement_design,
            )
            expected_changed_node_ids = tuple(
                node_id
                for node_id in all_changed_node_ids
                if any(
                    measurement_paths_overlap(
                        node_id,
                        affected_node_id,
                    )
                    for affected_node_id in objection.affected_node_ids
                )
            )
            if closure.changed_node_ids != expected_changed_node_ids:
                raise InvalidAuthorityTransition(
                    "objection closure change proof does not match Frames"
                )
            _put_idempotent_immutable(
                self._objection_closures,
                closure.objection_closure_id,
                closure,
                "objection closure",
            )
            return closure

    def get_objection_closure(
        self,
        objection_closure_id: str,
    ) -> ObjectionClosureRecord:
        with self._lock:
            return _get(
                self._objection_closures,
                objection_closure_id,
                "objection closure",
            )

    def record_frame_review(
        self,
        review: FrameReviewRecord,
    ) -> FrameReviewRecord:
        with self._lock:
            candidate = _get(
                self._frame_candidates,
                review.frame_candidate_id,
                "frame candidate",
            )
            active_id = self._active_frame_candidate_ids.get(
                candidate.case_id
            )
            if active_id != candidate.frame_candidate_id:
                raise InvalidAuthorityTransition(
                    "review targets a superseded frame candidate"
                )
            if (
                review.authority_epoch
                != self.get_mailbox_head(candidate.case_id).authority_epoch
                or review.reviewed_frame_content_sha256
                != candidate.proposed_frame_content_sha256
            ):
                raise InvalidAuthorityTransition(
                    "frame review authority or content is stale"
                )
            expected_closure_ids = {
                item.objection_closure_id
                for item in self._objection_closures.values()
                if item.replacement_frame_candidate_id
                == candidate.frame_candidate_id
            }
            if set(review.closure_proof_refs) != expected_closure_ids:
                raise InvalidAuthorityTransition(
                    "frame review has incomplete objection closure references"
                )
            _put_idempotent_immutable(
                self._frame_reviews,
                review.frame_review_id,
                review,
                "frame review",
            )
            return review

    def get_frame_review(
        self,
        frame_review_id: str,
    ) -> FrameReviewRecord:
        with self._lock:
            return _get(
                self._frame_reviews,
                frame_review_id,
                "frame review",
            )

    def get_frame_review_for_candidate(
        self,
        frame_candidate_id: str,
    ) -> FrameReviewRecord | None:
        with self._lock:
            matches = tuple(
                review
                for review in self._frame_reviews.values()
                if review.frame_candidate_id == frame_candidate_id
            )
            if len(matches) > 1:
                raise AuthorityConflict(
                    "frame candidate has multiple immutable reviews"
            )
            return matches[0] if matches else None

    def list_frame_reviews(
        self,
        case_id: str,
    ) -> tuple[FrameReviewRecord, ...]:
        with self._lock:
            candidates = {
                item.frame_candidate_id
                for item in self.list_frame_candidates(case_id)
            }
            return tuple(
                sorted(
                    (
                        review
                        for review in self._frame_reviews.values()
                        if review.frame_candidate_id in candidates
                    ),
                    key=lambda item: (
                        self._frame_candidates[
                            item.frame_candidate_id
                        ].candidate_generation,
                        item.frame_review_id,
                    ),
                )
            )

    def record_frame_admission_proof(
        self,
        proof: FrameAdmissionProof,
    ) -> FrameAdmissionProof:
        with self._lock:
            candidate = _get(
                self._frame_candidates,
                proof.frame_candidate_id,
                "frame candidate",
            )
            review = _get(
                self._frame_reviews,
                proof.frame_review_id,
                "frame review",
            )
            if (
                review.frame_candidate_id != candidate.frame_candidate_id
                or review.disposition is not FrameReviewDisposition.ACCEPT
                or any(item.blocking for item in review.objections)
                or proof.candidate_generation
                != candidate.candidate_generation
                or proof.frame_revision_id
                != candidate.proposed_frame_revision_id
                or proof.frame_content_sha256
                != candidate.proposed_frame_content_sha256
                or proof.frame_review_content_sha256
                != review.content_sha256
            ):
                raise InvalidAuthorityTransition(
                    "frame admission proof lacks an accepting fresh review"
                )
            closures = tuple(
                _get(
                    self._objection_closures,
                    closure_id,
                    "objection closure",
                )
                for closure_id in proof.objection_closure_record_ids
            )
            if {
                item.objection_id for item in closures
            } != set(candidate.addressed_objection_ids):
                raise InvalidAuthorityTransition(
                    "frame admission proof has incomplete objection closure"
                )
            if any(
                item.replacement_frame_candidate_id
                != candidate.frame_candidate_id
                for item in closures
            ):
                raise InvalidAuthorityTransition(
                    "objection closure targets another candidate"
                )
            if proof.authority_snapshot != self.get_authority_snapshot(
                proof.case_id
            ):
                raise InvalidAuthorityTransition(
                    "frame admission proof authority snapshot is stale"
                )
            _put_idempotent_immutable(
                self._frame_admission_proofs,
                proof.frame_admission_proof_id,
                proof,
                "frame admission proof",
            )
            prior = self._frame_admission_proof_by_frame.get(
                proof.frame_revision_id
            )
            if prior is not None and prior != proof.frame_admission_proof_id:
                raise AuthorityConflict(
                    "Frame already has another admission proof"
                )
            self._frame_admission_proof_by_frame[
                proof.frame_revision_id
            ] = proof.frame_admission_proof_id
            return proof

    def accept_plan(
        self,
        plan: WorkPlanRevision,
        *,
        expected_head_version: int,
        event_id: str,
        recorded_at: datetime,
        operation: OperationIdentity | None = None,
    ) -> InvestigationCase:
        with self._lock:
            idempotent = self._idempotent_head_result(
                event_id,
                JournalEventType.PLAN_ACCEPTED,
                plan.plan_revision_id,
                plan.case_id,
            )
            if idempotent is not None:
                return idempotent
            case = self._cas_case(plan.case_id, expected_head_version)
            if plan.frame_revision_id != case.accepted_frame_revision_id:
                raise InvalidAuthorityTransition(
                    "plan must bind the currently accepted frame"
                )
            current = max(
                (
                    candidate
                    for candidate in self._plans.values()
                    if candidate.case_id == case.case_id
                ),
                key=lambda candidate: candidate.revision_number,
                default=None,
            )
            expected_revision = 1 if current is None else current.revision_number + 1
            expected_prior = None if current is None else current.plan_revision_id
            if (
                plan.revision_number != expected_revision
                or plan.prior_plan_revision_id != expected_prior
            ):
                raise InvalidAuthorityTransition(
                    "plan revision does not extend the accepted plan"
                )
            _put_immutable(self._plans, plan.plan_revision_id, plan, "plan")
            updated = replace(
                case,
                head_version=case.head_version + 1,
                accepted_plan_revision_id=plan.plan_revision_id,
                accepted_answer_version_id=None,
                updated_at=recorded_at,
            )
            self._cases[case.case_id] = updated
            self._append_authority_event_locked(
                case_id=case.case_id,
                event_id=event_id,
                event_type=JournalEventType.PLAN_ACCEPTED,
                authority_ref=plan.plan_revision_id,
                action_id=plan.created_by_action_id,
                recorded_at=recorded_at,
                payload={
                    "revision_number": plan.revision_number,
                    "content_sha256": plan.content_sha256,
                    "head_version": updated.head_version,
                },
                operation=operation,
            )
            return updated

    def record_evidence(
        self,
        evidence: EvidenceRecord,
        *,
        expected_head_version: int,
        event_id: str,
        recorded_at: datetime,
    ) -> EvidenceRecord:
        with self._lock:
            existing_event = self._events_by_id.get(event_id)
            if existing_event is not None:
                if (
                    existing_event.event_type is JournalEventType.EVIDENCE_RECORDED
                    and existing_event.authority_ref == evidence.evidence_record_id
                ):
                    return self.get_evidence(evidence.evidence_record_id)
                raise AuthorityConflict("event ID already has different content")
            case = self._cas_case(evidence.case_id, expected_head_version)
            if (
                evidence.frame_revision_id != case.accepted_frame_revision_id
                or evidence.plan_revision_id != case.accepted_plan_revision_id
            ):
                raise InvalidAuthorityTransition(
                    "evidence must bind the currently accepted frame and plan"
                )
            plan = self.get_plan(evidence.plan_revision_id)
            if evidence.task_id not in {task.task_id for task in plan.tasks}:
                raise InvalidAuthorityTransition("evidence task is not in accepted plan")
            _put_immutable(
                self._evidence,
                evidence.evidence_record_id,
                evidence,
                "evidence",
            )
            self._append_authority_event_locked(
                case_id=case.case_id,
                event_id=event_id,
                event_type=JournalEventType.EVIDENCE_RECORDED,
                authority_ref=evidence.evidence_record_id,
                action_id=None,
                recorded_at=recorded_at,
                payload={
                    "task_id": evidence.task_id,
                    "payload_sha256": evidence.payload_sha256,
                    "strength": evidence.strength.value,
                },
            )
            return evidence

    def accept_answer(
        self,
        answer: AnswerVersion,
        *,
        expected_head_version: int,
        event_id: str,
        recorded_at: datetime,
        operation: OperationIdentity | None = None,
    ) -> InvestigationCase:
        with self._lock:
            if answer.status is AnswerStatus.SETTLED:
                raise InvalidAuthorityTransition(
                    "Gate 3 cannot publish settled answers"
                )
            idempotent = self._idempotent_head_result(
                event_id,
                JournalEventType.ANSWER_ACCEPTED,
                answer.answer_version_id,
                answer.case_id,
            )
            if idempotent is not None:
                return idempotent
            case = self._cas_case(answer.case_id, expected_head_version)
            if (
                answer.frame_revision_id != case.accepted_frame_revision_id
                or answer.plan_revision_id != case.accepted_plan_revision_id
            ):
                raise InvalidAuthorityTransition(
                    "answer must bind the currently accepted frame and plan"
                )
            current = max(
                (
                    candidate
                    for candidate in self._answers.values()
                    if candidate.case_id == case.case_id
                ),
                key=lambda candidate: candidate.version_number,
                default=None,
            )
            expected_version = 1 if current is None else current.version_number + 1
            expected_prior = None if current is None else current.answer_version_id
            if (
                answer.version_number != expected_version
                or answer.prior_answer_version_id != expected_prior
            ):
                raise InvalidAuthorityTransition(
                    "answer version does not extend the accepted answer"
                )
            for claim in answer.claims:
                for evidence_id in claim.evidence_record_ids:
                    evidence = self.get_evidence(evidence_id)
                    if (
                        evidence.frame_revision_id != answer.frame_revision_id
                        or evidence.plan_revision_id != answer.plan_revision_id
                    ):
                        raise InvalidAuthorityTransition(
                            "claim evidence is incompatible with answer authority"
                        )
            _put_immutable(
                self._answers,
                answer.answer_version_id,
                answer,
                "answer",
            )
            updated = replace(
                case,
                head_version=case.head_version + 1,
                accepted_answer_version_id=answer.answer_version_id,
                updated_at=recorded_at,
            )
            self._cases[case.case_id] = updated
            self._append_authority_event_locked(
                case_id=case.case_id,
                event_id=event_id,
                event_type=JournalEventType.ANSWER_ACCEPTED,
                authority_ref=answer.answer_version_id,
                action_id=answer.created_by_action_id,
                recorded_at=recorded_at,
                payload={
                    "version_number": answer.version_number,
                    "status": answer.status.value,
                    "content_sha256": answer.content_sha256,
                    "head_version": updated.head_version,
                },
                operation=operation,
            )
            return updated

    def record_measurement_resolution(
        self,
        outcome: MeasurementResolutionOutcome,
        *,
        admission: MeasurementResolutionAdmission,
        expected_head_version: int,
        event_id: str,
    ) -> MeasurementResolutionOutcome:
        with self.atomic():
            if self._resolution_input_verifier is None:
                raise InvalidAuthorityTransition(
                    "measurement resolution admission verifier is not "
                    "configured"
                )
            try:
                self._resolution_input_verifier.verify_resolution_admission(
                    admission=admission,
                    outcome=outcome,
                )
            except ValueError as error:
                raise InvalidAuthorityTransition(str(error)) from error
            case = self._cas_case(
                outcome.case_id,
                expected_head_version,
            )
            if (
                outcome.question_revision_id
                != case.accepted_question_revision_id
                or outcome.frame_revision_id
                != case.accepted_frame_revision_id
            ):
                raise InvalidAuthorityTransition(
                    "resolution must bind accepted question and frame"
                )
            frame = self.get_frame(outcome.frame_revision_id)
            estimand_ids = tuple(
                item.estimand_id
                for item in frame.measurement_design.estimands
            )
            try:
                index = estimand_ids.index(outcome.estimand_id)
            except ValueError as error:
                raise InvalidAuthorityTransition(
                    "resolution targets an unknown estimand"
                ) from error
            if (
                outcome.semantic_measurement_id
                != frame.semantic_measurement_ids[index]
                or outcome.authority_binding_id
                != frame.authority_binding_ids[index]
            ):
                raise InvalidAuthorityTransition(
                    "resolution identity does not match the accepted frame"
                )
            if outcome.kind is ResolutionOutcomeKind.RESOLVED_INSTANCE:
                instance = outcome.resolved_instance
                assert instance is not None
                if (
                    instance.frame_revision_id
                    != outcome.frame_revision_id
                    or instance.estimand_id != outcome.estimand_id
                    or instance.semantic_measurement_id
                    != outcome.semantic_measurement_id
                    or instance.authority_binding_id
                    != outcome.authority_binding_id
                ):
                    raise InvalidAuthorityTransition(
                        "resolved instance identity is inconsistent"
                    )
            try:
                validate_resolution_identities(outcome)
                validate_resolution_against_frame(frame, outcome)
            except ValueError as error:
                raise InvalidAuthorityTransition(str(error)) from error
            _put_idempotent_immutable(
                self._resolution_admissions,
                outcome.resolution_outcome_id,
                admission,
                "measurement resolution admission",
            )
            return self._record_derived_locked(
                records=self._resolution_outcomes,
                record_id=outcome.resolution_outcome_id,
                record=outcome,
                case_id=outcome.case_id,
                event_id=event_id,
                event_type=(
                    JournalEventType.MEASUREMENT_RESOLUTION_RECORDED
                ),
                created_at=outcome.created_at,
                label="measurement resolution",
            )

    def get_measurement_resolution(
        self,
        resolution_outcome_id: str,
    ) -> MeasurementResolutionOutcome:
        with self._lock:
            return _get(
                self._resolution_outcomes,
                resolution_outcome_id,
                "measurement resolution",
            )

    def get_measurement_resolution_admission(
        self,
        resolution_outcome_id: str,
    ) -> MeasurementResolutionAdmission:
        with self._lock:
            return _get(
                self._resolution_admissions,
                resolution_outcome_id,
                "measurement resolution admission",
            )

    def record_evidence_obligation(
        self,
        obligation: ResolvedEvidenceObligation,
        *,
        expected_head_version: int,
        event_id: str,
    ) -> ResolvedEvidenceObligation:
        with self._lock:
            case = self._cas_case(
                obligation.case_id,
                expected_head_version,
            )
            if obligation.frame_revision_id != case.accepted_frame_revision_id:
                raise InvalidAuthorityTransition(
                    "obligation must bind the accepted frame"
                )
            outcome = self.get_measurement_resolution(
                obligation.resolution_outcome_id
            )
            if (
                outcome.case_id != obligation.case_id
                or outcome.frame_revision_id != obligation.frame_revision_id
                or outcome.estimand_id != obligation.estimand_id
            ):
                raise InvalidAuthorityTransition(
                    "obligation resolution binding is inconsistent"
                )
            frame = self.get_frame(obligation.frame_revision_id)
            requirement = next(
                (
                    item
                    for item in frame.measurement_design.evidence_requirements
                    if item.evidence_requirement_id
                    == obligation.evidence_requirement_id
                ),
                None,
            )
            if requirement is None:
                raise InvalidAuthorityTransition(
                    "obligation targets an unknown evidence requirement"
                )
            if (
                obligation.estimand_id
                not in requirement.target_estimand_ids
                or obligation.evidence_requirement_sha256
                != content_sha256(requirement)
            ):
                raise InvalidAuthorityTransition(
                    "obligation changes its evidence requirement"
                )
            return self._record_derived_locked(
                records=self._evidence_obligations,
                record_id=obligation.obligation_id,
                record=obligation,
                case_id=obligation.case_id,
                event_id=event_id,
                event_type=JournalEventType.EVIDENCE_OBLIGATION_RECORDED,
                created_at=obligation.created_at,
                label="evidence obligation",
            )

    def get_evidence_obligation(
        self,
        obligation_id: str,
    ) -> ResolvedEvidenceObligation:
        with self._lock:
            return _get(
                self._evidence_obligations,
                obligation_id,
                "evidence obligation",
            )

    def record_evidence_validity(
        self,
        validity: EvidenceValidityRecord,
        *,
        event_id: str,
    ) -> EvidenceValidityRecord:
        with self._lock:
            evidence = self.get_evidence(validity.evidence_record_id)
            chain = tuple(
                item
                for item in self._evidence_validity.values()
                if item.evidence_record_id == validity.evidence_record_id
            )
            referenced = {
                item.prior_validity_record_id
                for item in chain
                if item.prior_validity_record_id is not None
            }
            heads = tuple(
                item
                for item in chain
                if item.evidence_validity_record_id not in referenced
            )
            if len(heads) > 1:
                raise AuthorityConflict(
                    "evidence validity chain has multiple heads"
                )
            current = heads[0] if heads else None
            if current is None:
                if validity.prior_validity_record_id is not None:
                    raise InvalidAuthorityTransition(
                        "first validity record cannot have a prior"
                    )
            elif (
                validity.prior_validity_record_id
                != current.evidence_validity_record_id
                or validity.expected_prior_content_sha256
                != current.content_sha256
            ):
                raise InvalidAuthorityTransition(
                    "validity record does not extend the current disposition"
                )
            return self._record_derived_locked(
                records=self._evidence_validity,
                record_id=validity.evidence_validity_record_id,
                record=validity,
                case_id=evidence.case_id,
                event_id=event_id,
                event_type=JournalEventType.EVIDENCE_VALIDITY_RECORDED,
                created_at=validity.created_at,
                label="evidence validity",
            )

    def record_obligation_satisfaction(
        self,
        satisfaction: ObligationSatisfactionRecord,
        *,
        event_id: str,
    ) -> ObligationSatisfactionRecord:
        with self._lock:
            obligation = self.get_evidence_obligation(
                satisfaction.obligation_id
            )
            return self._record_derived_locked(
                records=self._obligation_satisfaction,
                record_id=satisfaction.satisfaction_record_id,
                record=satisfaction,
                case_id=obligation.case_id,
                event_id=event_id,
                event_type=(
                    JournalEventType.OBLIGATION_SATISFACTION_RECORDED
                ),
                created_at=satisfaction.created_at,
                label="obligation satisfaction",
            )

    def record_settlement_precondition(
        self,
        report: SettlementPreconditionReport,
        *,
        expected_head_version: int,
        event_id: str,
    ) -> SettlementPreconditionReport:
        with self._lock:
            case = self._cas_case(
                report.case_id,
                expected_head_version,
            )
            if (
                report.accepted_head_version != case.head_version
                or report.question_revision_id
                != case.accepted_question_revision_id
                or report.frame_revision_id
                != case.accepted_frame_revision_id
                or report.plan_revision_id
                != case.accepted_plan_revision_id
            ):
                raise InvalidAuthorityTransition(
                    "settlement precondition is stale"
                )
            frame = self.get_frame(report.frame_revision_id)
            if (
                report.semantic_measurement_ids
                != frame.semantic_measurement_ids
                or report.authority_binding_ids
                != frame.authority_binding_ids
            ):
                raise InvalidAuthorityTransition(
                    "settlement precondition changes frame identity"
                )
            for outcome_id in report.resolution_outcome_ids:
                outcome = self.get_measurement_resolution(outcome_id)
                if outcome.frame_revision_id != report.frame_revision_id:
                    raise InvalidAuthorityTransition(
                        "settlement resolution belongs to another frame"
                    )
            for record_id in report.obligation_satisfaction_record_ids:
                record = _get(
                    self._obligation_satisfaction,
                    record_id,
                    "obligation satisfaction",
                )
                obligation = self.get_evidence_obligation(
                    record.obligation_id
                )
                if obligation.frame_revision_id != report.frame_revision_id:
                    raise InvalidAuthorityTransition(
                        "settlement obligation belongs to another frame"
                    )
            return self._record_derived_locked(
                records=self._settlement_preconditions,
                record_id=report.settlement_precondition_report_id,
                record=report,
                case_id=report.case_id,
                event_id=event_id,
                event_type=(
                    JournalEventType.SETTLEMENT_PRECONDITION_RECORDED
                ),
                created_at=report.created_at,
                label="settlement precondition",
            )

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
    ) -> InvestigationCase:
        if lifecycle not in {CaseLifecycle.STOPPED, CaseLifecycle.CLOSED}:
            raise InvalidAuthorityTransition(
                "controller can only transition a case to a terminal lifecycle"
            )
        event_type = (
            JournalEventType.CASE_STOPPED
            if lifecycle is CaseLifecycle.STOPPED
            else JournalEventType.CASE_CLOSED
        )
        with self._lock:
            idempotent = self._idempotent_head_result(
                event_id,
                event_type,
                case_id,
                case_id,
            )
            if idempotent is not None:
                return idempotent
            case = self._cas_case(case_id, expected_head_version)
            if case.lifecycle in {CaseLifecycle.STOPPED, CaseLifecycle.CLOSED}:
                raise InvalidAuthorityTransition("case is already terminal")
            updated = replace(
                case,
                lifecycle=lifecycle,
                head_version=case.head_version + 1,
                updated_at=recorded_at,
            )
            self._cases[case_id] = updated
            self._append_authority_event_locked(
                case_id=case_id,
                event_id=event_id,
                event_type=event_type,
                authority_ref=case_id,
                action_id=action_id,
                recorded_at=recorded_at,
                payload={
                    "lifecycle": lifecycle.value,
                    "head_version": updated.head_version,
                },
                operation=operation,
            )
            return updated

    def record_interpretation(
        self,
        interpretation: InterpretationRecord,
        *,
        event_id: str,
    ) -> InterpretationRecord:
        with self._lock:
            existing = self._existing_subordinate_retry(
                event_id=event_id,
                event_type=JournalEventType.INTERPRETATION_RECORDED,
                authority_ref=interpretation.interpretation_id,
                case_id=interpretation.case_id,
                records=self._interpretations,
                record=interpretation,
            )
            if existing is not None:
                return existing
            case = self.get_case(interpretation.case_id)
            if interpretation.frame_revision_id != case.accepted_frame_revision_id:
                raise InvalidAuthorityTransition(
                    "interpretation must bind the accepted frame"
                )
            for evidence_id in interpretation.evidence_record_ids:
                evidence = self.get_evidence(evidence_id)
                if (
                    evidence.case_id != interpretation.case_id
                    or evidence.frame_revision_id
                    != interpretation.frame_revision_id
                ):
                    raise InvalidAuthorityTransition(
                        "interpretation evidence is incompatible with frame"
                    )
            self._require_new_record(
                self._interpretations,
                interpretation.interpretation_id,
                "interpretation",
            )
            _put_immutable(
                self._interpretations,
                interpretation.interpretation_id,
                interpretation,
                "interpretation",
            )
            self._append_subordinate_event_locked(
                event_id=event_id,
                case_id=interpretation.case_id,
                event_type=JournalEventType.INTERPRETATION_RECORDED,
                authority_ref=interpretation.interpretation_id,
                recorded_at=interpretation.created_at,
                action_id=interpretation.created_by_action_id,
            )
            return interpretation

    def record_decision(
        self,
        decision: DecisionRecord,
        *,
        event_id: str,
    ) -> DecisionRecord:
        with self._lock:
            existing = self._existing_subordinate_retry(
                event_id=event_id,
                event_type=JournalEventType.USER_DECISION_RECORDED,
                authority_ref=decision.decision_record_id,
                case_id=decision.case_id,
                records=self._decisions,
                record=decision,
            )
            if existing is not None:
                return existing
            self.get_case(decision.case_id)
            self._require_new_record(
                self._decisions,
                decision.decision_record_id,
                "decision",
            )
            _put_immutable(
                self._decisions,
                decision.decision_record_id,
                decision,
                "decision",
            )
            self._append_subordinate_event_locked(
                event_id=event_id,
                case_id=decision.case_id,
                event_type=JournalEventType.USER_DECISION_RECORDED,
                authority_ref=decision.decision_record_id,
                recorded_at=decision.created_at,
                action_id=None,
            )
            return decision

    def record_reviewer_objection(
        self,
        objection: ReviewerObjection,
        *,
        event_id: str,
    ) -> ReviewerObjection:
        with self._lock:
            existing = self._existing_subordinate_retry(
                event_id=event_id,
                event_type=JournalEventType.REVIEWER_OBJECTION_RECORDED,
                authority_ref=objection.objection_id,
                case_id=objection.case_id,
                records=self._objections,
                record=objection,
            )
            if existing is not None:
                return existing
            self.get_case(objection.case_id)
            answer = self.get_answer(objection.answer_version_id)
            if answer.status is not AnswerStatus.PROVISIONAL:
                raise InvalidAuthorityTransition(
                    "reviewer objection must bind a provisional answer"
                )
            if objection.claim_id not in {claim.claim_id for claim in answer.claims}:
                raise InvalidAuthorityTransition(
                    "reviewer objection claim is not present in answer"
                )
            current = max(
                (
                    candidate
                    for candidate in self._objections.values()
                    if candidate.case_id == objection.case_id
                    and candidate.objection_key == objection.objection_key
                ),
                key=lambda candidate: candidate.revision_number,
                default=None,
            )
            expected_revision = (
                1 if current is None else current.revision_number + 1
            )
            expected_prior = None if current is None else current.objection_id
            if (
                objection.revision_number != expected_revision
                or objection.prior_objection_id != expected_prior
            ):
                raise InvalidAuthorityTransition(
                    "reviewer objection revision does not extend the current objection"
                )
            self._require_new_record(
                self._objections,
                objection.objection_id,
                "reviewer objection",
            )
            _put_immutable(
                self._objections,
                objection.objection_id,
                objection,
                "reviewer objection",
            )
            self._append_subordinate_event_locked(
                event_id=event_id,
                case_id=objection.case_id,
                event_type=JournalEventType.REVIEWER_OBJECTION_RECORDED,
                authority_ref=objection.objection_id,
                recorded_at=objection.created_at,
                action_id=None,
            )
            return objection

    def record_action(self, action: PersistedAction) -> PersistedAction:
        with self._lock:
            self.get_case(action.action.case_id)
            key = (
                action.action.case_id,
                action.action.idempotency_key,
            )
            existing_id = self._action_idempotency_keys.get(key)
            if existing_id is not None and existing_id != action.action.action_id:
                raise AuthorityConflict(
                    "action idempotency key already has different content"
                )
            _put_idempotent_immutable(
                self._actions,
                action.action.action_id,
                action,
                "action",
            )
            self._action_idempotency_keys[key] = action.action.action_id
            return action

    def get_action(self, action_id: str) -> PersistedAction:
        with self._lock:
            return _get(self._actions, action_id, "action")

    def record_context_packet(self, packet: ContextPacket) -> ContextPacket:
        with self._lock:
            case = self.get_case(packet.case_id)
            if packet.head_version != case.head_version:
                raise StaleHead("ContextPacket was built from a stale case head")
            _put_idempotent_immutable(
                self._contexts,
                packet.packet_id,
                packet,
                "ContextPacket",
            )
            return packet

    def get_context_packet(self, packet_id: str) -> ContextPacket:
        with self._lock:
            return _get(self._contexts, packet_id, "ContextPacket")

    def record_action_receipt(
        self,
        receipt: ActionReceipt,
    ) -> ActionReceipt:
        with self._lock:
            action = self.get_action(receipt.action_id).action
            if action.case_id != receipt.case_id:
                raise InvalidAuthorityTransition(
                    "action receipt case does not match action"
                )
            if action.content_sha256 != receipt.request_sha256:
                raise AuthorityConflict(
                    "action receipt request hash does not match action"
                )
            self._require_event_cursor(
                receipt.case_id,
                receipt.event_cursor,
            )
            key = (receipt.case_id, receipt.idempotency_key)
            existing = self._receipts.get(key)
            if existing is not None:
                if existing == receipt:
                    return existing
                raise AuthorityConflict(
                    "idempotency key already has a different receipt"
                )
            action_key = self._receipt_action_ids.get(receipt.action_id)
            if action_key is not None:
                raise AuthorityConflict("action already has a receipt")
            self._receipts[key] = receipt
            self._receipt_action_ids[receipt.action_id] = key
            return receipt

    def get_action_receipt(
        self,
        case_id: str,
        idempotency_key: str,
    ) -> ActionReceipt | None:
        with self._lock:
            return self._receipts.get((case_id, idempotency_key))

    def record_checkpoint(
        self,
        checkpoint: CheckpointRecord,
    ) -> CheckpointRecord:
        with self._lock:
            case = self.get_case(checkpoint.case_id)
            if checkpoint.head_version != case.head_version:
                raise StaleHead("checkpoint head does not match current case")
            packet = self.get_context_packet(checkpoint.context_packet_id)
            if (
                packet.case_id != checkpoint.case_id
                or packet.content_sha256 != checkpoint.context_sha256
            ):
                raise InvalidAuthorityTransition(
                    "checkpoint context binding is invalid"
                )
            event = self._require_event_cursor(
                checkpoint.case_id,
                checkpoint.event_cursor,
            )
            if event.event_type is not JournalEventType.CHECKPOINT_RECORDED:
                raise InvalidAuthorityTransition(
                    "checkpoint must bind a checkpoint event"
                )
            key = (checkpoint.case_id, checkpoint.event_cursor)
            existing_id = self._checkpoint_event_keys.get(key)
            if (
                existing_id is not None
                and existing_id != checkpoint.checkpoint_id
            ):
                raise AuthorityConflict(
                    "checkpoint event cursor already has different content"
                )
            _put_idempotent_immutable(
                self._checkpoints,
                checkpoint.checkpoint_id,
                checkpoint,
                "checkpoint",
            )
            self._checkpoint_event_keys[key] = checkpoint.checkpoint_id
            return checkpoint

    def latest_checkpoint(self, case_id: str) -> CheckpointRecord | None:
        with self._lock:
            self.get_case(case_id)
            return max(
                (
                    checkpoint
                    for checkpoint in self._checkpoints.values()
                    if checkpoint.case_id == case_id
                ),
                key=lambda checkpoint: checkpoint.event_cursor,
                default=None,
            )

    def enqueue_outbox(self, message: OutboxMessage) -> OutboxMessage:
        with self._lock:
            case = self.get_case(message.case_id)
            mailbox = self.get_mailbox_head(message.case_id)
            if message.expected_head_version != case.head_version:
                raise StaleHead("outbox expected case head is stale")
            if message.expected_authority_epoch != mailbox.authority_epoch:
                raise StaleHead("outbox expected mailbox authority is stale")
            if message.authority_snapshot != self.get_authority_snapshot(
                message.case_id
            ):
                raise StaleHead("outbox authority snapshot is stale")
            if (
                message.operation.authority_revision
                != message.expected_authority_epoch
            ):
                raise InvalidAuthorityTransition(
                    "outbox operation authority does not match its fence"
                )
            self._require_event_cursor(
                message.case_id,
                message.source_event_cursor,
            )
            if message.action_id is not None:
                action = self.get_action(message.action_id)
                if action.action.case_id != message.case_id:
                    raise InvalidAuthorityTransition(
                        "outbox action case does not match message"
                    )
                is_frame_review = (
                    message.job_kind is AsyncJobKind.REVIEWER
                    and action.action.kind is ActionKind.REVISE_FRAME
                    and message.payload.get("frame_candidate_id")
                )
                if (
                    action.action.kind not in _EFFECT_ACTION_KINDS
                    and not is_frame_review
                ):
                    raise InvalidAuthorityTransition(
                        "outbox action is incompatible with job kind"
                    )
                if (
                    action.action.kind in _EFFECT_ACTION_KINDS
                    and message.job_kind
                    is not _ACTION_JOB_KINDS[action.action.kind]
                ):
                    raise InvalidAuthorityTransition(
                        "outbox job kind does not match action"
                    )
                if is_frame_review:
                    if (
                        message.payload.get("frame_candidate_id")
                        != self.get_active_frame_candidate(
                            message.case_id
                        ).frame_candidate_id
                    ):
                        raise InvalidAuthorityTransition(
                            "review outbox does not target active candidate"
                        )
                elif (
                    message.payload.get("action_kind")
                    != action.action.kind.value
                ):
                    raise InvalidAuthorityTransition(
                        "outbox payload kind does not match action"
                    )
            elif message.job_kind in set(_ACTION_JOB_KINDS.values()):
                raise InvalidAuthorityTransition(
                    "effect outbox requires an admitted action"
                )
            duplicate_key = next(
                (
                    candidate
                    for candidate in self._outbox.values()
                    if candidate.case_id == message.case_id
                    and candidate.idempotency_key == message.idempotency_key
                ),
                None,
            )
            if duplicate_key is not None:
                if duplicate_key == message:
                    return duplicate_key
                raise AuthorityConflict(
                    "outbox idempotency key already has different content"
                )
            _put_immutable(
                self._outbox,
                message.outbox_message_id,
                message,
                "outbox message",
            )
            return message

    def get_outbox_message(self, message_id: str) -> OutboxMessage:
        with self._lock:
            return _get(self._outbox, message_id, "outbox message")

    def list_outbox_messages(
        self,
        *,
        case_id: str | None = None,
    ) -> tuple[OutboxMessage, ...]:
        with self._lock:
            if case_id is not None:
                self.get_case(case_id)
            return tuple(
                sorted(
                    (
                        message
                        for message in self._outbox.values()
                        if case_id is None or message.case_id == case_id
                    ),
                    key=lambda message: (
                        message.created_at,
                        message.source_event_cursor,
                        message.outbox_message_id,
                    ),
                )
            )

    def list_pending_outbox_messages(
        self,
        *,
        case_id: str | None = None,
    ) -> tuple[OutboxMessage, ...]:
        return tuple(
            message
            for message in self.list_outbox_messages(case_id=case_id)
            if message.outbox_message_id not in self._job_dispositions
        )

    def record_job_disposition(
        self,
        disposition: JobDispositionRecord,
    ) -> JobDispositionRecord:
        with self._lock:
            message = self.get_outbox_message(
                disposition.outbox_message_id
            )
            if (
                disposition.case_id != message.case_id
                or disposition.job_kind is not message.job_kind
                or disposition.operation != message.operation
                or disposition.expected_authority_epoch
                != message.expected_authority_epoch
            ):
                raise InvalidAuthorityTransition(
                    "job disposition does not bind its outbox message"
                )
            if disposition.disposition is JobDisposition.COMPLETED:
                if message.job_kind is AsyncJobKind.MESSAGE_BINDING:
                    if (
                        disposition.observed_authority_epoch
                        != message.expected_authority_epoch
                    ):
                        raise InvalidAuthorityTransition(
                            "message binding disposition changed its "
                            "ordered mailbox authority"
                        )
                elif (
                    disposition.observed_authority_epoch
                    != self.get_mailbox_head(
                        message.case_id
                    ).authority_epoch
                ):
                    raise InvalidAuthorityTransition(
                        "completed disposition observed stale authority"
                    )
            if disposition.fencing_token is not None:
                lease = self._job_leases.get(
                    disposition.outbox_message_id
                )
                if (
                    lease is None
                    or lease.owner_id != disposition.owner_id
                    or lease.fencing_token != disposition.fencing_token
                    or lease.expires_at <= disposition.completed_at
                ):
                    raise LeaseFenceLost(
                        "job disposition uses a stale delivery fence"
                    )
            prior = self._job_dispositions.get(
                disposition.outbox_message_id
            )
            if prior is not None:
                if prior == disposition:
                    return prior
                raise AuthorityConflict(
                    "outbox job already has another terminal disposition"
                )
            self._job_dispositions[
                disposition.outbox_message_id
            ] = disposition
            return disposition

    def get_job_disposition(
        self,
        outbox_message_id: str,
    ) -> JobDispositionRecord | None:
        with self._lock:
            self.get_outbox_message(outbox_message_id)
            return self._job_dispositions.get(outbox_message_id)

    def list_job_dispositions(
        self,
        case_id: str,
    ) -> tuple[JobDispositionRecord, ...]:
        with self._lock:
            self.get_case(case_id)
            return tuple(
                sorted(
                    (
                        item
                        for item in self._job_dispositions.values()
                        if item.case_id == case_id
                    ),
                    key=lambda item: (
                        item.completed_at,
                        item.job_disposition_record_id,
                    ),
                )
            )

    def advance_dispatcher_recovery_cursor(
        self,
        cursor: DispatcherRecoveryCursor,
    ) -> DispatcherRecoveryCursor:
        with self._lock:
            prior = self._dispatcher_recovery_cursors.get(
                cursor.dispatcher_id
            )
            if prior is not None:
                if prior.position == cursor.position:
                    return prior
                if prior.position is not None and (
                    cursor.position is None
                    or cursor.position < prior.position
                ):
                    raise InvalidAuthorityTransition(
                        "dispatcher recovery cursor cannot move backwards"
                    )
            self._dispatcher_recovery_cursors[cursor.dispatcher_id] = cursor
            return cursor

    def get_dispatcher_recovery_cursor(
        self,
        dispatcher_id: str,
    ) -> DispatcherRecoveryCursor | None:
        with self._lock:
            return self._dispatcher_recovery_cursors.get(dispatcher_id)

    def acquire_job_lease(
        self,
        *,
        outbox_message_id: str,
        owner_id: str,
        now: datetime,
        expires_at: datetime,
    ) -> JobLease:
        with self._lock:
            self.get_outbox_message(outbox_message_id)
            if outbox_message_id in self._job_dispositions:
                raise LeaseConflict(
                    "terminally disposed job cannot be claimed"
                )
            current = self._job_leases.get(outbox_message_id)
            if (
                current is not None
                and current.expires_at > now
            ):
                raise LeaseConflict("job already has an active delivery lease")
            token = (
                current.fencing_token + 1
                if current is not None
                else self._job_lease_tokens.get(outbox_message_id, 0) + 1
            )
            lease = JobLease(
                outbox_message_id=outbox_message_id,
                owner_id=owner_id,
                fencing_token=token,
                acquired_at=now,
                heartbeat_at=now,
                expires_at=expires_at,
            )
            self._job_leases[outbox_message_id] = lease
            self._job_lease_tokens[outbox_message_id] = token
            return lease

    def heartbeat_job_lease(
        self,
        lease: JobLease,
        *,
        heartbeat_at: datetime,
        expires_at: datetime,
    ) -> JobLease:
        with self._lock:
            current = self._job_leases.get(lease.outbox_message_id)
            if current != lease:
                raise LeaseFenceLost("job delivery lease fencing token was lost")
            if current.expires_at <= heartbeat_at:
                raise LeaseFenceLost(
                    "expired job delivery lease cannot be renewed"
                )
            renewed = replace(
                lease,
                heartbeat_at=heartbeat_at,
                expires_at=expires_at,
            )
            self._job_leases[lease.outbox_message_id] = renewed
            return renewed

    def release_job_lease(self, lease: JobLease) -> None:
        with self._lock:
            current = self._job_leases.get(lease.outbox_message_id)
            if current != lease:
                raise LeaseFenceLost("job delivery lease fencing token was lost")
            del self._job_leases[lease.outbox_message_id]

    def assert_job_lease(
        self,
        lease: JobLease,
        *,
        checked_at: datetime,
    ) -> JobLease:
        with self._lock:
            current = self._job_leases.get(lease.outbox_message_id)
            if current != lease or current.expires_at <= checked_at:
                raise LeaseFenceLost(
                    "job delivery lease is stale, expired, or superseded"
                )
            return current

    def record_decision_request(
        self,
        request: UserDecisionRequest,
    ) -> UserDecisionRequest:
        with self._lock:
            self.get_case(request.case_id)
            action = self.get_action(request.action_id)
            if action.action.case_id != request.case_id:
                raise InvalidAuthorityTransition(
                    "decision request case does not match action"
                )
            payload = action.action.payload
            if (
                action.action.kind is not ActionKind.ASK_USER
                or not isinstance(payload, AskUserPayload)
            ):
                raise InvalidAuthorityTransition(
                    "decision request requires an ask_user action"
                )
            if (
                request.question != payload.question
                or request.options != payload.options
                or request.recommended_option_id
                != payload.recommended_option_id
                or request.allow_freeform != payload.allow_freeform
            ):
                raise AuthorityConflict(
                    "decision request does not match ask_user action"
                )
            key = (request.case_id, request.action_id)
            existing_id = self._decision_request_action_keys.get(key)
            if (
                existing_id is not None
                and existing_id != request.decision_request_id
            ):
                raise AuthorityConflict(
                    "decision request action already has different content"
                )
            _put_idempotent_immutable(
                self._decision_requests,
                request.decision_request_id,
                request,
                "decision request",
            )
            self._decision_request_action_keys[key] = (
                request.decision_request_id
            )
            return request

    def get_decision_request(
        self,
        request_id: str,
    ) -> UserDecisionRequest:
        with self._lock:
            return _get(
                self._decision_requests,
                request_id,
                "decision request",
            )

    def record_effect_attempt(
        self,
        attempt: EffectAttemptRecord,
    ) -> EffectAttemptRecord:
        with self._lock:
            message = self.get_outbox_message(attempt.outbox_message_id)
            if message.case_id != attempt.case_id:
                raise InvalidAuthorityTransition(
                    "effect attempt case does not match outbox"
                )
            existing = self._effect_attempts.get(attempt.effect_attempt_id)
            if existing is not None:
                if existing == attempt:
                    return existing
                raise AuthorityConflict(
                    "effect attempt ID already has different content"
                )
            current = max(
                (
                    candidate
                    for candidate in self._effect_attempts.values()
                    if candidate.outbox_message_id
                    == attempt.outbox_message_id
                ),
                key=lambda candidate: candidate.attempt_number,
                default=None,
            )
            expected_number = (
                1 if current is None else current.attempt_number + 1
            )
            expected_prior = (
                None if current is None else current.effect_attempt_id
            )
            if (
                current is not None
                and current.status
                is not EffectAttemptStatus.RETRYABLE_FAILURE
            ):
                raise InvalidAuthorityTransition(
                    "completed effect attempt chain cannot be extended"
                )
            if (
                attempt.attempt_number != expected_number
                or attempt.prior_attempt_id != expected_prior
            ):
                raise InvalidAuthorityTransition(
                    "effect attempt does not extend the current attempt chain"
                )
            _put_idempotent_immutable(
                self._effect_attempts,
                attempt.effect_attempt_id,
                attempt,
                "effect attempt",
            )
            return attempt

    def list_effect_attempts(
        self,
        outbox_message_id: str,
    ) -> tuple[EffectAttemptRecord, ...]:
        with self._lock:
            self.get_outbox_message(outbox_message_id)
            return tuple(
                sorted(
                    (
                        attempt
                        for attempt in self._effect_attempts.values()
                        if attempt.outbox_message_id == outbox_message_id
                    ),
                    key=lambda attempt: attempt.attempt_number,
                )
            )

    def acquire_lease(
        self,
        *,
        case_id: str,
        run_id: str,
        owner_id: str,
        now: datetime,
        expires_at: datetime,
    ) -> ControllerLease:
        with self._lock:
            self.get_case(case_id)
            current = self._leases.get(case_id)
            if (
                current is not None
                and current.expires_at > now
            ):
                raise LeaseConflict("case already has an active controller lease")
            token = (
                self._lease_tokens.get(case_id, 0) + 1
            )
            lease = ControllerLease(
                case_id=case_id,
                run_id=run_id,
                owner_id=owner_id,
                fencing_token=token,
                acquired_at=now,
                expires_at=expires_at,
            )
            self._leases[case_id] = lease
            self._lease_tokens[case_id] = token
            return lease

    def release_lease(self, lease: ControllerLease) -> None:
        with self._lock:
            current = self._leases.get(lease.case_id)
            if (
                current is None
                or current.run_id != lease.run_id
                or current.owner_id != lease.owner_id
                or current.fencing_token != lease.fencing_token
            ):
                raise LeaseFenceLost("controller lease fencing token was lost")
            del self._leases[lease.case_id]

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
    ) -> EventJournalEntry:
        with self._lock:
            self.get_case(case_id)
            return self._append_event_locked(
                case_id=case_id,
                expected_next_cursor=expected_next_cursor,
                event_id=event_id,
                event_type=event_type,
                recorded_at=recorded_at,
                action_id=action_id,
                authority_ref=authority_ref,
                payload=payload,
                customer_projection=customer_projection,
                operation=operation,
            )

    def list_events(
        self,
        case_id: str,
        *,
        after_cursor: int = 0,
    ) -> tuple[EventJournalEntry, ...]:
        with self._lock:
            self.get_case(case_id)
            return tuple(
                event
                for event in self._events[case_id]
                if event.cursor > after_cursor
            )

    def _cas_case(
        self,
        case_id: str,
        expected_head_version: int,
    ) -> InvestigationCase:
        case = self.get_case(case_id)
        if case.head_version != expected_head_version:
            raise StaleHead(
                "expected head version {}, current is {}".format(
                    expected_head_version, case.head_version
                )
            )
        return case

    def _idempotent_head_result(
        self,
        event_id: str,
        event_type: JournalEventType,
        authority_ref: str,
        case_id: str,
    ) -> InvestigationCase | None:
        existing = self._events_by_id.get(event_id)
        if existing is None:
            return None
        if (
            existing.event_type is event_type
            and existing.authority_ref == authority_ref
            and existing.case_id == case_id
        ):
            return self.get_case(case_id)
        raise AuthorityConflict("event ID already has different content")

    def _append_authority_event_locked(
        self,
        *,
        case_id: str,
        event_id: str,
        event_type: JournalEventType,
        authority_ref: str,
        action_id: str | None,
        recorded_at: datetime,
        payload: dict[str, object],
        operation: OperationIdentity | None = None,
    ) -> EventJournalEntry:
        event_operation = (
            None
            if operation is None
            else _causal_event_operation(
                causal_operation=operation,
                event_id=event_id,
                payload=payload,
            )
        )
        return self._append_event_locked(
            case_id=case_id,
            expected_next_cursor=len(self._events[case_id]) + 1,
            event_id=event_id,
            event_type=event_type,
            recorded_at=recorded_at,
            action_id=action_id,
            authority_ref=authority_ref,
            payload=payload,
            customer_projection={
                "business_event": event_type.value,
                "authority_ref": authority_ref,
            },
            operation=event_operation,
        )

    def _append_subordinate_event_locked(
        self,
        *,
        event_id: str,
        case_id: str,
        event_type: JournalEventType,
        authority_ref: str,
        recorded_at: datetime,
        action_id: str | None,
    ) -> EventJournalEntry:
        existing = self._events_by_id.get(event_id)
        if existing is not None:
            if (
                existing.event_type is event_type
                and existing.authority_ref == authority_ref
                and existing.case_id == case_id
            ):
                return existing
            raise AuthorityConflict("event ID already has different content")
        return self._append_authority_event_locked(
            case_id=case_id,
            event_id=event_id,
            event_type=event_type,
            authority_ref=authority_ref,
            action_id=action_id,
            recorded_at=recorded_at,
            payload={},
        )

    def _existing_subordinate_retry(
        self,
        *,
        event_id: str,
        event_type: JournalEventType,
        authority_ref: str,
        case_id: str,
        records: dict[str, RecordT],
        record: RecordT,
    ) -> RecordT | None:
        existing_event = self._events_by_id.get(event_id)
        if existing_event is None:
            return None
        if (
            existing_event.event_type is event_type
            and existing_event.authority_ref == authority_ref
            and existing_event.case_id == case_id
        ):
            existing_record = records.get(authority_ref)
            if existing_record == record:
                return existing_record
        raise AuthorityConflict("event ID already has different content")

    @staticmethod
    def _require_new_record(
        records: dict[str, object],
        record_id: str,
        label: str,
    ) -> None:
        if record_id in records:
            raise AuthorityConflict(
                "{} was already persisted under another event".format(label)
            )

    def _append_event_locked(
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
        operation: OperationIdentity | None = None,
    ) -> EventJournalEntry:
        resolved_operation = operation or _derived_event_operation(
            case_id=case_id,
            event_id=event_id,
            action_id=action_id,
            authority_ref=authority_ref,
            payload=payload,
        )
        existing = self._events_by_id.get(event_id)
        if existing is not None:
            candidate = EventJournalEntry(
                event_id=event_id,
                case_id=case_id,
                cursor=existing.cursor,
                event_type=event_type,
                recorded_at=recorded_at,
                operation=resolved_operation,
                action_id=action_id,
                authority_ref=authority_ref,
                payload=payload,
                customer_projection=customer_projection,
            )
            if existing == candidate:
                return existing
            raise AuthorityConflict("event ID already has different content")
        actual_next = len(self._events[case_id]) + 1
        if expected_next_cursor != actual_next:
            raise AuthorityConflict(
                "expected cursor {}, next is {}".format(
                    expected_next_cursor, actual_next
                )
            )
        entry = EventJournalEntry(
            event_id=event_id,
            case_id=case_id,
            cursor=actual_next,
            event_type=event_type,
            recorded_at=recorded_at,
            operation=resolved_operation,
            action_id=action_id,
            authority_ref=authority_ref,
            payload=payload,
            customer_projection=customer_projection,
        )
        self._events[case_id].append(entry)
        self._events_by_id[event_id] = entry
        return entry

    def _require_event_cursor(
        self,
        case_id: str,
        cursor: int,
    ) -> EventJournalEntry:
        self.get_case(case_id)
        for event in self._events[case_id]:
            if event.cursor == cursor:
                return event
        raise AuthorityNotFound(
            "event cursor {} does not exist for case {!r}".format(
                cursor,
                case_id,
            )
        )

    def _record_derived_locked(
        self,
        *,
        records: dict[str, RecordT],
        record_id: str,
        record: RecordT,
        case_id: str,
        event_id: str,
        event_type: JournalEventType,
        created_at: datetime,
        label: str,
    ) -> RecordT:
        existing_event = self._events_by_id.get(event_id)
        if existing_event is not None:
            if (
                existing_event.event_type is event_type
                and existing_event.authority_ref == record_id
                and existing_event.case_id == case_id
            ):
                return _get(records, record_id, label)
            raise AuthorityConflict(
                "event ID already has different derived content"
            )
        _put_immutable(records, record_id, record, label)
        self._append_authority_event_locked(
            case_id=case_id,
            event_id=event_id,
            event_type=event_type,
            authority_ref=record_id,
            action_id=None,
            recorded_at=created_at,
            payload={
                "content_sha256": content_sha256(record),
            },
        )
        return record

    def _snapshot(self) -> dict[str, object]:
        return {
            "_cases": self._cases.copy(),
            "_questions": self._questions.copy(),
            "_frames": self._frames.copy(),
            "_plans": self._plans.copy(),
            "_evidence": self._evidence.copy(),
            "_answers": self._answers.copy(),
            "_resolution_outcomes": self._resolution_outcomes.copy(),
            "_resolution_admissions": self._resolution_admissions.copy(),
            "_evidence_obligations": self._evidence_obligations.copy(),
            "_evidence_validity": self._evidence_validity.copy(),
            "_obligation_satisfaction": (
                self._obligation_satisfaction.copy()
            ),
            "_settlement_preconditions": (
                self._settlement_preconditions.copy()
            ),
            "_interpretations": self._interpretations.copy(),
            "_decisions": self._decisions.copy(),
            "_objections": self._objections.copy(),
            "_actions": self._actions.copy(),
            "_action_idempotency_keys": self._action_idempotency_keys.copy(),
            "_contexts": self._contexts.copy(),
            "_receipts": self._receipts.copy(),
            "_receipt_action_ids": self._receipt_action_ids.copy(),
            "_checkpoints": self._checkpoints.copy(),
            "_checkpoint_event_keys": self._checkpoint_event_keys.copy(),
            "_outbox": self._outbox.copy(),
            "_job_dispositions": self._job_dispositions.copy(),
            "_dispatcher_recovery_cursors": (
                self._dispatcher_recovery_cursors.copy()
            ),
            "_message_ingress_records": (
                self._message_ingress_records.copy()
            ),
            "_pending_user_messages": (
                self._pending_user_messages.copy()
            ),
            "_message_impact_bindings": (
                self._message_impact_bindings.copy()
            ),
            "_logical_model_jobs": self._logical_model_jobs.copy(),
            "_provider_attempt_requests": (
                self._provider_attempt_requests.copy()
            ),
            "_provider_attempt_receipts": (
                self._provider_attempt_receipts.copy()
            ),
            "_durable_model_results": (
                self._durable_model_results.copy()
            ),
            "_obligation_schedules": self._obligation_schedules.copy(),
            "_obligation_dispatch_records": (
                self._obligation_dispatch_records.copy()
            ),
            "_obligation_completion_records": (
                self._obligation_completion_records.copy()
            ),
            "_obligation_schedule_checkpoints": (
                self._obligation_schedule_checkpoints.copy()
            ),
            "_run_trace_manifests": (
                self._run_trace_manifests.copy()
            ),
            "_decision_requests": self._decision_requests.copy(),
            "_decision_request_action_keys": (
                self._decision_request_action_keys.copy()
            ),
            "_effect_attempts": self._effect_attempts.copy(),
            "_leases": self._leases.copy(),
            "_lease_tokens": self._lease_tokens.copy(),
            "_job_leases": self._job_leases.copy(),
            "_job_lease_tokens": self._job_lease_tokens.copy(),
            "_frame_candidates": self._frame_candidates.copy(),
            "_active_frame_candidate_ids": (
                self._active_frame_candidate_ids.copy()
            ),
            "_frame_candidate_supersessions": (
                self._frame_candidate_supersessions.copy()
            ),
            "_frame_reviews": self._frame_reviews.copy(),
            "_objection_closures": self._objection_closures.copy(),
            "_frame_admission_proofs": (
                self._frame_admission_proofs.copy()
            ),
            "_frame_admission_proof_by_frame": (
                self._frame_admission_proof_by_frame.copy()
            ),
            "_events": {
                case_id: list(events)
                for case_id, events in self._events.items()
            },
            "_events_by_id": self._events_by_id.copy(),
            "_mailbox_heads": self._mailbox_heads.copy(),
            "_mailbox_messages": self._mailbox_messages.copy(),
        }

    def _restore(self, snapshot: dict[str, object]) -> None:
        for name, value in snapshot.items():
            setattr(self, name, value)


def _get(records: dict[str, RecordT], record_id: str, label: str) -> RecordT:
    try:
        return records[record_id]
    except KeyError as error:
        raise AuthorityNotFound(
            "{} {!r} does not exist".format(label, record_id)
        ) from error


def _put_immutable(
    records: dict[str, RecordT],
    record_id: str,
    record: RecordT,
    label: str,
) -> None:
    existing = records.get(record_id)
    if existing is None:
        records[record_id] = record
        return
    if existing == record:
        raise AuthorityConflict(
            "{} was already persisted under another event".format(label)
        )
    raise AuthorityConflict("{} ID already has different content".format(label))


def _put_idempotent_immutable(
    records: dict[str, RecordT],
    record_id: str,
    record: RecordT,
    label: str,
) -> None:
    existing = records.get(record_id)
    if existing is None:
        records[record_id] = record
        return
    if existing == record:
        return
    raise AuthorityConflict("{} ID already has different content".format(label))


def _derived_event_operation(
    *,
    case_id: str,
    event_id: str,
    action_id: str | None,
    authority_ref: str | None,
    payload: dict[str, object],
) -> OperationIdentity:
    operation_id = action_id or authority_ref or event_id
    authority_revision = payload.get("head_version", 0)
    if not isinstance(authority_revision, int):
        authority_revision = 0
    return OperationIdentity(
        operation_id=operation_id,
        idempotency_key=event_id,
        causation_id=action_id or event_id,
        correlation_id=case_id,
        authority_revision=authority_revision,
        payload_sha256=content_sha256(payload),
    )


def _causal_event_operation(
    *,
    causal_operation: OperationIdentity,
    event_id: str,
    payload: dict[str, object],
) -> OperationIdentity:
    return OperationIdentity(
        operation_id=f"event-operation:{event_id}",
        idempotency_key=f"event-key:{event_id}",
        causation_id=causal_operation.operation_id,
        correlation_id=causal_operation.correlation_id,
        authority_revision=causal_operation.authority_revision,
        payload_sha256=content_sha256(payload),
    )
