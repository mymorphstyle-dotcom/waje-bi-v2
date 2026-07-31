"""In-memory conformance adapter for the authority storage contract."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Callable, Iterator, TypeVar

from waje_vnext.domain.actions import (
    ActionKind,
    AskUserPayload,
)
from waje_vnext.domain.answering import (
    AnswerCandidateStatus,
    AnswerStatus,
    AnswerVersion,
    ClaimEvidenceSupport,
    ClaimPrecheckRecord,
    ProvisionalAnswerBundle,
    ProvisionalAnswerCandidate,
    SettlementPreconditionReport,
    compile_provisional_answer_bundle,
    derive_settlement_precondition_report,
    validate_provisional_answer_candidate,
)
from waje_vnext.domain.admission import validate_effect_outbox_binding
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
    CaseLifecycle,
    DecisionRecord,
    InvestigationCase,
    InterpretationRecord,
    ReviewerObjection,
    WorkPlanRevision,
)
from waje_vnext.domain.context import ContextPacket
from waje_vnext.domain.canonical import (
    content_sha256,
    require_aware_datetime,
)
from waje_vnext.domain.controller import (
    ControllerLease,
    EffectAttemptRecord,
    EffectAttemptStatus,
    PersistedAction,
    UserDecisionRequest,
)
from waje_vnext.domain.events import EventJournalEntry, JournalEventType
from waje_vnext.domain.evidence import (
    CapabilityResultEnvelope,
    CapabilityResultReceipt,
    EvidenceAdmissionProfile,
    EvidenceAdmissionRecord,
    EvidenceAdmissionStatus,
    EvidenceRecord,
    EvidenceUseBinding,
    EvidenceValidityRecord,
    EvidenceValidityStatus,
    ObligationSatisfactionRecord,
    build_conformance_execution_provenance,
    build_evidence_admission,
    build_evidence_use_binding,
    build_evidence_validity_successor,
    build_initial_evidence_validity,
    build_obligation_satisfaction,
    validate_capability_result_envelope,
    validate_capability_result_receipt,
    validate_evidence_record_authority,
)
from waje_vnext.domain.identity import (
    validate_frame_identities,
    validate_resolution_against_frame,
    validate_resolution_identities,
)
from waje_vnext.domain.measurement import (
    MeasurementResolutionOutcome,
    QuestionRevision,
    ResolvedEvidenceObligation,
    ResolutionOutcomeKind,
)
from waje_vnext.domain.measurement_resolver import (
    MeasurementResolutionAdmission,
    TrustedResolutionInputVerifier,
    validate_evidence_obligation_derivation,
    validate_executable_design,
)
from waje_vnext.domain.obligation_scheduler import (
    ObligationCompletionRecord,
    ObligationDispatchRecord,
    ObligationScheduleCheckpoint,
    ObligationScheduleRecord,
    ObligationTerminalStatus,
    same_obligation_business_authority,
    validate_obligation_dispatch_admission,
    validate_schedule_plan_binding,
    validate_persisted_obligation_completion,
)
from waje_vnext.domain.planning import (
    ConformanceExecutionSpec,
    ExecutionRealm,
    LogicalExecutionAttempt,
    PlanAdoptionRecord,
    PlanBundle,
    QueryBindingEnvelope,
    same_business_authority,
    validate_conformance_execution_spec_authority,
    validate_logical_execution_attempt_authority,
    validate_plan_bundle,
)
from waje_vnext.domain.runtime_state import (
    ANSWER_REVIEW_JOB_CONTRACT_REF,
    ActionReceipt,
    CheckpointRecord,
    OutboxMessage,
)
from waje_vnext.domain.workflow import (
    WorkflowReadModel,
    apply_workflow_fact,
    initial_workflow_read_model,
)
from waje_vnext.domain.workflow_adapter import (
    AcceptedPlanAuthority,
    AnswerAuthority,
    CheckpointAuthority,
    DispatchAuthority,
    SatisfactionAuthority,
    SupersedingAuthority,
    WorkflowEventAuthority,
    journal_event_to_workflow_fact,
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

from .answer_admission import (
    accepted_answer_candidate_is_current,
    validate_answer_candidate_action,
)
from .settlement_validation import validate_settlement_request
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
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._lock = RLock()
        self._resolution_input_verifier = resolution_input_verifier
        self._clock = clock or (lambda: datetime.now(UTC))
        self._cases: dict[str, InvestigationCase] = {}
        self._questions: dict[str, QuestionRevision] = {}
        self._frames: dict[str, AnalysisFrameRevision] = {}
        self._plans: dict[str, WorkPlanRevision] = {}
        self._plan_adoptions: dict[str, PlanAdoptionRecord] = {}
        self._plan_adoption_by_plan: dict[str, str] = {}
        self._query_bindings: dict[str, QueryBindingEnvelope] = {}
        self._conformance_execution_specs: dict[
            str,
            ConformanceExecutionSpec,
        ] = {}
        self._conformance_spec_by_logical_execution: dict[
            str,
            str,
        ] = {}
        self._conformance_spec_by_query_binding: dict[str, str] = {}
        self._logical_execution_attempts: dict[
            str,
            LogicalExecutionAttempt,
        ] = {}
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
        self._capability_result_envelopes: dict[
            str,
            CapabilityResultEnvelope,
        ] = {}
        self._capability_result_receipts: dict[
            str,
            CapabilityResultReceipt,
        ] = {}
        self._capability_result_receipt_by_outbox: dict[str, str] = {}
        self._evidence_admissions: dict[
            str,
            EvidenceAdmissionRecord,
        ] = {}
        self._evidence_admission_by_receipt: dict[
            tuple[str, EvidenceAdmissionProfile],
            str,
        ] = {}
        self._validity_head_by_evidence: dict[str, str] = {}
        self._evidence_use_bindings: dict[str, EvidenceUseBinding] = {}
        self._satisfaction_head_by_obligation: dict[
            str,
            str,
        ] = {}
        self._answer_candidates: dict[
            str,
            ProvisionalAnswerCandidate,
        ] = {}
        self._claim_prechecks: dict[str, ClaimPrecheckRecord] = {}
        self._workflow_read_models: dict[str, WorkflowReadModel] = {}
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

    def _trusted_now(self) -> datetime:
        now = self._clock()
        require_aware_datetime(now, "storage clock")
        return now

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
            if self.get_case(record.case_id).lifecycle in {
                CaseLifecycle.STOPPED,
                CaseLifecycle.CLOSED,
            }:
                raise InvalidAuthorityTransition(
                    "terminal case fences obligation schedule"
                )
            if self.get_authority_snapshot(record.case_id) != (
                record.authority_snapshot
            ):
                raise InvalidAuthorityTransition(
                    "obligation schedule authority is stale"
                )
            plan = self.get_plan(record.plan_revision_id)
            adoption = self.get_plan_adoption(
                record.plan_revision_id
            )
            query_bindings = self.list_query_bindings(
                record.plan_revision_id
            )
            obligations = tuple(
                self.get_evidence_obligation(obligation_id)
                for obligation_id in adoption.obligation_ids
            )
            try:
                validate_schedule_plan_binding(
                    schedule=record,
                    plan=plan,
                    adoption=adoption,
                    query_bindings=query_bindings,
                    obligations=obligations,
                )
            except ValueError as error:
                raise InvalidAuthorityTransition(str(error)) from error
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
        *,
        message: OutboxMessage,
        record: ObligationDispatchRecord,
    ) -> ObligationDispatchRecord:
        with self._lock:
            schedule = self.get_obligation_schedule(record.schedule_id)
            case = self.get_case(schedule.case_id)
            if case.lifecycle in {
                CaseLifecycle.STOPPED,
                CaseLifecycle.CLOSED,
            }:
                raise InvalidAuthorityTransition(
                    "terminal case fences obligation dispatch"
                )
            mailbox = self.get_mailbox_head(message.case_id)
            if message.expected_head_version != case.head_version:
                raise StaleHead("outbox expected case head is stale")
            if message.expected_authority_epoch != mailbox.authority_epoch:
                raise StaleHead("outbox expected mailbox authority is stale")
            if message.authority_snapshot != self.get_authority_snapshot(
                message.case_id
            ):
                raise StaleHead("outbox authority snapshot is stale")
            self._require_event_cursor(
                message.case_id,
                message.source_event_cursor,
            )
            source_event = self._events[message.case_id][
                message.source_event_cursor - 1
            ]
            expected_event_payload = {
                key: value
                for key, value in message.payload.items()
                if key != "obligation"
            }
            expected_event_payload["outbox_message_id"] = (
                message.outbox_message_id
            )
            if (
                source_event.event_type
                is not JournalEventType.OBLIGATION_DISPATCH_ENQUEUED
                or source_event.authority_ref
                != record.dispatch_record_id
                or source_event.payload != expected_event_payload
                or source_event.operation.causation_id
                != schedule.schedule_id
                or source_event.operation.correlation_id
                != schedule.correlation_id
                or message.operation.causation_id
                != source_event.operation.operation_id
            ):
                raise InvalidAuthorityTransition(
                    "obligation dispatch outbox lacks its schedule event"
                )
            persisted_obligation = self.get_evidence_obligation(
                record.dispatch.obligation_id
            )
            scheduled_obligation = next(
                (
                    item
                    for item in schedule.obligations
                    if item.obligation_id
                    == record.dispatch.obligation_id
                ),
                None,
            )
            if scheduled_obligation != persisted_obligation:
                raise InvalidAuthorityTransition(
                    "obligation dispatch does not bind persisted obligation"
                )
            try:
                validate_obligation_dispatch_admission(
                    schedule=schedule,
                    record=record,
                    message=message,
                )
            except ValueError as error:
                raise InvalidAuthorityTransition(str(error)) from error
            prior_dispatch = next(
                (
                    candidate
                    for candidate
                    in self._obligation_dispatch_records.values()
                    if candidate.schedule_id == record.schedule_id
                    and candidate.dispatch.obligation_id
                    == record.dispatch.obligation_id
                ),
                None,
            )
            if prior_dispatch is not None:
                prior_message = self._outbox.get(
                    prior_dispatch.outbox_message_id
                )
                if (
                    prior_dispatch == record
                    and prior_message == message
                ):
                    return prior_dispatch
                raise AuthorityConflict(
                    "obligation already has another dispatch"
                )
            duplicate_key = next(
                (
                    candidate
                    for candidate in self._outbox.values()
                    if candidate.case_id == message.case_id
                    and candidate.idempotency_key
                    == message.idempotency_key
                ),
                None,
            )
            if duplicate_key is not None and duplicate_key != message:
                raise AuthorityConflict(
                    "outbox idempotency key already has different content"
                )
            if duplicate_key is None:
                _put_immutable(
                    self._outbox,
                    message.outbox_message_id,
                    message,
                    "outbox message",
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
            case = self.get_case(schedule.case_id)
            if case.lifecycle in {
                CaseLifecycle.STOPPED,
                CaseLifecycle.CLOSED,
            }:
                raise InvalidAuthorityTransition(
                    "terminal case fences obligation completion"
                )
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
            if self.get_case(schedule.case_id).lifecycle in {
                CaseLifecycle.STOPPED,
                CaseLifecycle.CLOSED,
            }:
                raise InvalidAuthorityTransition(
                    "terminal case fences obligation checkpoint"
                )
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

    def accept_plan_bundle(
        self,
        bundle: PlanBundle,
        *,
        expected_head_version: int,
        event_id: str,
        recorded_at: datetime,
        operation: OperationIdentity | None = None,
    ) -> InvestigationCase:
        with self.atomic():
            plan = bundle.plan
            existing_event = self._events_by_id.get(event_id)
            if existing_event is not None:
                if (
                    existing_event.event_type
                    is JournalEventType.PLAN_ACCEPTED
                    and existing_event.authority_ref
                    == plan.plan_revision_id
                    and existing_event.case_id == plan.case_id
                ):
                    existing_bundle = PlanBundle(
                        plan=self.get_plan(plan.plan_revision_id),
                        query_bindings=self.list_query_bindings(
                            plan.plan_revision_id
                        ),
                        adoption=self.get_plan_adoption(
                            plan.plan_revision_id
                        ),
                    )
                    expected_replay_operation = (
                        _derived_event_operation(
                            case_id=plan.case_id,
                            event_id=event_id,
                            action_id=plan.created_by_action_id,
                            authority_ref=plan.plan_revision_id,
                            payload=dict(existing_event.payload),
                        )
                        if operation is None
                        else _causal_event_operation(
                            causal_operation=operation,
                            event_id=event_id,
                            payload=dict(existing_event.payload),
                        )
                    )
                    if (
                        existing_bundle != bundle
                        or existing_event.operation
                        != expected_replay_operation
                    ):
                        raise AuthorityConflict(
                            "plan event replay changes bundle content"
                        )
                    return self.get_case(plan.case_id)
                raise AuthorityConflict(
                    "event ID already has different content"
                )

            case = self._cas_case(
                plan.case_id,
                expected_head_version,
            )
            if (
                bundle.adoption.expected_head_version
                != expected_head_version
            ):
                raise InvalidAuthorityTransition(
                    "plan adoption expected head is stale"
                )
            current_snapshot = self.get_authority_snapshot(
                plan.case_id
            )
            if current_snapshot != bundle.adoption.authority_snapshot:
                raise StaleHead(
                    "plan adoption authority snapshot is stale"
                )
            if (
                operation is not None
                and operation.authority_revision
                != current_snapshot.mailbox_authority_epoch
            ):
                raise StaleHead(
                    "plan operation authority epoch is stale"
                )
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
            expected_revision = (
                1 if current is None else current.revision_number + 1
            )
            expected_prior = (
                None if current is None else current.plan_revision_id
            )
            if (
                plan.revision_number != expected_revision
                or plan.prior_plan_revision_id != expected_prior
                or (
                    case.accepted_plan_revision_id is not None
                    and plan.prior_plan_revision_id
                    != case.accepted_plan_revision_id
                )
            ):
                raise InvalidAuthorityTransition(
                    "plan revision does not extend the accepted plan"
                )
            frame = self.get_frame(plan.frame_revision_id)
            outcomes = tuple(
                self.get_measurement_resolution(outcome_id)
                for outcome_id
                in bundle.adoption.resolution_outcome_ids
            )
            admissions = tuple(
                self.get_measurement_resolution_admission(outcome_id)
                for outcome_id
                in bundle.adoption.resolution_outcome_ids
            )
            obligations = self.list_evidence_obligations(
                plan.frame_revision_id
            )
            validate_plan_bundle(
                bundle=bundle,
                case=case,
                authority_snapshot=current_snapshot,
                frame=frame,
                outcomes=outcomes,
                admissions=admissions,
                obligations=obligations,
            )
            _put_immutable(
                self._plans,
                plan.plan_revision_id,
                plan,
                "plan",
            )
            for binding in bundle.query_bindings:
                _put_immutable(
                    self._query_bindings,
                    binding.query_binding_id,
                    binding,
                    "query binding",
                )
            _put_immutable(
                self._plan_adoptions,
                bundle.adoption.plan_adoption_id,
                bundle.adoption,
                "plan adoption",
            )
            if plan.plan_revision_id in self._plan_adoption_by_plan:
                raise AuthorityConflict(
                    "plan already has an adoption record"
                )
            self._plan_adoption_by_plan[
                plan.plan_revision_id
            ] = bundle.adoption.plan_adoption_id
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
                    "plan_adoption_id": (
                        bundle.adoption.plan_adoption_id
                    ),
                    "plan_adoption_sha256": (
                        bundle.adoption.content_sha256
                    ),
                    "query_binding_ids": tuple(
                        item.query_binding_id
                        for item in bundle.query_bindings
                    ),
                    "head_version": updated.head_version,
                },
                operation=operation,
            )
            return updated

    def get_plan_adoption(
        self,
        plan_revision_id: str,
    ) -> PlanAdoptionRecord:
        with self._lock:
            adoption_id = _get(
                self._plan_adoption_by_plan,
                plan_revision_id,
                "plan adoption binding",
            )
            return _get(
                self._plan_adoptions,
                adoption_id,
                "plan adoption",
            )

    def get_query_binding(
        self,
        query_binding_id: str,
    ) -> QueryBindingEnvelope:
        with self._lock:
            return _get(
                self._query_bindings,
                query_binding_id,
                "query binding",
            )

    def list_query_bindings(
        self,
        plan_revision_id: str,
    ) -> tuple[QueryBindingEnvelope, ...]:
        with self._lock:
            return tuple(
                item
                for item in self._query_bindings.values()
                if item.plan_revision_id == plan_revision_id
            )

    def record_conformance_execution_spec(
        self,
        spec: ConformanceExecutionSpec,
        *,
        expected_authority_snapshot: AuthoritySnapshot,
    ) -> ConformanceExecutionSpec:
        with self._lock:
            existing = self._conformance_execution_specs.get(
                spec.conformance_execution_spec_id
            )
            if existing is not None:
                if existing == spec:
                    return existing
                raise AuthorityConflict(
                    "conformance execution spec ID has different content"
                )
            current = self.get_authority_snapshot(spec.case_id)
            if current != expected_authority_snapshot:
                raise StaleHead(
                    "conformance execution authority is stale"
                )
            binding = self.get_query_binding(
                spec.query_binding_id
            )
            try:
                validate_conformance_execution_spec_authority(
                    spec=spec,
                    binding=binding,
                    current_authority=current,
                )
            except ValueError as error:
                raise InvalidAuthorityTransition(
                    str(error)
                ) from error
            existing_logical = (
                self._conformance_spec_by_logical_execution.get(
                    spec.logical_execution_id
                )
            )
            existing_binding = (
                self._conformance_spec_by_query_binding.get(
                    spec.query_binding_id
                )
            )
            if (
                existing_logical is not None
                and existing_logical
                != spec.conformance_execution_spec_id
            ):
                raise AuthorityConflict(
                    "logical execution already has another spec"
                )
            if (
                existing_binding is not None
                and existing_binding
                != spec.conformance_execution_spec_id
            ):
                raise AuthorityConflict(
                    "query binding already has another execution spec"
                )
            _put_idempotent_immutable(
                self._conformance_execution_specs,
                spec.conformance_execution_spec_id,
                spec,
                "conformance execution spec",
            )
            self._conformance_spec_by_logical_execution[
                spec.logical_execution_id
            ] = spec.conformance_execution_spec_id
            self._conformance_spec_by_query_binding[
                spec.query_binding_id
            ] = spec.conformance_execution_spec_id
            return spec

    def get_conformance_execution_spec(
        self,
        conformance_execution_spec_id: str,
    ) -> ConformanceExecutionSpec:
        with self._lock:
            return _get(
                self._conformance_execution_specs,
                conformance_execution_spec_id,
                "conformance execution spec",
            )

    def record_logical_execution_attempt(
        self,
        attempt: LogicalExecutionAttempt,
    ) -> LogicalExecutionAttempt:
        with self._lock:
            existing = self._logical_execution_attempts.get(
                attempt.logical_execution_attempt_id
            )
            if existing is not None:
                if existing == attempt:
                    return existing
                raise AuthorityConflict(
                    "logical execution attempt ID has different content"
                )
            spec = self.get_conformance_execution_spec(
                attempt.conformance_execution_spec_id
            )
            binding = self.get_query_binding(
                attempt.query_binding_id
            )
            current = self.get_authority_snapshot(attempt.case_id)
            if not same_business_authority(
                current,
                attempt.authority_snapshot,
            ):
                raise StaleHead(
                    "logical execution attempt authority is stale"
                )
            prior_attempts = self.list_logical_execution_attempts(
                attempt.logical_execution_id
            )
            prior_attempt = (
                None if not prior_attempts else prior_attempts[-1]
            )
            try:
                validate_logical_execution_attempt_authority(
                    attempt=attempt,
                    spec=spec,
                    binding=binding,
                    current_authority=current,
                    prior_attempt=prior_attempt,
                )
            except ValueError as error:
                raise InvalidAuthorityTransition(
                    str(error)
                ) from error
            if attempt.attempt_number != len(prior_attempts) + 1:
                raise InvalidAuthorityTransition(
                    "logical attempt number is not contiguous"
                )
            if prior_attempts:
                if (
                    attempt.prior_attempt_id
                    != prior_attempts[-1].logical_execution_attempt_id
                ):
                    raise InvalidAuthorityTransition(
                        "logical retry does not extend prior attempt"
                    )
            _put_immutable(
                self._logical_execution_attempts,
                attempt.logical_execution_attempt_id,
                attempt,
                "logical execution attempt",
            )
            return attempt

    def list_logical_execution_attempts(
        self,
        logical_execution_id: str,
    ) -> tuple[LogicalExecutionAttempt, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        item
                        for item in (
                            self._logical_execution_attempts.values()
                        )
                        if item.logical_execution_id
                        == logical_execution_id
                    ),
                    key=lambda item: item.attempt_number,
                )
            )

    def get_capability_result_envelope(
        self,
        capability_result_envelope_id: str,
    ) -> CapabilityResultEnvelope:
        with self._lock:
            return _get(
                self._capability_result_envelopes,
                capability_result_envelope_id,
                "capability result envelope",
            )

    def get_capability_result_receipt(
        self,
        capability_result_receipt_id: str,
    ) -> CapabilityResultReceipt:
        with self._lock:
            return _get(
                self._capability_result_receipts,
                capability_result_receipt_id,
                "capability result receipt",
            )

    def find_capability_result_receipt_by_outbox(
        self,
        outbox_message_id: str,
    ) -> CapabilityResultReceipt | None:
        with self._lock:
            receipt_id = self._capability_result_receipt_by_outbox.get(
                outbox_message_id
            )
            return (
                None
                if receipt_id is None
                else self._capability_result_receipts[receipt_id]
            )

    def get_evidence(
        self,
        evidence_record_id: str,
    ) -> EvidenceRecord:
        with self._lock:
            return _get(
                self._evidence,
                evidence_record_id,
                "Gate 3.5 evidence",
            )

    def list_evidence(
        self,
        case_id: str,
    ) -> tuple[EvidenceRecord, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        item
                        for item in self._evidence.values()
                        if item.case_id == case_id
                    ),
                    key=lambda item: (
                        item.produced_at,
                        item.evidence_record_id,
                    ),
                )
            )

    def get_evidence_admission(
        self,
        evidence_admission_id: str,
    ) -> EvidenceAdmissionRecord:
        with self._lock:
            return _get(
                self._evidence_admissions,
                evidence_admission_id,
                "evidence admission",
            )

    def list_evidence_admissions(
        self,
        *,
        case_id: str,
        profile: EvidenceAdmissionProfile | None = None,
    ) -> tuple[EvidenceAdmissionRecord, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        item
                        for item in self._evidence_admissions.values()
                        if item.authority_fence.case_id == case_id
                        and (
                            profile is None
                            or item.profile is profile
                        )
                    ),
                    key=lambda item: (
                        item.admitted_at,
                        item.evidence_admission_id,
                    ),
                )
            )

    def get_evidence_validity(
        self,
        evidence_validity_id: str,
    ) -> EvidenceValidityRecord:
        with self._lock:
            return _get(
                self._evidence_validity,
                evidence_validity_id,
                "Gate 3.5 evidence validity",
            )

    def latest_evidence_validity(
        self,
        evidence_record_id: str,
    ) -> EvidenceValidityRecord:
        with self._lock:
            validity_id = _get(
                self._validity_head_by_evidence,
                evidence_record_id,
                "Gate 3.5 evidence validity head",
            )
            return self._evidence_validity[validity_id]

    def get_evidence_use_binding(
        self,
        evidence_use_binding_id: str,
    ) -> EvidenceUseBinding:
        with self._lock:
            return _get(
                self._evidence_use_bindings,
                evidence_use_binding_id,
                "evidence use binding",
            )

    def get_obligation_satisfaction(
        self,
        obligation_satisfaction_id: str,
    ) -> ObligationSatisfactionRecord:
        with self._lock:
            return _get(
                self._obligation_satisfaction,
                obligation_satisfaction_id,
                "Gate 3.5 obligation satisfaction",
            )

    def latest_obligation_satisfaction(
        self,
        obligation_id: str,
    ) -> ObligationSatisfactionRecord:
        with self._lock:
            satisfaction_id = _get(
                self._satisfaction_head_by_obligation,
                obligation_id,
                "Gate 3.5 obligation satisfaction head",
            )
            return self._obligation_satisfaction[
                satisfaction_id
            ]

    def get_answer_candidate(
        self,
        answer_candidate_id: str,
    ) -> ProvisionalAnswerCandidate:
        with self._lock:
            return _get(
                self._answer_candidates,
                answer_candidate_id,
                "answer candidate",
            )

    def get_claim_precheck(
        self,
        claim_precheck_id: str,
    ) -> ClaimPrecheckRecord:
        with self._lock:
            return _get(
                self._claim_prechecks,
                claim_precheck_id,
                "claim precheck",
            )

    def get_answer(
        self,
        answer_version_id: str,
    ) -> AnswerVersion:
        with self._lock:
            return _get(
                self._answers,
                answer_version_id,
                "Gate 3.5 answer",
            )

    def latest_answer(
        self,
        case_id: str,
    ) -> AnswerVersion | None:
        with self._lock:
            answers = tuple(
                item
                for item in self._answers.values()
                if item.case_id == case_id
            )
            return (
                None
                if not answers
                else max(answers, key=lambda item: item.version_number)
            )

    def get_settlement_precondition(
        self,
        settlement_precondition_report_id: str,
    ) -> SettlementPreconditionReport:
        with self._lock:
            return _get(
                self._settlement_preconditions,
                settlement_precondition_report_id,
                "Gate 3.5 settlement precondition",
            )

    def land_capability_result(
        self,
        *,
        envelope: CapabilityResultEnvelope,
        receipt: CapabilityResultReceipt,
        job_lease: JobLease,
        event_id: str,
        recorded_at: datetime,
    ) -> CapabilityResultReceipt:
        """Persist T1 without deciding whether Evidence is currently usable."""

        with self.atomic():
            self.assert_job_lease(job_lease, checked_at=recorded_at)
            if job_lease.outbox_message_id != envelope.outbox_message_id:
                raise InvalidAuthorityTransition(
                    "result lease does not bind the capability outbox"
                )
            validate_capability_result_envelope(envelope)
            validate_capability_result_receipt(
                receipt=receipt,
                envelope=envelope,
            )
            if (
                receipt.delivery_owner_id != job_lease.owner_id
                or receipt.delivery_fencing_token
                != job_lease.fencing_token
            ):
                raise InvalidAuthorityTransition(
                    "result receipt changes the delivery lease fence"
                )
            existing_receipt_id = (
                self._capability_result_receipt_by_outbox.get(
                    envelope.outbox_message_id
                )
            )
            if existing_receipt_id is not None:
                existing = self._capability_result_receipts[
                    existing_receipt_id
                ]
                if replace(
                    receipt,
                    capability_result_receipt_id=(
                        existing.capability_result_receipt_id
                    ),
                    received_at=existing.received_at,
                    delivery_owner_id=existing.delivery_owner_id,
                    delivery_fencing_token=(
                        existing.delivery_fencing_token
                    ),
                ) == existing:
                    return existing
                raise AuthorityConflict(
                    "capability outbox already landed different result"
                )
            schedule = self.get_obligation_schedule(
                envelope.schedule_id
            )
            dispatch = _get(
                self._obligation_dispatch_records,
                envelope.dispatch_record_id,
                "obligation dispatch",
            )
            message = self.get_outbox_message(
                envelope.outbox_message_id
            )
            try:
                validate_obligation_dispatch_admission(
                    schedule=schedule,
                    record=dispatch,
                    message=message,
                )
            except ValueError as error:
                raise InvalidAuthorityTransition(str(error)) from error
            if (
                dispatch.schedule_id != envelope.schedule_id
                or dispatch.outbox_message_id
                != envelope.outbox_message_id
                or dispatch.dispatch.obligation_id
                != envelope.obligation_id
                or dispatch.dispatch.query_binding_id
                != envelope.query_binding_id
                or schedule.correlation_id != envelope.run_id
                or receipt.operation_identity.authority_revision
                != message.expected_authority_epoch
            ):
                raise InvalidAuthorityTransition(
                    "capability result changes sealed dispatch"
                )
            binding = _get(
                self._query_bindings,
                envelope.query_binding_id,
                "query binding",
            )
            obligation = self.get_evidence_obligation(
                envelope.obligation_id
            )
            outcome = self.get_measurement_resolution(
                binding.resolution_outcome_id
            )
            attempt = _get(
                self._logical_execution_attempts,
                envelope.logical_execution_attempt_id,
                "logical execution attempt",
            )
            if (
                attempt.content_sha256
                != envelope.logical_execution_attempt_content_sha256
                or attempt.query_binding_id
                != envelope.query_binding_id
            ):
                raise InvalidAuthorityTransition(
                    "capability result changes logical attempt"
                )
            spec = self.get_conformance_execution_spec(
                attempt.conformance_execution_spec_id
            )
            prior_attempt = (
                None
                if attempt.prior_attempt_id is None
                else _get(
                    self._logical_execution_attempts,
                    attempt.prior_attempt_id,
                    "prior logical execution attempt",
                )
            )
            try:
                expected_provenance = (
                    build_conformance_execution_provenance(
                        binding=binding,
                        spec=spec,
                        attempt=attempt,
                        current_authority=(
                            schedule.authority_snapshot
                        ),
                        prior_attempt=prior_attempt,
                    )
                )
            except ValueError as error:
                raise InvalidAuthorityTransition(str(error)) from error
            if (
                envelope.evidence_record.execution_provenance
                != expected_provenance
            ):
                raise InvalidAuthorityTransition(
                    "capability result changes sealed execution provenance"
                )
            try:
                validate_evidence_record_authority(
                    record=envelope.evidence_record,
                    binding=binding,
                    obligation=obligation,
                    outcome=outcome,
                )
            except ValueError as error:
                raise InvalidAuthorityTransition(str(error)) from error
            _put_immutable(
                self._capability_result_envelopes,
                envelope.capability_result_envelope_id,
                envelope,
                "capability result envelope",
            )
            _put_immutable(
                self._evidence,
                envelope.evidence_record.evidence_record_id,
                envelope.evidence_record,
                "Gate 3.5 evidence",
            )
            _put_immutable(
                self._capability_result_receipts,
                receipt.capability_result_receipt_id,
                receipt,
                "capability result receipt",
            )
            self._capability_result_receipt_by_outbox[
                envelope.outbox_message_id
            ] = receipt.capability_result_receipt_id
            self._append_authority_event_locked(
                case_id=envelope.case_id,
                event_id=event_id,
                event_type=JournalEventType.CAPABILITY_RESULT_LANDED,
                authority_ref=receipt.capability_result_receipt_id,
                action_id=None,
                recorded_at=recorded_at,
                payload={
                    "receipt_id": receipt.capability_result_receipt_id,
                    "envelope_id": (
                        envelope.capability_result_envelope_id
                    ),
                    "evidence_record_id": (
                        envelope.evidence_record.evidence_record_id
                    ),
                    "outbox_message_id": envelope.outbox_message_id,
                },
                operation=receipt.operation_identity,
            )
            self._append_authority_event_locked(
                case_id=envelope.case_id,
                event_id=content_sha256(
                    {
                        "base_event_id": event_id,
                        "event_type": (
                            JournalEventType.EVIDENCE_RECORDED.value
                        ),
                        "authority_ref": (
                            envelope.evidence_record.evidence_record_id
                        ),
                    }
                ),
                event_type=JournalEventType.EVIDENCE_RECORDED,
                authority_ref=(
                    envelope.evidence_record.evidence_record_id
                ),
                action_id=None,
                recorded_at=recorded_at,
                payload={
                    "content_sha256": (
                        envelope.evidence_record.content_sha256
                    ),
                    "capability_result_receipt_id": (
                        receipt.capability_result_receipt_id
                    ),
                },
                operation=receipt.operation_identity,
            )
            return receipt

    def admit_landed_result(
        self,
        *,
        receipt_id: str,
        profile: EvidenceAdmissionProfile,
        event_id: str,
        recorded_at: datetime,
    ) -> tuple[
        EvidenceAdmissionRecord,
        EvidenceValidityRecord,
        ObligationSatisfactionRecord,
    ]:
        """Derive T2 from persisted T1 facts and current authority."""

        with self.atomic():
            existing_id = self._evidence_admission_by_receipt.get(
                (receipt_id, profile)
            )
            if existing_id is not None:
                admission = self._evidence_admissions[existing_id]
                canonical_validities = tuple(
                    item
                    for item in self._evidence_validity.values()
                    if item.evidence_admission_id
                    == admission.evidence_admission_id
                    and item.prior_evidence_validity_id is None
                )
                if len(canonical_validities) != 1:
                    raise InvalidAuthorityTransition(
                        "evidence admission lacks one canonical initial "
                        "validity"
                    )
                validity = canonical_validities[0]
                canonical_satisfactions = tuple(
                    item
                    for item in self._obligation_satisfaction.values()
                    if admission.evidence_admission_id
                    in item.evidence_admission_ids
                    and validity.evidence_validity_id
                    in item.evidence_validity_ids
                    and (
                        item.prior_obligation_satisfaction_id is None
                        or admission.evidence_admission_id
                        not in self._obligation_satisfaction[
                            item.prior_obligation_satisfaction_id
                        ].evidence_admission_ids
                    )
                )
                if len(canonical_satisfactions) != 1:
                    raise InvalidAuthorityTransition(
                        "evidence admission lacks one canonical initial "
                        "satisfaction"
                    )
                return (
                    admission,
                    validity,
                    canonical_satisfactions[0],
                )
            receipt = _get(
                self._capability_result_receipts,
                receipt_id,
                "capability result receipt",
            )
            envelope = _get(
                self._capability_result_envelopes,
                receipt.capability_result_envelope_id,
                "capability result envelope",
            )
            binding = _get(
                self._query_bindings,
                envelope.query_binding_id,
                "query binding",
            )
            obligation = self.get_evidence_obligation(
                envelope.obligation_id
            )
            outcome = self.get_measurement_resolution(
                binding.resolution_outcome_id
            )
            adoption = self.get_plan_adoption(
                envelope.plan_revision_id
            )
            frame = self.get_frame(envelope.frame_revision_id)
            expected_scope = next(
                (
                    item
                    for item in frame.measurement_design.scopes
                    if item.scope_id
                    == binding.requirement_binding.scope_id
                ),
                None,
            )
            if expected_scope is None:
                raise InvalidAuthorityTransition(
                    "query binding scope is absent from accepted Frame"
                )
            current = self.get_authority_snapshot(envelope.case_id)
            admission = build_evidence_admission(
                binding=binding,
                obligation=obligation,
                outcome=outcome,
                envelope=envelope,
                receipt=receipt,
                plan_adoption=adoption,
                expected_scope=expected_scope,
                current_authority=current,
                profile=profile,
                admitted_at=recorded_at,
            )
            validity = build_initial_evidence_validity(
                admission=admission,
                recorded_at=recorded_at,
            )
            prior_satisfaction = None
            prior_id = (
                self._satisfaction_head_by_obligation.get(
                    obligation.obligation_id
                )
            )
            if prior_id is not None:
                prior_satisfaction = (
                    self._obligation_satisfaction[prior_id]
                )
            obligation_admissions = tuple(
                sorted(
                    (
                        *(
                            item
                            for item in self._evidence_admissions.values()
                            if item.obligation_id
                            == obligation.obligation_id
                        ),
                        admission,
                    ),
                    key=lambda item: item.evidence_admission_id,
                )
            )
            current_validities = tuple(
                (
                    validity
                    if item.evidence_record_id
                    == admission.evidence_record_id
                    else self._evidence_validity[
                        self._validity_head_by_evidence[
                            item.evidence_record_id
                        ]
                    ]
                )
                for item in obligation_admissions
            )
            satisfaction = build_obligation_satisfaction(
                obligation=obligation,
                admissions=obligation_admissions,
                validities=current_validities,
                boundary_outcome=None,
                prior=prior_satisfaction,
                recorded_at=recorded_at,
            )
            _put_immutable(
                self._evidence_admissions,
                admission.evidence_admission_id,
                admission,
                "evidence admission",
            )
            self._evidence_admission_by_receipt[
                (receipt_id, profile)
            ] = admission.evidence_admission_id
            _put_immutable(
                self._evidence_validity,
                validity.evidence_validity_id,
                validity,
                "Gate 3.5 evidence validity",
            )
            self._validity_head_by_evidence[
                validity.evidence_record_id
            ] = validity.evidence_validity_id
            _put_immutable(
                self._obligation_satisfaction,
                satisfaction.obligation_satisfaction_id,
                satisfaction,
                "Gate 3.5 obligation satisfaction",
            )
            self._satisfaction_head_by_obligation[
                satisfaction.obligation_id
            ] = satisfaction.obligation_satisfaction_id
            event_materials = (
                (
                    JournalEventType.EVIDENCE_ADMISSION_RECORDED,
                    admission.evidence_admission_id,
                    admission.content_sha256,
                ),
                (
                    JournalEventType.EVIDENCE_VALIDITY_RECORDED,
                    validity.evidence_validity_id,
                    validity.content_sha256,
                ),
                (
                    JournalEventType.OBLIGATION_SATISFACTION_RECORDED,
                    satisfaction.obligation_satisfaction_id,
                    satisfaction.content_sha256,
                ),
            )
            for event_type, authority_ref, digest in event_materials:
                derived_event_id = content_sha256(
                    {
                        "base_event_id": event_id,
                        "event_type": event_type.value,
                        "authority_ref": authority_ref,
                    }
                )
                self._append_authority_event_locked(
                    case_id=envelope.case_id,
                    event_id=derived_event_id,
                    event_type=event_type,
                    authority_ref=authority_ref,
                    action_id=None,
                    recorded_at=recorded_at,
                    payload={"content_sha256": digest},
                    operation=receipt.operation_identity,
                )
            return admission, validity, satisfaction

    def transition_evidence_validity(
        self,
        *,
        evidence_record_id: str,
        status: EvidenceValidityStatus,
        reason_code: str,
        event_id: str,
        recorded_at: datetime,
    ) -> tuple[
        EvidenceValidityRecord,
        ObligationSatisfactionRecord,
    ]:
        with self.atomic():
            validity_event_id = content_sha256(
                {
                    "base_event_id": event_id,
                    "event_type": (
                        JournalEventType.EVIDENCE_VALIDITY_RECORDED.value
                    ),
                }
            )
            satisfaction_event_id = content_sha256(
                {
                    "base_event_id": event_id,
                    "event_type": (
                        JournalEventType.OBLIGATION_SATISFACTION_RECORDED.value
                    ),
                }
            )
            validity_event = self._events_by_id.get(validity_event_id)
            satisfaction_event = self._events_by_id.get(
                satisfaction_event_id
            )
            if validity_event is not None or satisfaction_event is not None:
                if (
                    validity_event is None
                    or satisfaction_event is None
                    or validity_event.authority_ref is None
                    or satisfaction_event.authority_ref is None
                ):
                    raise AuthorityConflict(
                        "validity transition replay is incomplete"
                    )
                replay_validity = _get(
                    self._evidence_validity,
                    validity_event.authority_ref,
                    "replayed evidence validity",
                )
                replay_satisfaction = _get(
                    self._obligation_satisfaction,
                    satisfaction_event.authority_ref,
                    "replayed obligation satisfaction",
                )
                if (
                    replay_validity.evidence_record_id
                    != evidence_record_id
                    or replay_validity.status is not status
                    or replay_validity.reason_code != reason_code
                ):
                    raise AuthorityConflict(
                        "validity transition event already has different content"
                    )
                return replay_validity, replay_satisfaction
            prior_id = _get(
                self._validity_head_by_evidence,
                evidence_record_id,
                "evidence validity head",
            )
            prior = self._evidence_validity[prior_id]
            successor = build_evidence_validity_successor(
                prior=prior,
                status=status,
                reason_code=reason_code,
                recorded_at=recorded_at,
            )
            admission = self._evidence_admissions[
                prior.evidence_admission_id
            ]
            obligation = self.get_evidence_obligation(
                admission.obligation_id
            )
            satisfaction_prior_id = (
                self._satisfaction_head_by_obligation[
                    obligation.obligation_id
                ]
            )
            satisfaction_prior = (
                self._obligation_satisfaction[
                    satisfaction_prior_id
                ]
            )
            admissions = tuple(
                sorted(
                    (
                        item
                        for item in self._evidence_admissions.values()
                        if item.obligation_id
                        == obligation.obligation_id
                    ),
                    key=lambda item: item.evidence_admission_id,
                )
            )
            validities = tuple(
                (
                    successor
                    if item.evidence_record_id == evidence_record_id
                    else self._evidence_validity[
                        self._validity_head_by_evidence[
                            item.evidence_record_id
                        ]
                    ]
                )
                for item in admissions
            )
            satisfaction = build_obligation_satisfaction(
                obligation=obligation,
                admissions=admissions,
                validities=validities,
                boundary_outcome=None,
                prior=satisfaction_prior,
                recorded_at=recorded_at,
            )
            _put_immutable(
                self._evidence_validity,
                successor.evidence_validity_id,
                successor,
                "Gate 3.5 evidence validity",
            )
            self._validity_head_by_evidence[
                evidence_record_id
            ] = successor.evidence_validity_id
            _put_immutable(
                self._obligation_satisfaction,
                satisfaction.obligation_satisfaction_id,
                satisfaction,
                "Gate 3.5 obligation satisfaction",
            )
            self._satisfaction_head_by_obligation[
                obligation.obligation_id
            ] = satisfaction.obligation_satisfaction_id
            for event_type, authority_ref, digest in (
                (
                    JournalEventType.EVIDENCE_VALIDITY_RECORDED,
                    successor.evidence_validity_id,
                    successor.content_sha256,
                ),
                (
                    JournalEventType.OBLIGATION_SATISFACTION_RECORDED,
                    satisfaction.obligation_satisfaction_id,
                    satisfaction.content_sha256,
                ),
            ):
                self._append_authority_event_locked(
                    case_id=admission.authority_fence.case_id,
                    event_id=content_sha256(
                        {
                            "base_event_id": event_id,
                            "event_type": event_type.value,
                        }
                    ),
                    event_type=event_type,
                    authority_ref=authority_ref,
                    action_id=None,
                    recorded_at=recorded_at,
                    payload={"content_sha256": digest},
                )
            return successor, satisfaction

    def accept_provisional_answer_candidate(
        self,
        *,
        candidate: ProvisionalAnswerCandidate,
        expected_head_version: int,
        event_id: str,
        recorded_at: datetime,
        operation: OperationIdentity | None = None,
    ) -> tuple[ProvisionalAnswerBundle, InvestigationCase]:
        """Derive claim use/prechecks and atomically admit provisional Answer."""

        with self.atomic():
            try:
                persisted_action = self.get_action(
                    candidate.created_by_action_id
                )
                validate_answer_candidate_action(
                    candidate=candidate,
                    persisted_action=persisted_action,
                )
            except (AuthorityNotFound, ValueError) as error:
                raise InvalidAuthorityTransition(str(error)) from error
            existing_candidate = self._answer_candidates.get(
                candidate.answer_candidate_id
            )
            if existing_candidate is not None:
                if existing_candidate != candidate:
                    raise AuthorityConflict(
                        "answer candidate ID has different content"
                    )
                answer = next(
                    (
                        item
                        for item in self._answers.values()
                        if item.answer_candidate_id
                        == candidate.answer_candidate_id
                    ),
                    None,
                )
                prechecks = tuple(
                    sorted(
                        (
                            item
                            for item in self._claim_prechecks.values()
                            if item.answer_candidate_id
                            == candidate.answer_candidate_id
                        ),
                        key=lambda item: tuple(
                            claim.proposal_claim_key
                            for claim in candidate.claims
                        ).index(item.proposal_claim_key),
                    )
                )
                bundle = ProvisionalAnswerBundle(
                    candidate=candidate,
                    prechecks=prechecks,
                    status=(
                        AnswerCandidateStatus.ACCEPTED_PROVISIONAL
                        if answer is not None
                        else AnswerCandidateStatus.REJECTED
                    ),
                    answer=answer,
                )
                current_case = self.get_case(candidate.case_id)
                current_authority = self.get_authority_snapshot(
                    candidate.case_id
                )
                if answer is not None:
                    if (
                        current_case.accepted_answer_version_id
                        != answer.answer_version_id
                        or not accepted_answer_candidate_is_current(
                            candidate=candidate,
                            current_authority=current_authority,
                        )
                    ):
                        raise StaleHead(
                            "answer candidate was superseded after acceptance"
                        )
                elif current_authority != candidate.authority_snapshot:
                    raise StaleHead(
                        "rejected answer candidate authority is stale"
                    )
                return bundle, current_case
            case = self._cas_case(
                candidate.case_id,
                expected_head_version,
            )
            current = self.get_authority_snapshot(case.case_id)
            adoption = self.get_plan_adoption(
                candidate.plan_revision_id
            )
            try:
                validate_provisional_answer_candidate(
                    candidate=candidate,
                    current_authority=current,
                    plan_adoption=adoption,
                )
            except ValueError as error:
                raise StaleHead(str(error)) from error
            supports_by_claim_key: dict[
                str, tuple[ClaimEvidenceSupport, ...]
            ] = {}
            satisfactions_by_claim_key: dict[
                str, tuple[ObligationSatisfactionRecord, ...]
            ] = {}
            created_uses: list[EvidenceUseBinding] = []
            for proposal in candidate.claims:
                supports: list[ClaimEvidenceSupport] = []
                for selection in proposal.evidence_selections:
                    evidence = self._evidence.get(
                        selection.evidence_record_id
                    )
                    if evidence is None:
                        continue
                    admissions = tuple(
                        item
                        for item in self._evidence_admissions.values()
                        if item.evidence_record_id
                        == evidence.evidence_record_id
                        and item.status
                        is EvidenceAdmissionStatus.ACCEPTED
                    )
                    if len(admissions) != 1:
                        continue
                    admission = admissions[0]
                    validity_id = (
                        self._validity_head_by_evidence.get(
                            evidence.evidence_record_id
                        )
                    )
                    if validity_id is None:
                        continue
                    validity = self._evidence_validity[
                        validity_id
                    ]
                    if validity.status is not (
                        EvidenceValidityStatus.ADMITTED_VALID
                    ):
                        continue
                    binding = _get(
                        self._query_bindings,
                        evidence.query_binding_id,
                        "query binding",
                    )
                    try:
                        use = build_evidence_use_binding(
                            evidence=evidence,
                            admission=admission,
                            validity=validity,
                            binding=binding,
                            answer_candidate_id=(
                                candidate.answer_candidate_id
                            ),
                            proposal_claim_key=(
                                proposal.proposal_claim_key
                            ),
                            claim_scope=proposal.applicability_scope,
                            requested_claim_strength=(
                                proposal.requested_strength
                            ),
                            bound_at=recorded_at,
                        )
                    except ValueError:
                        continue
                    created_uses.append(use)
                    supports.append(
                        ClaimEvidenceSupport(
                            evidence=evidence,
                            admission=admission,
                            validity=validity,
                            query_binding=binding,
                            use_binding=use,
                        )
                    )
                supports_by_claim_key[
                    proposal.proposal_claim_key
                ] = tuple(supports)
                satisfactions: list[
                    ObligationSatisfactionRecord
                ] = []
                for obligation_id in proposal.obligation_ids:
                    satisfaction_id = (
                        self._satisfaction_head_by_obligation.get(
                            obligation_id
                        )
                    )
                    if satisfaction_id is not None:
                        satisfactions.append(
                            self._obligation_satisfaction[
                                satisfaction_id
                            ]
                        )
                satisfactions_by_claim_key[
                    proposal.proposal_claim_key
                ] = tuple(satisfactions)
            bundle = compile_provisional_answer_bundle(
                candidate=candidate,
                current_authority=current,
                plan_adoption=adoption,
                supports_by_claim_key=supports_by_claim_key,
                satisfactions_by_claim_key=satisfactions_by_claim_key,
                check_dispositions_by_claim_key={},
                checked_at=recorded_at,
            )
            _put_immutable(
                self._answer_candidates,
                candidate.answer_candidate_id,
                candidate,
                "answer candidate",
            )
            for use in created_uses:
                _put_immutable(
                    self._evidence_use_bindings,
                    use.evidence_use_binding_id,
                    use,
                    "evidence use binding",
                )
            for precheck in bundle.prechecks:
                _put_immutable(
                    self._claim_prechecks,
                    precheck.claim_precheck_id,
                    precheck,
                    "claim precheck",
                )
            candidate_event_id = content_sha256(
                {
                    "base_event_id": event_id,
                    "kind": "answer-candidate",
                }
            )
            self._append_authority_event_locked(
                case_id=candidate.case_id,
                event_id=candidate_event_id,
                event_type=JournalEventType.ANSWER_CANDIDATE_RECORDED,
                authority_ref=candidate.answer_candidate_id,
                action_id=candidate.created_by_action_id,
                recorded_at=recorded_at,
                payload={
                    "content_sha256": candidate.content_sha256,
                    "candidate_status": bundle.status.value,
                },
                operation=operation,
            )
            for precheck in bundle.prechecks:
                self._append_authority_event_locked(
                    case_id=candidate.case_id,
                    event_id=content_sha256(
                        {
                            "base_event_id": event_id,
                            "kind": "claim-precheck",
                            "claim_precheck_id": (
                                precheck.claim_precheck_id
                            ),
                        }
                    ),
                    event_type=JournalEventType.CLAIM_PRECHECK_RECORDED,
                    authority_ref=precheck.claim_precheck_id,
                    action_id=candidate.created_by_action_id,
                    recorded_at=recorded_at,
                    payload={
                        "claim_id": precheck.claim_id,
                        "status": precheck.status.value,
                        "content_sha256": precheck.content_sha256,
                    },
                    operation=operation,
                )
            if bundle.answer is None:
                return bundle, case
            answer = bundle.answer
            if (
                self.get_authority_snapshot(case.case_id) != current
            ):
                raise StaleHead(
                    "answer inputs changed during bundle admission"
                )
            prior = max(
                (
                    item
                    for item in self._answers.values()
                    if item.case_id == case.case_id
                ),
                key=lambda item: item.version_number,
                default=None,
            )
            expected_version = 1 if prior is None else (
                prior.version_number + 1
            )
            expected_prior = (
                None if prior is None else prior.answer_version_id
            )
            if (
                answer.version_number != expected_version
                or answer.prior_answer_version_id != expected_prior
            ):
                raise InvalidAuthorityTransition(
                    "provisional Answer does not extend current Answer head"
                )
            _put_immutable(
                self._answers,
                answer.answer_version_id,
                answer,
                "Gate 3.5 answer",
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
            return bundle, updated

    def derive_settlement_precondition(
        self,
        *,
        case_id: str,
        expected_head_version: int,
        answer_version_id: str,
        objection_disposition_refs: tuple[str, ...],
        unresolved_blocking_objection_refs: tuple[str, ...],
        trace_manifest_id: str,
        trace_manifest_content_sha256: str,
        trace_complete: bool,
        event_id: str,
        recorded_at: datetime,
    ) -> SettlementPreconditionReport:
        with self.atomic():
            case = self._cas_case(case_id, expected_head_version)
            existing_event = self._events_by_id.get(event_id)
            existing_report = None
            if existing_event is not None:
                if (
                    existing_event.case_id != case_id
                    or existing_event.event_type
                    is not JournalEventType.SETTLEMENT_PRECONDITION_RECORDED
                    or existing_event.authority_ref is None
                ):
                    raise AuthorityConflict(
                        "settlement retry event identity is already in use"
                    )
                existing_report = _get(
                    self._settlement_preconditions,
                    existing_event.authority_ref,
                    "Gate 3.5 settlement precondition",
                )
            if case.accepted_answer_version_id != answer_version_id:
                raise InvalidAuthorityTransition(
                    "settlement derive request does not target current Answer"
                )
            answer = _get(
                self._answers,
                answer_version_id,
                "Gate 3.5 answer",
            )
            candidate = _get(
                self._answer_candidates,
                answer.answer_candidate_id,
                "answer candidate",
            )
            prechecks = tuple(
                self._claim_prechecks[item]
                for item in answer.claim_precheck_ids
            )
            supports: list[ClaimEvidenceSupport] = []
            for claim in answer.claims:
                for use_id in claim.evidence_use_binding_ids:
                    use = _get(
                        self._evidence_use_bindings,
                        use_id,
                        "evidence use binding",
                    )
                    evidence = self._evidence[
                        use.evidence_record_id
                    ]
                    admission = self._evidence_admissions[
                        use.evidence_admission_id
                    ]
                    current_validity_id = (
                        self._validity_head_by_evidence[
                            evidence.evidence_record_id
                        ]
                    )
                    validity = self._evidence_validity[
                        current_validity_id
                    ]
                    binding = self._query_bindings[
                        evidence.query_binding_id
                    ]
                    supports.append(
                        ClaimEvidenceSupport(
                            evidence=evidence,
                            admission=admission,
                            validity=validity,
                            query_binding=binding,
                            use_binding=use,
                        )
                    )
            obligation_ids = {
                obligation_id
                for claim in answer.claims
                for obligation_id in claim.obligation_ids
            }
            satisfactions = tuple(
                self._obligation_satisfaction[
                    self._satisfaction_head_by_obligation[
                        obligation_id
                    ]
                ]
                for obligation_id in sorted(obligation_ids)
            )
            current = self.get_authority_snapshot(case_id)
            adoption = self.get_plan_adoption(
                answer.plan_revision_id
            )
            trace_manifest = self.get_run_trace_manifest(
                trace_manifest_id
            )
            try:
                trace_complete = validate_settlement_request(
                    answer=answer,
                    supports=tuple(supports),
                    trace_manifest=trace_manifest,
                    trace_manifest_content_sha256=(
                        trace_manifest_content_sha256
                    ),
                    trace_complete=trace_complete,
                    objections=self.list_reviewer_objections(case_id),
                    objection_disposition_refs=(
                        objection_disposition_refs
                    ),
                    unresolved_blocking_objection_refs=(
                        unresolved_blocking_objection_refs
                    ),
                )
            except ValueError as error:
                raise InvalidAuthorityTransition(str(error)) from error
            report = derive_settlement_precondition_report(
                answer=answer,
                candidate=candidate,
                prechecks=prechecks,
                supports=tuple(supports),
                satisfactions=satisfactions,
                current_authority=current,
                plan_adoption=adoption,
                objection_disposition_refs=(
                    objection_disposition_refs
                ),
                unresolved_blocking_objection_refs=(
                    unresolved_blocking_objection_refs
                ),
                trace_manifest_id=trace_manifest_id,
                trace_manifest_content_sha256=(
                    trace_manifest_content_sha256
                ),
                trace_complete=trace_complete,
                created_at=(
                    existing_report.created_at
                    if existing_report is not None
                    else recorded_at
                ),
            )
            if existing_report is not None:
                if report != existing_report:
                    raise AuthorityConflict(
                        "settlement retry changed canonical inputs"
                    )
                return existing_report
            _put_immutable(
                self._settlement_preconditions,
                report.settlement_precondition_report_id,
                report,
                "Gate 3.5 settlement precondition",
            )
            self._append_authority_event_locked(
                case_id=case_id,
                event_id=event_id,
                event_type=(
                    JournalEventType.SETTLEMENT_PRECONDITION_RECORDED
                ),
                authority_ref=(
                    report.settlement_precondition_report_id
                ),
                action_id=None,
                recorded_at=recorded_at,
                payload={
                    "answer_version_id": answer_version_id,
                    "status": report.status.value,
                    "content_sha256": report.content_sha256,
                },
            )
            return report

    def record_measurement_resolution(
        self,
        outcome: MeasurementResolutionOutcome,
        *,
        admission: MeasurementResolutionAdmission,
        expected_head_version: int,
        event_id: str,
        operation: OperationIdentity | None = None,
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
            current_authority = self.get_authority_snapshot(
                outcome.case_id
            )
            if not outcome.derivation_authority.matches(
                current_authority
            ):
                raise StaleHead(
                    "measurement derivation authority is stale"
                )
            if (
                operation is not None
                and operation.authority_revision
                != outcome.derivation_authority.mailbox_authority_epoch
            ):
                raise StaleHead(
                    "measurement operation authority is stale"
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
                operation=operation,
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

    def list_measurement_resolutions(
        self,
        frame_revision_id: str,
    ) -> tuple[MeasurementResolutionOutcome, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        item
                        for item in self._resolution_outcomes.values()
                        if item.frame_revision_id == frame_revision_id
                    ),
                    key=lambda item: item.estimand_id,
                )
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
        operation: OperationIdentity | None = None,
    ) -> ResolvedEvidenceObligation:
        with self._lock:
            case = self._cas_case(
                obligation.case_id,
                expected_head_version,
            )
            current_authority = self.get_authority_snapshot(
                obligation.case_id
            )
            if not obligation.derivation_authority.matches(
                current_authority
            ):
                raise StaleHead(
                    "obligation derivation authority is stale"
                )
            if (
                operation is not None
                and operation.authority_revision
                != obligation.derivation_authority.mailbox_authority_epoch
            ):
                raise StaleHead(
                    "obligation operation authority is stale"
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
                or outcome.derivation_authority
                != obligation.derivation_authority
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
            try:
                validate_evidence_obligation_derivation(
                    frame=frame,
                    outcome=outcome,
                    obligation=obligation,
                )
            except ValueError as error:
                raise InvalidAuthorityTransition(str(error)) from error
            return self._record_derived_locked(
                records=self._evidence_obligations,
                record_id=obligation.obligation_id,
                record=obligation,
                case_id=obligation.case_id,
                event_id=event_id,
                event_type=JournalEventType.EVIDENCE_OBLIGATION_RECORDED,
                created_at=obligation.created_at,
                label="evidence obligation",
                operation=operation,
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

    def list_evidence_obligations(
        self,
        frame_revision_id: str,
    ) -> tuple[ResolvedEvidenceObligation, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        item
                        for item in self._evidence_obligations.values()
                        if item.frame_revision_id == frame_revision_id
                    ),
                    key=lambda item: (
                        item.estimand_id,
                        item.evidence_requirement_id,
                        item.obligation_id,
                    ),
                )
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
            for evidence_id, admission_id, validity_id in zip(
                interpretation.evidence_record_ids,
                interpretation.evidence_admission_ids,
                interpretation.evidence_validity_ids,
                strict=True,
            ):
                evidence = self.get_evidence(evidence_id)
                admission = self.get_evidence_admission(admission_id)
                validity = self.get_evidence_validity(validity_id)
                if (
                    evidence.case_id != interpretation.case_id
                    or evidence.frame_revision_id
                    != interpretation.frame_revision_id
                    or admission.evidence_record_id != evidence_id
                    or admission.status
                    is not EvidenceAdmissionStatus.ACCEPTED
                    or validity.evidence_record_id != evidence_id
                    or validity.evidence_admission_id != admission_id
                    or validity.status
                    is not EvidenceValidityStatus.ADMITTED_VALID
                    or self._validity_head_by_evidence.get(
                        evidence_id
                    )
                    != validity_id
                ):
                    raise InvalidAuthorityTransition(
                        "interpretation requires currently admitted evidence"
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
            if message.job_kind is AsyncJobKind.OBLIGATION:
                raise InvalidAuthorityTransition(
                    "obligation outbox requires schedule dispatch admission"
                )
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
            source_event = self._require_event_cursor(
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
                is_answer_review = (
                    message.job_kind is AsyncJobKind.REVIEWER
                    and action.action.kind is ActionKind.PROPOSE_ANSWER
                    and message.payload.get("answer_candidate_id")
                    and message.payload.get("answer_version_id")
                )
                if (
                    action.action.kind not in _EFFECT_ACTION_KINDS
                    and not is_frame_review
                    and not is_answer_review
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
                elif is_answer_review:
                    if (
                        message.contract_ref
                        != ANSWER_REVIEW_JOB_CONTRACT_REF
                    ):
                        raise InvalidAuthorityTransition(
                            "answer review outbox uses wrong contract"
                        )
                    answer_id = message.payload.get(
                        "answer_version_id"
                    )
                    candidate_id = message.payload.get(
                        "answer_candidate_id"
                    )
                    if (
                        not isinstance(answer_id, str)
                        or not isinstance(candidate_id, str)
                        or case.accepted_answer_version_id != answer_id
                    ):
                        raise InvalidAuthorityTransition(
                            "answer review outbox does not target current Answer"
                        )
                    answer = self.get_answer(answer_id)
                    candidate = self.get_answer_candidate(candidate_id)
                    expected_payload = {
                        "answer_candidate_id": (
                            candidate.answer_candidate_id
                        ),
                        "answer_candidate_content_sha256": (
                            candidate.content_sha256
                        ),
                        "answer_version_id": answer.answer_version_id,
                        "answer_version_content_sha256": (
                            answer.content_sha256
                        ),
                        "claim_precheck_ids": (
                            answer.claim_precheck_ids
                        ),
                        "claim_precheck_content_sha256s": (
                            answer.claim_precheck_content_sha256s
                        ),
                    }
                    if (
                        answer.answer_candidate_id
                        != candidate.answer_candidate_id
                        or candidate.created_by_action_id
                        != action.action.action_id
                        or dict(message.payload) != expected_payload
                        or source_event.event_type
                        is not JournalEventType.REVIEWER_JOB_ENQUEUED
                        or source_event.authority_ref
                        != answer.answer_version_id
                    ):
                        raise InvalidAuthorityTransition(
                            "answer review outbox changes accepted Answer bundle"
                        )
                elif (
                    message.payload.get("action_kind")
                    != action.action.kind.value
                ):
                    raise InvalidAuthorityTransition(
                        "outbox payload kind does not match action"
                    )
                if action.action.kind in _EFFECT_ACTION_KINDS:
                    admission_event = next(
                        (
                            event
                            for event in self.list_events(
                                message.case_id
                            )
                            if (
                                event.event_type
                                is JournalEventType.ACTION_ADMITTED
                                and event.action_id
                                == action.action.action_id
                            )
                        ),
                        None,
                    )
                    receipt = self.get_action_receipt(
                        action.action.case_id,
                        action.action.idempotency_key,
                    )
                    if admission_event is None or receipt is None:
                        raise InvalidAuthorityTransition(
                            "effect outbox lacks admission proof"
                        )
                    current_plan = (
                        None
                        if case.accepted_plan_revision_id is None
                        else self.get_plan(
                            case.accepted_plan_revision_id
                        )
                    )
                    try:
                        validate_effect_outbox_binding(
                            case=case,
                            message=message,
                            action=action.action,
                            admission_event=admission_event,
                            source_event=source_event,
                            receipt=receipt,
                            current_plan=current_plan,
                            current_query_bindings=(
                                ()
                                if current_plan is None
                                else self.list_query_bindings(
                                    current_plan.plan_revision_id
                                )
                            ),
                        )
                    except ValueError as error:
                        raise InvalidAuthorityTransition(
                            str(error)
                        ) from error
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
                trusted_now = self._trusted_now()
                if (
                    lease is None
                    or lease.owner_id != disposition.owner_id
                    or lease.fencing_token != disposition.fencing_token
                    or lease.expires_at <= trusted_now
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
        lease_duration = expires_at - now
        if lease_duration <= timedelta(0):
            raise ValueError("job lease duration must be positive")
        with self._lock:
            trusted_now = self._trusted_now()
            self.get_outbox_message(outbox_message_id)
            if outbox_message_id in self._job_dispositions:
                raise LeaseConflict(
                    "terminally disposed job cannot be claimed"
                )
            current = self._job_leases.get(outbox_message_id)
            if (
                current is not None
                and current.expires_at > trusted_now
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
                acquired_at=trusted_now,
                heartbeat_at=trusted_now,
                expires_at=trusted_now + lease_duration,
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
        lease_duration = expires_at - heartbeat_at
        if lease_duration <= timedelta(0):
            raise ValueError("job lease duration must be positive")
        with self._lock:
            trusted_now = self._trusted_now()
            current = self._job_leases.get(lease.outbox_message_id)
            if current != lease:
                raise LeaseFenceLost("job delivery lease fencing token was lost")
            if current.expires_at <= trusted_now:
                raise LeaseFenceLost(
                    "expired job delivery lease cannot be renewed"
                )
            renewed = replace(
                lease,
                heartbeat_at=trusted_now,
                expires_at=trusted_now + lease_duration,
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
        del checked_at
        with self._lock:
            trusted_now = self._trusted_now()
            current = self._job_leases.get(lease.outbox_message_id)
            if current != lease or current.expires_at <= trusted_now:
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
        lease_duration = expires_at - now
        if lease_duration <= timedelta(0):
            raise ValueError("controller lease duration must be positive")
        with self._lock:
            trusted_now = self._trusted_now()
            self.get_case(case_id)
            current = self._leases.get(case_id)
            if (
                current is not None
                and current.expires_at > trusted_now
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
                acquired_at=trusted_now,
                expires_at=trusted_now + lease_duration,
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

    def resolve_workflow_event_authority(
        self,
        event: EventJournalEntry,
    ) -> WorkflowEventAuthority:
        """Resolve immutable records used by the closed journal adapter."""

        with self._lock:
            persisted = self._events_by_id.get(event.event_id)
            if persisted != event:
                raise InvalidAuthorityTransition(
                    "Workflow event is absent from the durable journal"
                )
            if event.event_type is JournalEventType.PLAN_ACCEPTED:
                plan = self.get_plan(event.authority_ref or "")
                adoption = self.get_plan_adoption(
                    plan.plan_revision_id
                )
                obligations = tuple(
                    self.get_evidence_obligation(obligation_id)
                    for obligation_id in adoption.obligation_ids
                )
                return AcceptedPlanAuthority(
                    plan=plan,
                    adoption=adoption,
                    question=self.get_question(
                        adoption.question_revision_id
                    ),
                    frame=self.get_frame(adoption.frame_revision_id),
                    obligations=obligations,
                )
            if event.event_type is JournalEventType.QUESTION_ACCEPTED:
                return SupersedingAuthority(
                    question=self.get_question(
                        event.authority_ref or ""
                    )
                )
            if event.event_type is JournalEventType.FRAME_ACCEPTED:
                return SupersedingAuthority(
                    frame=self.get_frame(event.authority_ref or "")
                )
            if (
                event.event_type
                is JournalEventType.OBLIGATION_DISPATCH_ENQUEUED
            ):
                record = _get(
                    self._obligation_dispatch_records,
                    event.authority_ref or "",
                    "Workflow dispatch authority",
                )
                return DispatchAuthority(
                    schedule=self.get_obligation_schedule(
                        record.schedule_id
                    ),
                    dispatch=record,
                )
            if (
                event.event_type
                is JournalEventType.OBLIGATION_SCHEDULE_CHECKPOINTED
            ):
                checkpoint = _get(
                    self._obligation_schedule_checkpoints,
                    event.authority_ref or "",
                    "obligation schedule checkpoint",
                )
                return CheckpointAuthority(
                    schedule=self.get_obligation_schedule(
                        checkpoint.schedule_id
                    ),
                    checkpoint=checkpoint,
                    completions=self.list_obligation_completions(
                        checkpoint.schedule_id
                    ),
                )
            if (
                event.event_type
                is JournalEventType.OBLIGATION_SATISFACTION_RECORDED
            ):
                return SatisfactionAuthority(
                    satisfaction=_get(
                        self._obligation_satisfaction,
                        event.authority_ref or "",
                        "Gate 3.5 obligation satisfaction",
                    )
                )
            if event.event_type is JournalEventType.ANSWER_ACCEPTED:
                return AnswerAuthority(
                    answer=_get(
                        self._answers,
                        event.authority_ref or "",
                        "Gate 3.5 answer",
                    )
                )
            return None

    def get_workflow_read_model(
        self,
        case_id: str,
        *,
        realm: ExecutionRealm,
        evidence_profile: EvidenceAdmissionProfile,
    ) -> WorkflowReadModel:
        with self._lock:
            self.get_case(case_id)
            current = self._workflow_read_models.get(case_id)
            if current is None:
                current = initial_workflow_read_model(
                    case_id,
                    realm=realm,
                    evidence_profile=evidence_profile,
                )
                self._workflow_read_models[case_id] = current
                return current
            if (
                current.snapshot.realm is not realm
                or current.snapshot.evidence_profile
                is not evidence_profile
            ):
                raise InvalidAuthorityTransition(
                    "Workflow realm or evidence profile cannot change"
                )
            return current

    def commit_workflow_read_model(
        self,
        model: WorkflowReadModel,
        *,
        expected_head_version: int,
        applied_at: datetime,
    ) -> WorkflowReadModel:
        """Persist one projection cursor with CAS and source-event proof."""

        require_aware_datetime(applied_at, "applied_at")
        with self._lock:
            self.get_case(model.head.case_id)
            current = self._workflow_read_models.get(
                model.head.case_id
            )
            if current is None:
                current = initial_workflow_read_model(
                    model.head.case_id,
                    realm=model.snapshot.realm,
                    evidence_profile=model.snapshot.evidence_profile,
                )
                self._workflow_read_models[model.head.case_id] = current
            if (
                current.snapshot.realm is not model.snapshot.realm
                or current.snapshot.evidence_profile
                is not model.snapshot.evidence_profile
            ):
                raise InvalidAuthorityTransition(
                    "Workflow realm or evidence profile cannot change"
                )
            if current == model:
                return current
            if current.head.version != expected_head_version:
                raise StaleHead(
                    "Workflow projection head changed before commit"
                )
            if (
                model.head.version != expected_head_version + 1
                or len(model.application_receipts)
                != len(current.application_receipts) + 1
                or model.application_receipts[:-1]
                != current.application_receipts
            ):
                raise InvalidAuthorityTransition(
                    "Workflow commit must append exactly one cursor"
                )
            receipt = model.application_receipts[-1]
            source_event = self._require_event_cursor(
                model.head.case_id,
                receipt.cursor,
            )
            if (
                source_event.event_id != receipt.source_event_id
                or source_event.content_sha256
                != receipt.source_event_sha256
            ):
                raise InvalidAuthorityTransition(
                    "Workflow receipt does not bind its source journal event"
                )
            self._workflow_read_models[model.head.case_id] = model
            return model

    def project_workflow_read_model(
        self,
        case_id: str,
        *,
        realm: ExecutionRealm,
        evidence_profile: EvidenceAdmissionProfile,
        applied_at: datetime,
    ) -> WorkflowReadModel:
        """Incrementally project every durable event under one store lock."""

        with self.atomic():
            model = self.get_workflow_read_model(
                case_id,
                realm=realm,
                evidence_profile=evidence_profile,
            )
            for event in self.list_events(
                case_id,
                after_cursor=model.head.last_applied_cursor,
            ):
                fact = journal_event_to_workflow_fact(
                    event,
                    current=model,
                    authority_resolver=self,
                )
                proposed = apply_workflow_fact(model, fact)
                model = self.commit_workflow_read_model(
                    proposed,
                    expected_head_version=model.head.version,
                    applied_at=applied_at,
                )
            self._validate_workflow_projection_against_case(model)
            return model

    def rebuild_workflow_read_model(
        self,
        case_id: str,
        *,
        realm: ExecutionRealm,
        evidence_profile: EvidenceAdmissionProfile,
    ) -> WorkflowReadModel:
        """Rebuild from cursor zero without trusting the persisted projection."""

        with self._lock:
            self.get_case(case_id)
            model = initial_workflow_read_model(
                case_id,
                realm=realm,
                evidence_profile=evidence_profile,
            )
            for event in self.list_events(case_id):
                fact = journal_event_to_workflow_fact(
                    event,
                    current=model,
                    authority_resolver=self,
                )
                model = apply_workflow_fact(model, fact)
            self._validate_workflow_projection_against_case(model)
            return model

    def _validate_workflow_projection_against_case(
        self,
        model: WorkflowReadModel,
    ) -> None:
        case = self.get_case(model.head.case_id)
        if (
            model.snapshot.case.active_plan_revision_id
            != case.accepted_plan_revision_id
            or model.snapshot.case.accepted_answer_version_id
            != case.accepted_answer_version_id
        ):
            raise InvalidAuthorityTransition(
                "Workflow projection differs from current case authority"
            )
        if case.accepted_plan_revision_id is not None and (
            model.snapshot.accepted_question_revision_id
            != case.accepted_question_revision_id
            or model.snapshot.accepted_frame_revision_id
            != case.accepted_frame_revision_id
        ):
            raise InvalidAuthorityTransition(
                "Workflow active Plan changes Question or Frame authority"
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
        operation: OperationIdentity | None = None,
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
            operation=operation,
        )
        return record

    def _snapshot(self) -> dict[str, object]:
        return {
            "_cases": self._cases.copy(),
            "_questions": self._questions.copy(),
            "_frames": self._frames.copy(),
            "_plans": self._plans.copy(),
            "_plan_adoptions": self._plan_adoptions.copy(),
            "_plan_adoption_by_plan": (
                self._plan_adoption_by_plan.copy()
            ),
            "_query_bindings": self._query_bindings.copy(),
            "_conformance_execution_specs": (
                self._conformance_execution_specs.copy()
            ),
            "_conformance_spec_by_logical_execution": (
                self._conformance_spec_by_logical_execution.copy()
            ),
            "_conformance_spec_by_query_binding": (
                self._conformance_spec_by_query_binding.copy()
            ),
            "_logical_execution_attempts": (
                self._logical_execution_attempts.copy()
            ),
            "_resolution_outcomes": self._resolution_outcomes.copy(),
            "_resolution_admissions": self._resolution_admissions.copy(),
            "_evidence_obligations": self._evidence_obligations.copy(),
            "_capability_result_envelopes": (
                self._capability_result_envelopes.copy()
            ),
            "_capability_result_receipts": (
                self._capability_result_receipts.copy()
            ),
            "_capability_result_receipt_by_outbox": (
                self._capability_result_receipt_by_outbox.copy()
            ),
            "_evidence": self._evidence.copy(),
            "_evidence_admissions": self._evidence_admissions.copy(),
            "_evidence_admission_by_receipt": (
                self._evidence_admission_by_receipt.copy()
            ),
            "_evidence_validity": (
                self._evidence_validity.copy()
            ),
            "_validity_head_by_evidence": (
                self._validity_head_by_evidence.copy()
            ),
            "_evidence_use_bindings": (
                self._evidence_use_bindings.copy()
            ),
            "_obligation_satisfaction": (
                self._obligation_satisfaction.copy()
            ),
            "_satisfaction_head_by_obligation": (
                self._satisfaction_head_by_obligation.copy()
            ),
            "_answer_candidates": self._answer_candidates.copy(),
            "_claim_prechecks": self._claim_prechecks.copy(),
            "_answers": self._answers.copy(),
            "_settlement_preconditions": (
                self._settlement_preconditions.copy()
            ),
            "_workflow_read_models": self._workflow_read_models.copy(),
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
    causal_operation_sha256 = content_sha256(causal_operation)
    return OperationIdentity(
        operation_id=f"event-operation:{event_id}",
        idempotency_key=(
            f"event-key:{event_id}:{causal_operation_sha256}"
        ),
        causation_id=causal_operation.operation_id,
        correlation_id=causal_operation.correlation_id,
        authority_revision=causal_operation.authority_revision,
        payload_sha256=content_sha256(payload),
    )
