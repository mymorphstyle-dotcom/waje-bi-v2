"""PostgreSQL adapter for the Gate 1 authority contract."""

from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Iterator, Mapping, TypeVar

import psycopg
from psycopg import Connection, Cursor, errors, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

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

from .codec import (
    decode_answer,
    decode_action_receipt,
    decode_checkpoint,
    decode_context_packet,
    decode_decision_request,
    decode_decision,
    decode_durable_model_result,
    decode_evidence,
    decode_evidence_obligation,
    decode_evidence_validity,
    decode_effect_attempt,
    decode_frame,
    decode_frame_admission_proof,
    decode_frame_candidate,
    decode_frame_candidate_supersession,
    decode_frame_review,
    decode_interpretation,
    decode_job_disposition,
    decode_logical_model_job,
    decode_message_impact_binding,
    decode_message_ingress_record,
    decode_objection,
    decode_objection_closure,
    decode_obligation_completion_record,
    decode_obligation_dispatch,
    decode_obligation_schedule,
    decode_obligation_schedule_checkpoint,
    decode_pending_user_message,
    decode_provider_attempt_receipt,
    decode_provider_attempt_request,
    decode_run_trace_manifest,
    decode_obligation_satisfaction,
    decode_outbox_message,
    decode_persisted_action,
    decode_plan,
    decode_question,
    decode_measurement_resolution,
    decode_measurement_resolution_admission,
    decode_settlement_precondition,
    encode_record,
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
ENVIRONMENT_VARIABLE = "WAJE_VNEXT_DATABASE_URL"
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


def apply_gate1_migration(
    dsn: str,
    *,
    migration_path: Path,
) -> str:
    """Apply schema v1 once and return the migration file checksum."""

    return _apply_migration(
        dsn,
        migration_path=migration_path,
        version=1,
        name="gate1_authority",
    )


def apply_gate2_migration(
    dsn: str,
    *,
    migration_path: Path,
) -> str:
    """Apply controller schema v2 once and return its checksum."""

    return _apply_migration(
        dsn,
        migration_path=migration_path,
        version=2,
        name="gate2_controller",
    )


def apply_gate3_1_migration(
    dsn: str,
    *,
    migration_path: Path,
) -> str:
    """Apply the clean epoch-3 measurement authority amendment."""

    return _apply_migration(
        dsn,
        migration_path=migration_path,
        version=3,
        name="gate3_1_measurement_authority",
    )


def apply_gate3_2_migration(
    dsn: str,
    *,
    migration_path: Path,
) -> str:
    """Apply the Gate 3.2 durable runtime saga amendment."""

    return _apply_migration(
        dsn,
        migration_path=migration_path,
        version=4,
        name="gate3_2_runtime_sagas",
    )


def _apply_migration(
    dsn: str,
    *,
    migration_path: Path,
    version: int,
    name: str,
) -> str:
    migration_bytes = migration_path.read_bytes()
    checksum = hashlib.sha256(migration_bytes).hexdigest()
    with psycopg.connect(dsn) as connection:
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT pg_advisory_xact_lock(
                    hashtext('waje_vnext_schema_migrations')
                )
                """
            )
            cursor.execute("SELECT to_regclass('waje_vnext.schema_migrations')")
            registry = cursor.fetchone()[0]
            if registry is not None:
                cursor.execute(
                    """
                    SELECT checksum_sha256
                    FROM waje_vnext.schema_migrations
                    WHERE version = %s
                    """,
                    (version,),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    if existing[0] != checksum:
                        raise AuthorityConflict(
                            "migration version {} checksum does not match".format(
                                version
                            )
                        )
                    return checksum
            cursor.execute(migration_bytes.decode("utf-8"))
            cursor.execute(
                """
                INSERT INTO waje_vnext.schema_migrations (
                    version, name, checksum_sha256
                ) VALUES (%s, %s, %s)
                """,
                (version, name, checksum),
            )
    return checksum


class PostgresAuthorityStore:
    """Transactional PostgreSQL implementation of the authority storage port."""

    def __init__(
        self,
        connection: Connection[Any],
        *,
        resolution_input_verifier: (
            TrustedResolutionInputVerifier | None
        ) = None,
    ) -> None:
        self._connection = connection
        self._lock = RLock()
        self._resolution_input_verifier = resolution_input_verifier

    @classmethod
    def connect(
        cls,
        dsn: str,
        *,
        resolution_input_verifier: (
            TrustedResolutionInputVerifier | None
        ) = None,
    ) -> "PostgresAuthorityStore":
        return cls(
            psycopg.connect(dsn),
            resolution_input_verifier=resolution_input_verifier,
        )

    @classmethod
    def from_env(cls) -> "PostgresAuthorityStore":
        dsn = os.environ.get(ENVIRONMENT_VARIABLE)
        if not dsn:
            raise RuntimeError("{} is required".format(ENVIRONMENT_VARIABLE))
        return cls.connect(dsn)

    def close(self) -> None:
        self._connection.close()

    @contextmanager
    def atomic(self) -> Iterator[None]:
        with self._lock, self._connection.transaction():
            yield

    def open_case(
        self,
        *,
        case_id: str,
        thread_id: str,
        event_id: str,
        opened_at: datetime,
        operation: OperationIdentity | None = None,
    ) -> InvestigationCase:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                cursor.execute(
                    """
                    SELECT pg_advisory_xact_lock(
                        hashtextextended(%s, 1729)
                    )
                    """,
                    (case_id,),
                )
                existing = self._event_by_id(cursor, event_id)
                if existing is not None:
                    if (
                        existing.event_type is JournalEventType.CASE_OPENED
                        and existing.case_id == case_id
                        and existing.payload.get("thread_id") == thread_id
                    ):
                        return self._get_case(cursor, case_id)
                    raise AuthorityConflict(
                        "event ID already has different content"
                    )
                try:
                    cursor.execute(
                        """
                        INSERT INTO waje_vnext.investigation_cases (
                            case_id,
                            thread_id,
                            lifecycle,
                            head_version,
                            analysis_cycle_id,
                            opened_at,
                            updated_at
                        ) VALUES (%s, %s, 'open', 0, %s, %s, %s)
                        """,
                        (
                            case_id,
                            thread_id,
                            f"{case_id}:cycle:0",
                            opened_at,
                            opened_at,
                        ),
                    )
                    cursor.execute(
                        """
                        INSERT INTO waje_vnext.event_stream_heads (
                            case_id, last_cursor
                        ) VALUES (%s, 0)
                        """,
                        (case_id,),
                    )
                    cursor.execute(
                        """
                        INSERT INTO waje_vnext.case_mailbox_heads (
                            case_id,
                            last_sequence,
                            authority_epoch,
                            updated_at
                        ) VALUES (%s, 0, 0, %s)
                        """,
                        (case_id, opened_at),
                    )
                except errors.UniqueViolation as error:
                    raise AuthorityConflict("case ID already exists") from error
                self._append_event(
                    cursor,
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
                return self._get_case(cursor, case_id)

    def get_case(self, case_id: str) -> InvestigationCase:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                return self._get_case(cursor, case_id)

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
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                self._get_case(cursor, case_id)
                cursor.execute(
                    """
                    SELECT *
                    FROM waje_vnext.case_mailbox_heads
                    WHERE case_id = %s
                    FOR UPDATE
                    """,
                    (case_id,),
                )
                head_row = cursor.fetchone()
                if head_row is None:
                    raise AuthorityNotFound(
                        "case mailbox head does not exist"
                    )
                cursor.execute(
                    """
                    SELECT *
                    FROM waje_vnext.case_mailbox_messages
                    WHERE message_id = %s
                       OR (
                           case_id = %s
                           AND idempotency_key = %s
                       )
                    """,
                    (message_id, case_id, operation.idempotency_key),
                )
                existing_row = cursor.fetchone()
                if existing_row is not None:
                    existing = _mailbox_message_from_row(existing_row)
                    if (
                        existing.case_id == case_id
                        and existing.kind is kind
                        and existing.operation == operation
                        and existing.payload == payload
                    ):
                        return existing
                    raise AuthorityConflict(
                        "mailbox identity already has different content"
                    )
                sequence = head_row["last_sequence"] + 1
                authority_epoch = head_row["authority_epoch"] + 1
                message = MailboxMessage(
                    message_id=message_id,
                    case_id=case_id,
                    sequence=sequence,
                    authority_epoch=authority_epoch,
                    kind=kind,
                    operation=operation,
                    payload=payload,
                    created_at=created_at,
                )
                cursor.execute(
                    """
                    INSERT INTO waje_vnext.case_mailbox_messages (
                        message_id,
                        case_id,
                        sequence,
                        authority_epoch,
                        message_kind,
                        operation_id,
                        idempotency_key,
                        causation_id,
                        correlation_id,
                        authority_revision,
                        payload_sha256,
                        payload,
                        created_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        message.message_id,
                        message.case_id,
                        message.sequence,
                        message.authority_epoch,
                        message.kind.value,
                        message.operation.operation_id,
                        message.operation.idempotency_key,
                        message.operation.causation_id,
                        message.operation.correlation_id,
                        message.operation.authority_revision,
                        message.operation.payload_sha256,
                        Jsonb(encode_record(message)["payload"]),
                        message.created_at,
                    ),
                )
                cursor.execute(
                    """
                    UPDATE waje_vnext.case_mailbox_heads
                    SET last_sequence = %s,
                        authority_epoch = %s,
                        updated_at = %s
                    WHERE case_id = %s
                    """,
                    (
                        message.sequence,
                        message.authority_epoch,
                        message.created_at,
                        message.case_id,
                    ),
                )
                return message

    def get_mailbox_head(self, case_id: str) -> MailboxHead:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                self._get_case(cursor, case_id)
                cursor.execute(
                    """
                    SELECT *
                    FROM waje_vnext.case_mailbox_heads
                    WHERE case_id = %s
                    FOR UPDATE
                    """,
                    (case_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise AuthorityNotFound("case mailbox head does not exist")
                return MailboxHead(
                    case_id=row["case_id"],
                    last_sequence=row["last_sequence"],
                    authority_epoch=row["authority_epoch"],
                    updated_at=row["updated_at"],
                )

    def get_authority_snapshot(self, case_id: str) -> AuthoritySnapshot:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                return self._authority_snapshot_from_cursor(
                    cursor,
                    case_id,
                )

    def list_mailbox_messages(
        self,
        case_id: str,
        *,
        after_sequence: int = 0,
    ) -> tuple[MailboxMessage, ...]:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                self._get_case(cursor, case_id)
                cursor.execute(
                    """
                    SELECT *
                    FROM waje_vnext.case_mailbox_messages
                    WHERE case_id = %s
                      AND sequence > %s
                    ORDER BY sequence
                    """,
                    (case_id, after_sequence),
                )
                return tuple(
                    _mailbox_message_from_row(row)
                    for row in cursor.fetchall()
                )

    def record_message_ingress(
        self,
        record: MessageIngressRecord,
    ) -> MessageIngressRecord:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                cursor.execute(
                    """
                    SELECT *
                    FROM waje_vnext.case_mailbox_messages
                    WHERE message_id = %s
                    """,
                    (record.message_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise AuthorityNotFound(
                        "mailbox message does not exist"
                    )
                message = _mailbox_message_from_row(row)
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
                payload = encode_record(record)
                self._insert_idempotent_immutable(
                    cursor,
                    table="message_ingress_records",
                    id_column="ingress_record_id",
                    record_id=record.ingress_record_id,
                    columns=(
                        "case_id",
                        "message_id",
                        "run_id",
                        "authority_epoch",
                        "payload",
                    ),
                    values=(
                        record.case_id,
                        record.message_id,
                        record.run_id,
                        record.authority_epoch,
                        Jsonb(payload),
                    ),
                    payload=payload,
                    label="message ingress record",
                )
                return record

    def list_message_ingress_records(
        self,
        case_id: str,
    ) -> tuple[MessageIngressRecord, ...]:
        return self._list_payloads(
            table="message_ingress_records",
            case_id=case_id,
            order_by=("authority_epoch", "ingress_record_id"),
            decoder=decode_message_ingress_record,
        )

    def record_pending_user_message(
        self,
        record: PendingUserMessage,
    ) -> PendingUserMessage:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                ingress = self._get_payload(
                    cursor,
                    table="message_ingress_records",
                    id_column="ingress_record_id",
                    record_id=record.ingress_record_id,
                    label="message ingress record",
                    decoder=decode_message_ingress_record,
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
                payload = encode_record(record)
                self._insert_idempotent_immutable(
                    cursor,
                    table="pending_user_messages",
                    id_column="pending_message_id",
                    record_id=record.pending_message_id,
                    columns=(
                        "ingress_record_id",
                        "binding_job_id",
                        "payload",
                    ),
                    values=(
                        record.ingress_record_id,
                        record.binding_job_id,
                        Jsonb(payload),
                    ),
                    payload=payload,
                    label="pending user message",
                )
                return record

    def get_pending_user_message(
        self,
        pending_message_id: str,
    ) -> PendingUserMessage:
        return self._get_authority(
            table="pending_user_messages",
            id_column="pending_message_id",
            record_id=pending_message_id,
            label="pending user message",
            decoder=decode_pending_user_message,
        )

    def record_message_impact_binding(
        self,
        binding: MessageImpactBinding,
    ) -> MessageImpactBinding:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                pending = self._get_payload(
                    cursor,
                    table="pending_user_messages",
                    id_column="pending_message_id",
                    record_id=binding.pending_message_id,
                    label="pending user message",
                    decoder=decode_pending_user_message,
                )
                cursor.execute(
                    """
                    SELECT *
                    FROM waje_vnext.case_mailbox_messages
                    WHERE message_id = %s
                    """,
                    (pending.message_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise AuthorityNotFound(
                        "mailbox message does not exist"
                    )
                message = _mailbox_message_from_row(row)
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
                payload = encode_record(binding)
                try:
                    self._insert_idempotent_immutable(
                        cursor,
                        table="message_impact_bindings",
                        id_column="binding_id",
                        record_id=binding.binding_id,
                        columns=(
                            "pending_message_id",
                            "case_id",
                            "authority_epoch",
                            "disposition",
                            "semantic_binding_sha256",
                            "payload",
                        ),
                        values=(
                            binding.pending_message_id,
                            binding.case_id,
                            binding.authority_epoch,
                            binding.disposition.value,
                            binding.semantic_binding_sha256,
                            Jsonb(payload),
                        ),
                        payload=payload,
                        label="message impact binding",
                    )
                except errors.UniqueViolation as error:
                    raise AuthorityConflict(
                        "pending message already has another binding"
                    ) from error
                return binding

    def get_message_impact_binding(
        self,
        binding_id: str,
    ) -> MessageImpactBinding:
        return self._get_authority(
            table="message_impact_bindings",
            id_column="binding_id",
            record_id=binding_id,
            label="message impact binding",
            decoder=decode_message_impact_binding,
        )

    def list_message_impact_bindings(
        self,
        case_id: str,
    ) -> tuple[MessageImpactBinding, ...]:
        return self._list_payloads(
            table="message_impact_bindings",
            case_id=case_id,
            order_by=("authority_epoch", "binding_id"),
            decoder=decode_message_impact_binding,
        )

    def record_logical_model_job(
        self,
        record: LogicalModelJob,
    ) -> LogicalModelJob:
        payload = encode_record(record)
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                outbox = self._get_payload(
                    cursor,
                    table="outbox_messages",
                    id_column="outbox_message_id",
                    record_id=record.job_id,
                    label="outbox message",
                    decoder=decode_outbox_message,
                )
                if (
                    outbox.case_id != record.case_id
                    or outbox.operation.operation_id
                    != record.operation_id
                    or outbox.authority_snapshot_sha256
                    != record.authority_snapshot_sha256
                ):
                    raise InvalidAuthorityTransition(
                        "logical model job does not bind its outbox authority"
                    )
                try:
                    self._insert_idempotent_immutable(
                        cursor,
                        table="logical_model_jobs",
                        id_column="logical_model_job_id",
                        record_id=record.logical_model_job_id,
                        columns=(
                            "case_id",
                            "job_id",
                            "authority_snapshot_sha256",
                            "payload",
                        ),
                        values=(
                            record.case_id,
                            record.job_id,
                            record.authority_snapshot_sha256,
                            Jsonb(payload),
                        ),
                        payload=payload,
                        label="logical model job",
                    )
                except errors.UniqueViolation as error:
                    raise AuthorityConflict(
                        "outbox already has another logical model job"
                    ) from error
                return record

    def get_logical_model_job(
        self,
        logical_model_job_id: str,
    ) -> LogicalModelJob:
        return self._get_authority(
            table="logical_model_jobs",
            id_column="logical_model_job_id",
            record_id=logical_model_job_id,
            label="logical model job",
            decoder=decode_logical_model_job,
        )

    def list_logical_model_jobs(
        self,
        case_id: str,
    ) -> tuple[LogicalModelJob, ...]:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                self._get_case(cursor, case_id)
                cursor.execute(
                    """
                    SELECT payload
                    FROM waje_vnext.logical_model_jobs
                    WHERE case_id = %s
                    ORDER BY logical_model_job_id
                    """,
                    (case_id,),
                )
                return tuple(
                    decode_logical_model_job(row["payload"])
                    for row in cursor.fetchall()
                )

    def record_provider_attempt_request(
        self,
        record: ProviderAttemptRequest,
    ) -> ProviderAttemptRequest:
        payload = encode_record(record)
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                self._get_payload(
                    cursor,
                    table="logical_model_jobs",
                    id_column="logical_model_job_id",
                    record_id=record.logical_model_job_id,
                    label="logical model job",
                    decoder=decode_logical_model_job,
                )
                try:
                    self._insert_idempotent_immutable(
                        cursor,
                        table="provider_attempt_requests",
                        id_column="provider_attempt_id",
                        record_id=record.provider_attempt_id,
                        columns=(
                            "logical_model_job_id",
                            "attempt_number",
                            "prior_provider_attempt_id",
                            "payload",
                        ),
                        values=(
                            record.logical_model_job_id,
                            record.attempt_number,
                            record.prior_provider_attempt_id,
                            Jsonb(payload),
                        ),
                        payload=payload,
                        label="provider attempt request",
                    )
                except errors.UniqueViolation as error:
                    raise AuthorityConflict(
                        "logical job attempt number already exists"
                    ) from error
                return record

    def record_provider_attempt_receipt(
        self,
        record: ProviderAttemptReceipt,
    ) -> ProviderAttemptReceipt:
        payload = encode_record(record)
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                request = self._get_payload(
                    cursor,
                    table="provider_attempt_requests",
                    id_column="provider_attempt_id",
                    record_id=record.provider_attempt_id,
                    label="provider attempt request",
                    decoder=decode_provider_attempt_request,
                )
                if (
                    request.logical_model_job_id
                    != record.logical_model_job_id
                ):
                    raise InvalidAuthorityTransition(
                        "provider receipt belongs to another logical job"
                    )
                try:
                    self._insert_idempotent_immutable(
                        cursor,
                        table="provider_attempt_receipts",
                        id_column="provider_attempt_receipt_id",
                        record_id=record.provider_attempt_receipt_id,
                        columns=(
                            "provider_attempt_id",
                            "logical_model_job_id",
                            "disposition",
                            "payload",
                        ),
                        values=(
                            record.provider_attempt_id,
                            record.logical_model_job_id,
                            record.disposition.value,
                            Jsonb(payload),
                        ),
                        payload=payload,
                        label="provider attempt receipt",
                    )
                except errors.UniqueViolation as error:
                    raise AuthorityConflict(
                        "provider attempt already has another receipt"
                    ) from error
                return record

    def get_provider_attempt_request(
        self,
        provider_attempt_id: str,
    ) -> ProviderAttemptRequest:
        return self._get_authority(
            table="provider_attempt_requests",
            id_column="provider_attempt_id",
            record_id=provider_attempt_id,
            label="provider attempt request",
            decoder=decode_provider_attempt_request,
        )

    def get_provider_attempt_receipt(
        self,
        provider_attempt_receipt_id: str,
    ) -> ProviderAttemptReceipt:
        return self._get_authority(
            table="provider_attempt_receipts",
            id_column="provider_attempt_receipt_id",
            record_id=provider_attempt_receipt_id,
            label="provider attempt receipt",
            decoder=decode_provider_attempt_receipt,
        )

    def list_provider_attempt_receipts(
        self,
        logical_model_job_id: str,
    ) -> tuple[ProviderAttemptReceipt, ...]:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                self._get_payload(
                    cursor,
                    table="logical_model_jobs",
                    id_column="logical_model_job_id",
                    record_id=logical_model_job_id,
                    label="logical model job",
                    decoder=decode_logical_model_job,
                )
                cursor.execute(
                    """
                    SELECT r.payload
                    FROM waje_vnext.provider_attempt_receipts AS r
                    JOIN waje_vnext.provider_attempt_requests AS q
                      ON q.provider_attempt_id = r.provider_attempt_id
                    WHERE r.logical_model_job_id = %s
                    ORDER BY q.attempt_number
                    """,
                    (logical_model_job_id,),
                )
                return tuple(
                    decode_provider_attempt_receipt(row["payload"])
                    for row in cursor.fetchall()
                )

    def record_durable_model_result(
        self,
        record: DurableModelResult,
    ) -> DurableModelResult:
        payload = encode_record(record)
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                request = self._get_payload(
                    cursor,
                    table="provider_attempt_requests",
                    id_column="provider_attempt_id",
                    record_id=record.provider_attempt_id,
                    label="provider attempt request",
                    decoder=decode_provider_attempt_request,
                )
                cursor.execute(
                    """
                    SELECT payload
                    FROM waje_vnext.provider_attempt_receipts
                    WHERE provider_attempt_id = %s
                    """,
                    (record.provider_attempt_id,),
                )
                receipt_row = cursor.fetchone()
                if receipt_row is None:
                    raise InvalidAuthorityTransition(
                        "durable model result lacks a provider receipt"
                    )
                receipt = decode_provider_attempt_receipt(
                    receipt_row["payload"]
                )
                if (
                    request.logical_model_job_id
                    != record.logical_model_job_id
                    or receipt.logical_model_job_id
                    != record.logical_model_job_id
                    or receipt.disposition
                    is not ProviderAttemptDisposition.SUCCEEDED
                    or receipt.output_sha256 != record.output_sha256
                ):
                    raise InvalidAuthorityTransition(
                        "durable model result lacks its successful attempt"
                    )
                try:
                    self._insert_idempotent_immutable(
                        cursor,
                        table="durable_model_results",
                        id_column="durable_model_result_id",
                        record_id=record.durable_model_result_id,
                        columns=(
                            "logical_model_job_id",
                            "provider_attempt_id",
                            "output_sha256",
                            "payload",
                            "recorded_at",
                        ),
                        values=(
                            record.logical_model_job_id,
                            record.provider_attempt_id,
                            record.output_sha256,
                            Jsonb(payload),
                            record.recorded_at,
                        ),
                        payload=payload,
                        label="durable model result",
                    )
                except errors.UniqueViolation as error:
                    raise AuthorityConflict(
                        "logical model job already has a different result"
                    ) from error
                return record

    def get_durable_model_result(
        self,
        logical_model_job_id: str,
    ) -> DurableModelResult | None:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                self._get_payload(
                    cursor,
                    table="logical_model_jobs",
                    id_column="logical_model_job_id",
                    record_id=logical_model_job_id,
                    label="logical model job",
                    decoder=decode_logical_model_job,
                )
                cursor.execute(
                    """
                    SELECT payload
                    FROM waje_vnext.durable_model_results
                    WHERE logical_model_job_id = %s
                    """,
                    (logical_model_job_id,),
                )
                row = cursor.fetchone()
                return (
                    None
                    if row is None
                    else decode_durable_model_result(row["payload"])
                )

    def record_obligation_schedule(
        self,
        record: ObligationScheduleRecord,
    ) -> ObligationScheduleRecord:
        payload = encode_record(record)
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                if (
                    self._authority_snapshot_from_cursor(
                        cursor,
                        record.case_id,
                    )
                    != record.authority_snapshot
                ):
                    raise InvalidAuthorityTransition(
                        "obligation schedule authority is stale"
                    )
                self._insert_idempotent_immutable(
                    cursor,
                    table="obligation_schedules",
                    id_column="schedule_id",
                    record_id=record.schedule_id,
                    columns=(
                        "case_id",
                        "frame_revision_id",
                        "authority_snapshot_sha256",
                        "payload",
                        "created_at",
                    ),
                    values=(
                        record.case_id,
                        record.frame_revision_id,
                        record.authority_snapshot_sha256,
                        Jsonb(payload),
                        record.created_at,
                    ),
                    payload=payload,
                    label="obligation schedule",
                )
                return record

    def get_obligation_schedule(
        self,
        schedule_id: str,
    ) -> ObligationScheduleRecord:
        return self._get_authority(
            table="obligation_schedules",
            id_column="schedule_id",
            record_id=schedule_id,
            label="obligation schedule",
            decoder=decode_obligation_schedule,
        )

    def record_obligation_dispatch(
        self,
        record: ObligationDispatchRecord,
    ) -> ObligationDispatchRecord:
        payload = encode_record(record)
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                schedule = self._get_payload(
                    cursor,
                    table="obligation_schedules",
                    id_column="schedule_id",
                    record_id=record.schedule_id,
                    label="obligation schedule",
                    decoder=decode_obligation_schedule,
                )
                message = self._get_payload(
                    cursor,
                    table="outbox_messages",
                    id_column="outbox_message_id",
                    record_id=record.outbox_message_id,
                    label="outbox message",
                    decoder=decode_outbox_message,
                )
                if (
                    record.dispatch.obligation_id
                    not in {
                        item.obligation_id
                        for item in schedule.obligations
                    }
                    or record.dispatch.authority_snapshot
                    != schedule.authority_snapshot
                    or message.job_kind is not AsyncJobKind.OBLIGATION
                    or str(message.payload.get("schedule_id", ""))
                    != record.schedule_id
                    or str(message.payload.get("obligation_id", ""))
                    != record.dispatch.obligation_id
                ):
                    raise InvalidAuthorityTransition(
                        "obligation dispatch does not bind schedule outbox"
                    )
                self._insert_idempotent_immutable(
                    cursor,
                    table="obligation_dispatch_records",
                    id_column="dispatch_record_id",
                    record_id=record.dispatch_record_id,
                    columns=(
                        "schedule_id",
                        "obligation_id",
                        "outbox_message_id",
                        "payload",
                        "created_at",
                    ),
                    values=(
                        record.schedule_id,
                        record.dispatch.obligation_id,
                        record.outbox_message_id,
                        Jsonb(payload),
                        record.created_at,
                    ),
                    payload=payload,
                    label="obligation dispatch",
                )
                return record

    def list_obligation_dispatches(
        self,
        schedule_id: str,
    ) -> tuple[ObligationDispatchRecord, ...]:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                self._get_payload(
                    cursor,
                    table="obligation_schedules",
                    id_column="schedule_id",
                    record_id=schedule_id,
                    label="obligation schedule",
                    decoder=decode_obligation_schedule,
                )
                cursor.execute(
                    """
                    SELECT payload
                    FROM waje_vnext.obligation_dispatch_records
                    WHERE schedule_id = %s
                    ORDER BY obligation_id
                    """,
                    (schedule_id,),
                )
                return tuple(
                    decode_obligation_dispatch(row["payload"])
                    for row in cursor.fetchall()
                )

    def record_obligation_completion(
        self,
        record: ObligationCompletionRecord,
    ) -> ObligationCompletionRecord:
        payload = encode_record(record)
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                schedule = self._get_payload(
                    cursor,
                    table="obligation_schedules",
                    id_column="schedule_id",
                    record_id=record.schedule_id,
                    label="obligation schedule",
                    decoder=decode_obligation_schedule,
                )
                current = self._authority_snapshot_from_cursor(
                    cursor,
                    schedule.case_id,
                )
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
                        "obligation cannot be superseded without "
                        "authority drift"
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
                cursor.execute(
                    """
                    SELECT payload
                    FROM waje_vnext.obligation_dispatch_records
                    WHERE schedule_id = %s AND obligation_id = %s
                    """,
                    (
                        record.schedule_id,
                        record.completion.obligation_id,
                    ),
                )
                dispatch_row = cursor.fetchone()
                dispatch = (
                    None
                    if dispatch_row is None
                    else decode_obligation_dispatch(
                        dispatch_row["payload"]
                    )
                )
                cursor.execute(
                    """
                    SELECT payload
                    FROM waje_vnext.obligation_completion_records
                    WHERE schedule_id = %s
                    ORDER BY obligation_id
                    """,
                    (record.schedule_id,),
                )
                prior_completions = tuple(
                    decode_obligation_completion_record(
                        row["payload"]
                    ).completion
                    for row in cursor.fetchall()
                )
                try:
                    validate_persisted_obligation_completion(
                        schedule=schedule,
                        completion=record.completion,
                        prior_completions=prior_completions,
                        dispatch=(
                            None
                            if dispatch is None
                            else dispatch.dispatch
                        ),
                        current_authority=current,
                    )
                except ValueError as error:
                    raise InvalidAuthorityTransition(str(error)) from error
                self._insert_idempotent_immutable(
                    cursor,
                    table="obligation_completion_records",
                    id_column="completion_record_id",
                    record_id=record.completion_record_id,
                    columns=(
                        "schedule_id",
                        "obligation_id",
                        "payload",
                        "created_at",
                    ),
                    values=(
                        record.schedule_id,
                        record.completion.obligation_id,
                        Jsonb(payload),
                        record.created_at,
                    ),
                    payload=payload,
                    label="obligation completion",
                )
                return record

    def list_obligation_completions(
        self,
        schedule_id: str,
    ) -> tuple[ObligationCompletionRecord, ...]:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                self._get_payload(
                    cursor,
                    table="obligation_schedules",
                    id_column="schedule_id",
                    record_id=schedule_id,
                    label="obligation schedule",
                    decoder=decode_obligation_schedule,
                )
                cursor.execute(
                    """
                    SELECT payload
                    FROM waje_vnext.obligation_completion_records
                    WHERE schedule_id = %s
                    ORDER BY obligation_id
                    """,
                    (schedule_id,),
                )
                return tuple(
                    decode_obligation_completion_record(row["payload"])
                    for row in cursor.fetchall()
                )

    def record_obligation_schedule_checkpoint(
        self,
        record: ObligationScheduleCheckpoint,
    ) -> ObligationScheduleCheckpoint:
        payload = encode_record(record)
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                schedule = self._get_payload(
                    cursor,
                    table="obligation_schedules",
                    id_column="schedule_id",
                    record_id=record.schedule_id,
                    label="obligation schedule",
                    decoder=decode_obligation_schedule,
                )
                cursor.execute(
                    """
                    SELECT checkpoint_id, checkpoint_number
                    FROM waje_vnext.obligation_schedule_checkpoints
                    WHERE schedule_id = %s
                    ORDER BY checkpoint_number DESC
                    LIMIT 1
                    FOR UPDATE
                    """,
                    (record.schedule_id,),
                )
                prior_row = cursor.fetchone()
                expected_number = (
                    1
                    if prior_row is None
                    else prior_row["checkpoint_number"] + 1
                )
                expected_prior = (
                    None
                    if prior_row is None
                    else prior_row["checkpoint_id"]
                )
                cursor.execute(
                    """
                    SELECT obligation_id
                    FROM waje_vnext.obligation_dispatch_records
                    WHERE schedule_id = %s
                    """,
                    (record.schedule_id,),
                )
                dispatched = {
                    row["obligation_id"] for row in cursor.fetchall()
                }
                cursor.execute(
                    """
                    SELECT obligation_id
                    FROM waje_vnext.obligation_completion_records
                    WHERE schedule_id = %s
                    """,
                    (record.schedule_id,),
                )
                completed = {
                    row["obligation_id"] for row in cursor.fetchall()
                }
                expected_dispatched = tuple(
                    sorted(dispatched - completed)
                )
                expected_completed = tuple(sorted(completed))
                expected_pending = tuple(
                    sorted(
                        {
                            item.obligation_id
                            for item in schedule.obligations
                        }
                        - dispatched
                        - completed
                    )
                )
                if (
                    record.checkpoint_number != expected_number
                    or record.prior_checkpoint_id != expected_prior
                    or record.schedule_sha256
                    != schedule.content_sha256
                    or record.authority_snapshot_sha256
                    != schedule.authority_snapshot_sha256
                    or record.dispatched_obligation_ids
                    != expected_dispatched
                    or record.completed_obligation_ids
                    != expected_completed
                    or record.pending_obligation_ids
                    != expected_pending
                ):
                    raise InvalidAuthorityTransition(
                        "obligation checkpoint is not a state derivation"
                    )
                self._insert_idempotent_immutable(
                    cursor,
                    table="obligation_schedule_checkpoints",
                    id_column="checkpoint_id",
                    record_id=record.checkpoint_id,
                    columns=(
                        "schedule_id",
                        "checkpoint_number",
                        "prior_checkpoint_id",
                        "payload",
                        "created_at",
                    ),
                    values=(
                        record.schedule_id,
                        record.checkpoint_number,
                        record.prior_checkpoint_id,
                        Jsonb(payload),
                        record.created_at,
                    ),
                    payload=payload,
                    label="obligation schedule checkpoint",
                )
                return record

    def list_obligation_schedule_checkpoints(
        self,
        schedule_id: str,
    ) -> tuple[ObligationScheduleCheckpoint, ...]:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                self._get_payload(
                    cursor,
                    table="obligation_schedules",
                    id_column="schedule_id",
                    record_id=schedule_id,
                    label="obligation schedule",
                    decoder=decode_obligation_schedule,
                )
                cursor.execute(
                    """
                    SELECT payload
                    FROM waje_vnext.obligation_schedule_checkpoints
                    WHERE schedule_id = %s
                    ORDER BY checkpoint_number
                    """,
                    (schedule_id,),
                )
                return tuple(
                    decode_obligation_schedule_checkpoint(row["payload"])
                    for row in cursor.fetchall()
                )

    def record_run_trace_manifest(
        self,
        record: RunTraceManifest,
    ) -> RunTraceManifest:
        validate_run_trace_manifest_references(self, record)
        payload = encode_record(record)
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                self._get_case(cursor, record.case_id)
                self._insert_idempotent_immutable(
                    cursor,
                    table="run_trace_manifests",
                    id_column="trace_manifest_id",
                    record_id=record.trace_manifest_id,
                    columns=(
                        "case_id",
                        "run_id",
                        "lineage_sha256",
                        "payload",
                    ),
                    values=(
                        record.case_id,
                        record.run_id,
                        record.lineage_sha256,
                        Jsonb(payload),
                    ),
                    payload=payload,
                    label="run trace manifest",
                )
                return record

    def get_run_trace_manifest(
        self,
        trace_manifest_id: str,
    ) -> RunTraceManifest:
        return self._get_authority(
            table="run_trace_manifests",
            id_column="trace_manifest_id",
            record_id=trace_manifest_id,
            label="run trace manifest",
            decoder=decode_run_trace_manifest,
        )

    def get_frame(self, frame_revision_id: str) -> AnalysisFrameRevision:
        return self._get_authority(
            table="analysis_frame_revisions",
            id_column="frame_revision_id",
            record_id=frame_revision_id,
            label="frame",
            decoder=decode_frame,
        )

    def get_question(
        self,
        question_revision_id: str,
    ) -> QuestionRevision:
        return self._get_authority(
            table="question_revisions",
            id_column="question_revision_id",
            record_id=question_revision_id,
            label="question",
            decoder=decode_question,
        )

    def get_measurement_resolution(
        self,
        resolution_outcome_id: str,
    ) -> MeasurementResolutionOutcome:
        return self._get_authority(
            table="measurement_resolution_outcomes",
            id_column="resolution_outcome_id",
            record_id=resolution_outcome_id,
            label="measurement resolution",
            decoder=decode_measurement_resolution,
        )

    def get_measurement_resolution_admission(
        self,
        resolution_outcome_id: str,
    ) -> MeasurementResolutionAdmission:
        return self._get_authority(
            table="measurement_resolution_admissions",
            id_column="resolution_outcome_id",
            record_id=resolution_outcome_id,
            label="measurement resolution admission",
            decoder=decode_measurement_resolution_admission,
        )

    def get_evidence_obligation(
        self,
        obligation_id: str,
    ) -> ResolvedEvidenceObligation:
        return self._get_authority(
            table="resolved_evidence_obligations",
            id_column="obligation_id",
            record_id=obligation_id,
            label="evidence obligation",
            decoder=decode_evidence_obligation,
        )

    def get_plan(self, plan_revision_id: str) -> WorkPlanRevision:
        return self._get_authority(
            table="work_plan_revisions",
            id_column="plan_revision_id",
            record_id=plan_revision_id,
            label="plan",
            decoder=decode_plan,
        )

    def get_evidence(self, evidence_record_id: str) -> EvidenceRecord:
        return self._get_authority(
            table="evidence_records",
            id_column="evidence_record_id",
            record_id=evidence_record_id,
            label="evidence",
            decoder=decode_evidence,
        )

    def get_answer(self, answer_version_id: str) -> AnswerVersion:
        return self._get_authority(
            table="answer_versions",
            id_column="answer_version_id",
            record_id=answer_version_id,
            label="answer",
            decoder=decode_answer,
        )

    def list_evidence(self, case_id: str) -> tuple[EvidenceRecord, ...]:
        return self._list_payloads(
            table="evidence_records",
            case_id=case_id,
            order_by=("created_at", "evidence_record_id"),
            decoder=decode_evidence,
        )

    def list_decisions(self, case_id: str) -> tuple[DecisionRecord, ...]:
        return self._list_payloads(
            table="decision_records",
            case_id=case_id,
            order_by=("created_at", "decision_record_id"),
            decoder=decode_decision,
        )

    def list_reviewer_objections(
        self,
        case_id: str,
    ) -> tuple[ReviewerObjection, ...]:
        return self._list_payloads(
            table="reviewer_objections",
            case_id=case_id,
            order_by=("objection_key", "revision_number"),
            decoder=decode_objection,
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
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                idempotent = self._idempotent_head_event(
                    cursor,
                    event_id=event_id,
                    event_type=JournalEventType.QUESTION_ACCEPTED,
                    authority_ref=question.question_revision_id,
                    case_id=question.case_id,
                )
                if idempotent is not None:
                    return idempotent
                case = self._lock_case(
                    cursor,
                    question.case_id,
                    expected_head_version,
                )
                current = (
                    None
                    if case.accepted_question_revision_id is None
                    else self._get_question(
                        cursor,
                        case.accepted_question_revision_id,
                    )
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
                if question.acceptance_event_id != event_id:
                    raise InvalidAuthorityTransition(
                        "question must bind its acceptance event"
                    )
                if question.accepted_head_version != case.head_version + 1:
                    raise InvalidAuthorityTransition(
                        "question accepted_head_version is stale"
                    )
                for source in question.source_messages:
                    cursor.execute(
                        """
                        SELECT case_id, sequence, payload
                        FROM waje_vnext.case_mailbox_messages
                        WHERE message_id = %s
                        """,
                        (source.message_id,),
                    )
                    row = cursor.fetchone()
                    if row is None or row["case_id"] != question.case_id:
                        raise InvalidAuthorityTransition(
                            "question source message is unavailable"
                        )
                    source_content = str(
                        row["payload"].get("message", "")
                    )
                    if (
                        row["sequence"] != source.sequence
                        or content_sha256(source_content)
                        != source.content_sha256
                    ):
                        raise InvalidAuthorityTransition(
                            "question source message does not match mailbox"
                        )
                payload = encode_record(question)
                self._insert_immutable(
                    cursor,
                    table="question_revisions",
                    id_column="question_revision_id",
                    record_id=question.question_revision_id,
                    columns=(
                        "case_id",
                        "revision_number",
                        "prior_question_revision_id",
                        "analysis_cycle_id",
                        "accepted_head_version",
                        "content_sha256",
                        "payload",
                        "created_at",
                    ),
                    values=(
                        question.case_id,
                        question.revision_number,
                        question.prior_question_revision_id,
                        question.analysis_cycle_id,
                        question.accepted_head_version,
                        question.content_sha256,
                        Jsonb(payload),
                        question.created_at,
                    ),
                    payload=payload,
                    label="question",
                )
                cursor.execute(
                    """
                    UPDATE waje_vnext.investigation_cases
                    SET head_version = head_version + 1,
                        accepted_question_revision_id = %s,
                        accepted_frame_revision_id = NULL,
                        accepted_plan_revision_id = NULL,
                        accepted_answer_version_id = NULL,
                        analysis_cycle_id = %s,
                        updated_at = %s
                    WHERE case_id = %s AND head_version = %s
                    RETURNING *
                    """,
                    (
                        question.question_revision_id,
                        question.analysis_cycle_id,
                        recorded_at,
                        question.case_id,
                        case.head_version,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise StaleHead(
                        "case head changed during question acceptance"
                    )
                updated = _case_from_row(row)
                self._append_authority_event(
                    cursor,
                    case_id=question.case_id,
                    event_id=event_id,
                    event_type=JournalEventType.QUESTION_ACCEPTED,
                    recorded_at=recorded_at,
                    action_id=None,
                    authority_ref=question.question_revision_id,
                    payload={
                        "revision_number": question.revision_number,
                        "content_sha256": question.content_sha256,
                        "analysis_cycle_id": question.analysis_cycle_id,
                        "head_version": updated.head_version,
                    },
                    operation=operation,
                )
                return updated

    def record_frame_candidate(
        self,
        candidate: FrameCandidateRecord,
    ) -> FrameCandidateRecord:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                case = self._get_case(
                    cursor,
                    candidate.case_id,
                    for_update=True,
                )
                if (
                    candidate.question_revision_id
                    != case.accepted_question_revision_id
                    or candidate.proposed_frame.question_revision_id
                    != case.accepted_question_revision_id
                ):
                    raise InvalidAuthorityTransition(
                        "frame candidate must bind the accepted question"
                    )
                cursor.execute(
                    """
                    SELECT c.payload
                    FROM waje_vnext.active_frame_candidate_heads h
                    JOIN waje_vnext.frame_candidate_records c
                      ON c.frame_candidate_id = h.frame_candidate_id
                    WHERE h.case_id = %s
                    FOR UPDATE OF h
                    """,
                    (candidate.case_id,),
                )
                prior_row = cursor.fetchone()
                prior = (
                    None
                    if prior_row is None
                    else decode_frame_candidate(prior_row["payload"])
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
                    cursor.execute(
                        """
                        SELECT payload
                        FROM waje_vnext.frame_review_records
                        WHERE frame_candidate_id = %s
                        """,
                        (prior.frame_candidate_id,),
                    )
                    prior_review_row = cursor.fetchone()
                    if prior_review_row is None:
                        raise InvalidAuthorityTransition(
                            "replacement candidate requires prior review"
                        )
                    prior_review = decode_frame_review(
                        prior_review_row["payload"]
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
                payload = encode_record(candidate)
                self._insert_idempotent_immutable(
                    cursor,
                    table="frame_candidate_records",
                    id_column="frame_candidate_id",
                    record_id=candidate.frame_candidate_id,
                    columns=(
                        "case_id",
                        "candidate_generation",
                        "prior_frame_candidate_id",
                        "proposed_frame_revision_id",
                        "proposed_frame_content_sha256",
                        "payload",
                    ),
                    values=(
                        candidate.case_id,
                        candidate.candidate_generation,
                        candidate.prior_frame_candidate_id,
                        candidate.proposed_frame_revision_id,
                        candidate.proposed_frame_content_sha256,
                        Jsonb(payload),
                    ),
                    payload=payload,
                    label="frame candidate",
                )
                cursor.execute(
                    """
                    INSERT INTO waje_vnext.active_frame_candidate_heads (
                        case_id,
                        frame_candidate_id,
                        candidate_generation,
                        proposed_frame_content_sha256
                    )
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (case_id) DO UPDATE
                    SET frame_candidate_id = EXCLUDED.frame_candidate_id,
                        candidate_generation = EXCLUDED.candidate_generation,
                        proposed_frame_content_sha256 =
                            EXCLUDED.proposed_frame_content_sha256
                    WHERE
                        waje_vnext.active_frame_candidate_heads
                            .candidate_generation
                        = EXCLUDED.candidate_generation - 1
                    RETURNING frame_candidate_id
                    """,
                    (
                        candidate.case_id,
                        candidate.frame_candidate_id,
                        candidate.candidate_generation,
                        candidate.proposed_frame_content_sha256,
                    ),
                )
                moved = cursor.fetchone()
                if moved is None:
                    cursor.execute(
                        """
                        SELECT frame_candidate_id
                        FROM waje_vnext.active_frame_candidate_heads
                        WHERE case_id = %s
                        """,
                        (candidate.case_id,),
                    )
                    current = cursor.fetchone()
                    if (
                        current is None
                        or current["frame_candidate_id"]
                        != candidate.frame_candidate_id
                    ):
                        raise StaleHead(
                            "active frame candidate changed concurrently"
                        )
                return candidate

    def get_frame_candidate(
        self,
        frame_candidate_id: str,
    ) -> FrameCandidateRecord:
        return self._get_authority(
            table="frame_candidate_records",
            id_column="frame_candidate_id",
            record_id=frame_candidate_id,
            label="frame candidate",
            decoder=decode_frame_candidate,
        )

    def get_active_frame_candidate(
        self,
        case_id: str,
    ) -> FrameCandidateRecord | None:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                self._get_case(cursor, case_id)
                cursor.execute(
                    """
                    SELECT c.payload
                    FROM waje_vnext.active_frame_candidate_heads h
                    JOIN waje_vnext.frame_candidate_records c
                      ON c.frame_candidate_id = h.frame_candidate_id
                    WHERE h.case_id = %s
                    """,
                    (case_id,),
                )
                row = cursor.fetchone()
                return (
                    None
                    if row is None
                    else decode_frame_candidate(row["payload"])
                )

    def list_frame_candidates(
        self,
        case_id: str,
    ) -> tuple[FrameCandidateRecord, ...]:
        return self._list_payloads(
            table="frame_candidate_records",
            case_id=case_id,
            order_by=("candidate_generation",),
            decoder=decode_frame_candidate,
        )

    def supersede_active_frame_candidate(
        self,
        record: FrameCandidateSupersessionRecord,
    ) -> FrameCandidateSupersessionRecord:
        payload = encode_record(record)
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                cursor.execute(
                    """
                    SELECT frame_candidate_id
                    FROM waje_vnext.active_frame_candidate_heads
                    WHERE case_id = %s
                    FOR UPDATE
                    """,
                    (record.case_id,),
                )
                active = cursor.fetchone()
                if (
                    active is None
                    or active["frame_candidate_id"]
                    != record.frame_candidate_id
                ):
                    cursor.execute(
                        """
                        SELECT payload
                        FROM waje_vnext.frame_candidate_supersession_records
                        WHERE supersession_record_id = %s
                        """,
                        (record.supersession_record_id,),
                    )
                    existing_row = cursor.fetchone()
                    existing = (
                        None
                        if existing_row is None
                        else decode_frame_candidate_supersession(
                            existing_row["payload"]
                        )
                    )
                    if existing == record:
                        return existing
                    raise InvalidAuthorityTransition(
                        "frame candidate supersession does not target active head"
                    )
                candidate = self._get_payload(
                    cursor,
                    table="frame_candidate_records",
                    id_column="frame_candidate_id",
                    record_id=record.frame_candidate_id,
                    label="frame candidate",
                    decoder=decode_frame_candidate,
                )
                question = self._get_question(
                    cursor,
                    record.superseded_by_question_revision_id,
                )
                cursor.execute(
                    """
                    SELECT authority_epoch
                    FROM waje_vnext.case_mailbox_heads
                    WHERE case_id = %s
                    """,
                    (record.case_id,),
                )
                mailbox = cursor.fetchone()
                if (
                    question.case_id != record.case_id
                    or candidate.question_revision_id
                    == question.question_revision_id
                    or record.authority_epoch
                    != mailbox["authority_epoch"]
                ):
                    raise InvalidAuthorityTransition(
                        "frame candidate supersession authority is invalid"
                    )
                self._insert_idempotent_immutable(
                    cursor,
                    table="frame_candidate_supersession_records",
                    id_column="supersession_record_id",
                    record_id=record.supersession_record_id,
                    columns=(
                        "case_id",
                        "frame_candidate_id",
                        "superseded_by_question_revision_id",
                        "authority_epoch",
                        "payload",
                        "created_at",
                    ),
                    values=(
                        record.case_id,
                        record.frame_candidate_id,
                        record.superseded_by_question_revision_id,
                        record.authority_epoch,
                        Jsonb(payload),
                        record.created_at,
                    ),
                    payload=payload,
                    label="frame candidate supersession",
                )
                cursor.execute(
                    """
                    DELETE FROM waje_vnext.active_frame_candidate_heads
                    WHERE case_id = %s AND frame_candidate_id = %s
                    """,
                    (record.case_id, record.frame_candidate_id),
                )
                if cursor.rowcount != 1:
                    raise StaleHead(
                        "active frame candidate changed concurrently"
                    )
                return record

    def list_frame_candidate_supersessions(
        self,
        case_id: str,
    ) -> tuple[FrameCandidateSupersessionRecord, ...]:
        return self._list_payloads(
            table="frame_candidate_supersession_records",
            case_id=case_id,
            order_by=("created_at", "supersession_record_id"),
            decoder=decode_frame_candidate_supersession,
        )

    def record_objection_closure(
        self,
        closure: ObjectionClosureRecord,
    ) -> ObjectionClosureRecord:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                source = self._get_payload(
                    cursor,
                    table="frame_candidate_records",
                    id_column="frame_candidate_id",
                    record_id=closure.source_frame_candidate_id,
                    label="source frame candidate",
                    decoder=decode_frame_candidate,
                )
                replacement = self._get_payload(
                    cursor,
                    table="frame_candidate_records",
                    id_column="frame_candidate_id",
                    record_id=closure.replacement_frame_candidate_id,
                    label="replacement frame candidate",
                    decoder=decode_frame_candidate,
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
                if (
                    closure.objection_id
                    not in replacement.addressed_objection_ids
                ):
                    raise InvalidAuthorityTransition(
                        "replacement candidate does not address objection"
                    )
                review = self._get_payload(
                    cursor,
                    table="frame_review_records",
                    id_column="frame_review_id",
                    record_id=closure.source_frame_review_id,
                    label="source frame review",
                    decoder=decode_frame_review,
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
                all_changed_node_ids = (
                    derive_changed_measurement_node_ids(
                        source.proposed_frame.measurement_design,
                        replacement.proposed_frame.measurement_design,
                    )
                )
                expected_changed_node_ids = tuple(
                    node_id
                    for node_id in all_changed_node_ids
                    if any(
                        measurement_paths_overlap(
                            node_id,
                            affected_node_id,
                        )
                        for affected_node_id
                        in objection.affected_node_ids
                    )
                )
                if (
                    closure.changed_node_ids
                    != expected_changed_node_ids
                ):
                    raise InvalidAuthorityTransition(
                        "objection closure change proof does not match Frames"
                    )
                payload = encode_record(closure)
                self._insert_idempotent_immutable(
                    cursor,
                    table="objection_closure_records",
                    id_column="objection_closure_id",
                    record_id=closure.objection_closure_id,
                    columns=(
                        "objection_id",
                        "source_frame_candidate_id",
                        "replacement_frame_candidate_id",
                        "payload",
                    ),
                    values=(
                        closure.objection_id,
                        closure.source_frame_candidate_id,
                        closure.replacement_frame_candidate_id,
                        Jsonb(payload),
                    ),
                    payload=payload,
                    label="objection closure",
                )
                return closure

    def get_objection_closure(
        self,
        objection_closure_id: str,
    ) -> ObjectionClosureRecord:
        return self._get_authority(
            table="objection_closure_records",
            id_column="objection_closure_id",
            record_id=objection_closure_id,
            label="objection closure",
            decoder=decode_objection_closure,
        )

    def record_frame_review(
        self,
        review: FrameReviewRecord,
    ) -> FrameReviewRecord:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                candidate = self._get_payload(
                    cursor,
                    table="frame_candidate_records",
                    id_column="frame_candidate_id",
                    record_id=review.frame_candidate_id,
                    label="frame candidate",
                    decoder=decode_frame_candidate,
                )
                cursor.execute(
                    """
                    SELECT frame_candidate_id
                    FROM waje_vnext.active_frame_candidate_heads
                    WHERE case_id = %s
                    FOR UPDATE
                    """,
                    (candidate.case_id,),
                )
                active = cursor.fetchone()
                if (
                    active is None
                    or active["frame_candidate_id"]
                    != candidate.frame_candidate_id
                ):
                    raise InvalidAuthorityTransition(
                        "review targets a superseded frame candidate"
                    )
                snapshot = self._authority_snapshot_from_cursor(
                    cursor,
                    candidate.case_id,
                )
                if (
                    review.authority_epoch
                    != snapshot.mailbox_authority_epoch
                    or review.reviewed_frame_content_sha256
                    != candidate.proposed_frame_content_sha256
                ):
                    raise InvalidAuthorityTransition(
                        "frame review authority or content is stale"
                    )
                cursor.execute(
                    """
                    SELECT objection_closure_id
                    FROM waje_vnext.objection_closure_records
                    WHERE replacement_frame_candidate_id = %s
                    """,
                    (candidate.frame_candidate_id,),
                )
                expected_closure_ids = {
                    row["objection_closure_id"]
                    for row in cursor.fetchall()
                }
                if set(review.closure_proof_refs) != expected_closure_ids:
                    raise InvalidAuthorityTransition(
                        "frame review has incomplete objection closure references"
                    )
                payload = encode_record(review)
                self._insert_idempotent_immutable(
                    cursor,
                    table="frame_review_records",
                    id_column="frame_review_id",
                    record_id=review.frame_review_id,
                    columns=(
                        "frame_candidate_id",
                        "reviewer_job_id",
                        "disposition",
                        "reviewed_frame_content_sha256",
                        "payload",
                    ),
                    values=(
                        review.frame_candidate_id,
                        review.reviewer_job_id,
                        review.disposition.value,
                        review.reviewed_frame_content_sha256,
                        Jsonb(payload),
                    ),
                    payload=payload,
                    label="frame review",
                )
                return review

    def get_frame_review(
        self,
        frame_review_id: str,
    ) -> FrameReviewRecord:
        return self._get_authority(
            table="frame_review_records",
            id_column="frame_review_id",
            record_id=frame_review_id,
            label="frame review",
            decoder=decode_frame_review,
        )

    def get_frame_review_for_candidate(
        self,
        frame_candidate_id: str,
    ) -> FrameReviewRecord | None:
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT payload
                FROM waje_vnext.frame_review_records
                WHERE frame_candidate_id = %s
                """,
                (frame_candidate_id,),
            )
            rows = cursor.fetchall()
        if len(rows) > 1:
            raise AuthorityConflict(
                "frame candidate has multiple immutable reviews"
            )
        if not rows:
            return None
        return decode_frame_review(rows[0]["payload"])

    def list_frame_reviews(
        self,
        case_id: str,
    ) -> tuple[FrameReviewRecord, ...]:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                self._get_case(cursor, case_id)
                cursor.execute(
                    """
                    SELECT r.payload
                    FROM waje_vnext.frame_review_records r
                    JOIN waje_vnext.frame_candidate_records c
                      ON c.frame_candidate_id = r.frame_candidate_id
                    WHERE c.case_id = %s
                    ORDER BY c.candidate_generation, r.frame_review_id
                    """,
                    (case_id,),
                )
                return tuple(
                    decode_frame_review(row["payload"])
                    for row in cursor.fetchall()
                )

    def record_frame_admission_proof(
        self,
        proof: FrameAdmissionProof,
    ) -> FrameAdmissionProof:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                self._get_case(cursor, proof.case_id, for_update=True)
                candidate = self._get_payload(
                    cursor,
                    table="frame_candidate_records",
                    id_column="frame_candidate_id",
                    record_id=proof.frame_candidate_id,
                    label="frame candidate",
                    decoder=decode_frame_candidate,
                )
                review = self._get_payload(
                    cursor,
                    table="frame_review_records",
                    id_column="frame_review_id",
                    record_id=proof.frame_review_id,
                    label="frame review",
                    decoder=decode_frame_review,
                )
                if (
                    candidate.case_id != proof.case_id
                    or review.frame_candidate_id
                    != candidate.frame_candidate_id
                    or review.disposition
                    is not FrameReviewDisposition.ACCEPT
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
                closure_ids = proof.objection_closure_record_ids
                closures = tuple(
                    self._get_payload(
                        cursor,
                        table="objection_closure_records",
                        id_column="objection_closure_id",
                        record_id=closure_id,
                        label="objection closure",
                        decoder=decode_objection_closure,
                    )
                    for closure_id in closure_ids
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
                if (
                    proof.authority_snapshot
                    != self._authority_snapshot_from_cursor(
                        cursor,
                        proof.case_id,
                    )
                ):
                    raise InvalidAuthorityTransition(
                        "frame admission proof authority snapshot is stale"
                    )
                payload = encode_record(proof)
                self._insert_idempotent_immutable(
                    cursor,
                    table="frame_admission_proofs",
                    id_column="frame_admission_proof_id",
                    record_id=proof.frame_admission_proof_id,
                    columns=(
                        "case_id",
                        "frame_candidate_id",
                        "candidate_generation",
                        "frame_revision_id",
                        "frame_content_sha256",
                        "frame_review_id",
                        "authority_snapshot_sha256",
                        "payload",
                    ),
                    values=(
                        proof.case_id,
                        proof.frame_candidate_id,
                        proof.candidate_generation,
                        proof.frame_revision_id,
                        proof.frame_content_sha256,
                        proof.frame_review_id,
                        proof.authority_snapshot_sha256,
                        Jsonb(payload),
                    ),
                    payload=payload,
                    label="frame admission proof",
                )
                return proof

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
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                idempotent = self._idempotent_head_event(
                    cursor,
                    event_id=event_id,
                    event_type=JournalEventType.FRAME_ACCEPTED,
                    authority_ref=frame.frame_revision_id,
                    case_id=frame.case_id,
                )
                if idempotent is not None:
                    return idempotent
                case = self._lock_case(cursor, frame.case_id, expected_head_version)
                proof = self._get_payload(
                    cursor,
                    table="frame_admission_proofs",
                    id_column="frame_admission_proof_id",
                    record_id=frame_admission_proof_id,
                    label="frame admission proof",
                    decoder=decode_frame_admission_proof,
                )
                if (
                    proof.case_id != frame.case_id
                    or proof.frame_revision_id != frame.frame_revision_id
                    or proof.frame_content_sha256 != frame.content_sha256
                ):
                    raise InvalidAuthorityTransition(
                        "frame admission proof does not bind this Frame"
                    )
                if (
                    proof.authority_snapshot
                    != self._authority_snapshot_from_cursor(
                        cursor,
                        frame.case_id,
                    )
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
                question = self._get_question(
                    cursor,
                    frame.question_revision_id,
                )
                try:
                    question.validate_spans(
                        frame.measurement_design.question_grounding
                        .source_spans
                    )
                    validate_frame_identities(question, frame)
                    findings = validate_executable_design(
                        frame.measurement_design
                    )
                    if findings:
                        raise ValueError(
                            "measurement design is not executable: {}".format(
                                ",".join(
                                    sorted(
                                        {item.code for item in findings}
                                    )
                                )
                            )
                        )
                except ValueError as error:
                    raise InvalidAuthorityTransition(
                        str(error)
                    ) from error
                current = (
                    None
                    if case.accepted_frame_revision_id is None
                    else self._get_frame(cursor, case.accepted_frame_revision_id)
                )
                expected_revision = (
                    1 if current is None else current.revision_number + 1
                )
                expected_prior = (
                    None if current is None else current.frame_revision_id
                )
                if (
                    frame.revision_number != expected_revision
                    or frame.prior_frame_revision_id != expected_prior
                ):
                    raise InvalidAuthorityTransition(
                        "frame revision does not extend the accepted frame"
                    )
                for decision_id in (
                    frame.measurement_design.question_grounding
                    .decision_record_ids
                ):
                    decision = self._get_payload(
                        cursor,
                        table="decision_records",
                        id_column="decision_record_id",
                        record_id=decision_id,
                        label="decision",
                        decoder=decode_decision,
                    )
                    if decision.case_id != frame.case_id:
                        raise InvalidAuthorityTransition(
                            "frame decision belongs to another case"
                        )
                payload = encode_record(frame)
                self._insert_immutable(
                    cursor,
                    table="analysis_frame_revisions",
                    id_column="frame_revision_id",
                    record_id=frame.frame_revision_id,
                    columns=(
                        "case_id",
                        "question_revision_id",
                        "revision_number",
                        "prior_frame_revision_id",
                        "schema_epoch",
                        "identity_algorithm_version",
                        "semantic_measurement_ids",
                        "authority_binding_ids",
                        "content_sha256",
                        "payload",
                        "created_at",
                    ),
                    values=(
                        frame.case_id,
                        frame.question_revision_id,
                        frame.revision_number,
                        frame.prior_frame_revision_id,
                        frame.schema_epoch,
                        frame.identity_algorithm_version,
                        list(frame.semantic_measurement_ids),
                        list(frame.authority_binding_ids),
                        frame.content_sha256,
                        Jsonb(payload),
                        frame.created_at,
                    ),
                    payload=payload,
                    label="frame",
                )
                updated = self._move_heads(
                    cursor,
                    case=case,
                    recorded_at=recorded_at,
                    frame_id=frame.frame_revision_id,
                    plan_id=None,
                    answer_id=None,
                )
                self._append_authority_event(
                    cursor,
                    case_id=case.case_id,
                    event_id=event_id,
                    event_type=JournalEventType.FRAME_ACCEPTED,
                    recorded_at=recorded_at,
                    action_id=frame.created_by_action_id,
                    authority_ref=frame.frame_revision_id,
                    payload={
                        "revision_number": frame.revision_number,
                        "content_sha256": frame.content_sha256,
                        "head_version": updated.head_version,
                    },
                    operation=operation,
                )
                return updated

    def accept_plan(
        self,
        plan: WorkPlanRevision,
        *,
        expected_head_version: int,
        event_id: str,
        recorded_at: datetime,
        operation: OperationIdentity | None = None,
    ) -> InvestigationCase:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                idempotent = self._idempotent_head_event(
                    cursor,
                    event_id=event_id,
                    event_type=JournalEventType.PLAN_ACCEPTED,
                    authority_ref=plan.plan_revision_id,
                    case_id=plan.case_id,
                )
                if idempotent is not None:
                    return idempotent
                case = self._lock_case(cursor, plan.case_id, expected_head_version)
                if plan.frame_revision_id != case.accepted_frame_revision_id:
                    raise InvalidAuthorityTransition(
                        "plan must bind the currently accepted frame"
                    )
                current = self._latest_plan_for_case(cursor, case.case_id)
                expected_revision = (
                    1 if current is None else current.revision_number + 1
                )
                expected_prior = (
                    None if current is None else current.plan_revision_id
                )
                if (
                    plan.revision_number != expected_revision
                    or plan.prior_plan_revision_id != expected_prior
                ):
                    raise InvalidAuthorityTransition(
                        "plan revision does not extend the accepted plan"
                    )
                payload = encode_record(plan)
                self._insert_immutable(
                    cursor,
                    table="work_plan_revisions",
                    id_column="plan_revision_id",
                    record_id=plan.plan_revision_id,
                    columns=(
                        "case_id",
                        "frame_revision_id",
                        "revision_number",
                        "prior_plan_revision_id",
                        "content_sha256",
                        "payload",
                        "created_at",
                    ),
                    values=(
                        plan.case_id,
                        plan.frame_revision_id,
                        plan.revision_number,
                        plan.prior_plan_revision_id,
                        plan.content_sha256,
                        Jsonb(payload),
                        plan.created_at,
                    ),
                    payload=payload,
                    label="plan",
                )
                updated = self._move_heads(
                    cursor,
                    case=case,
                    recorded_at=recorded_at,
                    frame_id=case.accepted_frame_revision_id,
                    plan_id=plan.plan_revision_id,
                    answer_id=None,
                )
                self._append_authority_event(
                    cursor,
                    case_id=case.case_id,
                    event_id=event_id,
                    event_type=JournalEventType.PLAN_ACCEPTED,
                    recorded_at=recorded_at,
                    action_id=plan.created_by_action_id,
                    authority_ref=plan.plan_revision_id,
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
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                existing = self._event_by_id(cursor, event_id)
                if existing is not None:
                    if (
                        existing.event_type is JournalEventType.EVIDENCE_RECORDED
                        and existing.authority_ref == evidence.evidence_record_id
                    ):
                        return self._get_evidence(
                            cursor, evidence.evidence_record_id
                        )
                    raise AuthorityConflict(
                        "event ID already has different content"
                    )
                case = self._lock_case(
                    cursor, evidence.case_id, expected_head_version
                )
                if (
                    evidence.frame_revision_id != case.accepted_frame_revision_id
                    or evidence.plan_revision_id != case.accepted_plan_revision_id
                ):
                    raise InvalidAuthorityTransition(
                        "evidence must bind the accepted frame and plan"
                    )
                plan = self._get_plan(cursor, evidence.plan_revision_id)
                if evidence.task_id not in {task.task_id for task in plan.tasks}:
                    raise InvalidAuthorityTransition(
                        "evidence task is not in accepted plan"
                    )
                payload = encode_record(evidence)
                self._insert_immutable(
                    cursor,
                    table="evidence_records",
                    id_column="evidence_record_id",
                    record_id=evidence.evidence_record_id,
                    columns=(
                        "case_id",
                        "frame_revision_id",
                        "plan_revision_id",
                        "task_id",
                        "payload_sha256",
                        "payload",
                        "created_at",
                    ),
                    values=(
                        evidence.case_id,
                        evidence.frame_revision_id,
                        evidence.plan_revision_id,
                        evidence.task_id,
                        evidence.payload_sha256,
                        Jsonb(payload),
                        evidence.created_at,
                    ),
                    payload=payload,
                    label="evidence",
                )
                self._append_authority_event(
                    cursor,
                    case_id=case.case_id,
                    event_id=event_id,
                    event_type=JournalEventType.EVIDENCE_RECORDED,
                    recorded_at=recorded_at,
                    action_id=None,
                    authority_ref=evidence.evidence_record_id,
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
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                if answer.status is AnswerStatus.SETTLED:
                    raise InvalidAuthorityTransition(
                        "Gate 3 cannot publish settled answers"
                    )
                idempotent = self._idempotent_head_event(
                    cursor,
                    event_id=event_id,
                    event_type=JournalEventType.ANSWER_ACCEPTED,
                    authority_ref=answer.answer_version_id,
                    case_id=answer.case_id,
                )
                if idempotent is not None:
                    return idempotent
                case = self._lock_case(cursor, answer.case_id, expected_head_version)
                if (
                    answer.frame_revision_id != case.accepted_frame_revision_id
                    or answer.plan_revision_id != case.accepted_plan_revision_id
                ):
                    raise InvalidAuthorityTransition(
                        "answer must bind the accepted frame and plan"
                    )
                current = self._latest_answer_for_case(cursor, case.case_id)
                expected_version = (
                    1 if current is None else current.version_number + 1
                )
                expected_prior = (
                    None if current is None else current.answer_version_id
                )
                if (
                    answer.version_number != expected_version
                    or answer.prior_answer_version_id != expected_prior
                ):
                    raise InvalidAuthorityTransition(
                        "answer version does not extend the accepted answer"
                    )
                for claim in answer.claims:
                    for evidence_id in claim.evidence_record_ids:
                        evidence = self._get_evidence(cursor, evidence_id)
                        if (
                            evidence.frame_revision_id != answer.frame_revision_id
                            or evidence.plan_revision_id != answer.plan_revision_id
                        ):
                            raise InvalidAuthorityTransition(
                                "claim evidence is incompatible with answer"
                            )
                payload = encode_record(answer)
                self._insert_immutable(
                    cursor,
                    table="answer_versions",
                    id_column="answer_version_id",
                    record_id=answer.answer_version_id,
                    columns=(
                        "case_id",
                        "frame_revision_id",
                        "plan_revision_id",
                        "version_number",
                        "prior_answer_version_id",
                        "status",
                        "content_sha256",
                        "payload",
                        "created_at",
                    ),
                    values=(
                        answer.case_id,
                        answer.frame_revision_id,
                        answer.plan_revision_id,
                        answer.version_number,
                        answer.prior_answer_version_id,
                        answer.status.value,
                        answer.content_sha256,
                        Jsonb(payload),
                        answer.created_at,
                    ),
                    payload=payload,
                    label="answer",
                )
                updated = self._move_heads(
                    cursor,
                    case=case,
                    recorded_at=recorded_at,
                    frame_id=case.accepted_frame_revision_id,
                    plan_id=case.accepted_plan_revision_id,
                    answer_id=answer.answer_version_id,
                )
                self._append_authority_event(
                    cursor,
                    case_id=case.case_id,
                    event_id=event_id,
                    event_type=JournalEventType.ANSWER_ACCEPTED,
                    recorded_at=recorded_at,
                    action_id=answer.created_by_action_id,
                    authority_ref=answer.answer_version_id,
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
        def validate(
            cursor: Cursor[Mapping[str, Any]],
            record: MeasurementResolutionOutcome,
        ) -> None:
            if self._resolution_input_verifier is None:
                raise InvalidAuthorityTransition(
                    "measurement resolution admission verifier is not "
                    "configured"
                )
            try:
                self._resolution_input_verifier.verify_resolution_admission(
                    admission=admission,
                    outcome=record,
                )
            except ValueError as error:
                raise InvalidAuthorityTransition(str(error)) from error
            case = self._lock_case(
                cursor,
                record.case_id,
                expected_head_version,
            )
            if (
                record.question_revision_id
                != case.accepted_question_revision_id
                or record.frame_revision_id
                != case.accepted_frame_revision_id
            ):
                raise InvalidAuthorityTransition(
                    "resolution must bind accepted question and frame"
                )
            frame = self._get_frame(cursor, record.frame_revision_id)
            estimand_ids = tuple(
                item.estimand_id
                for item in frame.measurement_design.estimands
            )
            try:
                index = estimand_ids.index(record.estimand_id)
            except ValueError as error:
                raise InvalidAuthorityTransition(
                    "resolution targets an unknown estimand"
                ) from error
            if (
                record.semantic_measurement_id
                != frame.semantic_measurement_ids[index]
                or record.authority_binding_id
                != frame.authority_binding_ids[index]
            ):
                raise InvalidAuthorityTransition(
                    "resolution identity does not match the accepted frame"
                )
            if record.kind is ResolutionOutcomeKind.RESOLVED_INSTANCE:
                instance = record.resolved_instance
                assert instance is not None
                if (
                    instance.frame_revision_id != record.frame_revision_id
                    or instance.estimand_id != record.estimand_id
                    or instance.semantic_measurement_id
                    != record.semantic_measurement_id
                    or instance.authority_binding_id
                    != record.authority_binding_id
                ):
                    raise InvalidAuthorityTransition(
                        "resolved instance identity is inconsistent"
                    )
            try:
                validate_resolution_identities(record)
                validate_resolution_against_frame(frame, record)
            except ValueError as error:
                raise InvalidAuthorityTransition(str(error)) from error

        with self.atomic():
            stored = self._record_subordinate(
                record=outcome,
                record_id=outcome.resolution_outcome_id,
                case_id=outcome.case_id,
                table="measurement_resolution_outcomes",
                id_column="resolution_outcome_id",
                columns=(
                    "case_id",
                    "question_revision_id",
                    "frame_revision_id",
                    "estimand_id",
                    "semantic_measurement_id",
                    "authority_binding_id",
                    "outcome_kind",
                    "content_sha256",
                    "payload",
                    "created_at",
                    "schema_epoch",
                ),
                values=(
                    outcome.case_id,
                    outcome.question_revision_id,
                    outcome.frame_revision_id,
                    outcome.estimand_id,
                    outcome.semantic_measurement_id,
                    outcome.authority_binding_id,
                    outcome.kind.value,
                    outcome.content_sha256,
                    Jsonb(encode_record(outcome)),
                    outcome.created_at,
                    outcome.schema_epoch,
                ),
                event_id=event_id,
                event_type=(
                    JournalEventType.MEASUREMENT_RESOLUTION_RECORDED
                ),
                action_id=None,
                recorded_at=outcome.created_at,
                validator=validate,
                label="measurement resolution",
            )
            admission_payload = encode_record(admission)
            with self._cursor() as cursor:
                self._insert_idempotent_immutable(
                    cursor,
                    table="measurement_resolution_admissions",
                    id_column="resolution_outcome_id",
                    record_id=outcome.resolution_outcome_id,
                    columns=(
                        "issuer_ref",
                        "registry_content_sha256",
                        "resolver_input_bundle_sha256",
                        "resolution_context_sha256",
                        "payload",
                        "created_at",
                    ),
                    values=(
                        admission.issuer_ref,
                        admission.registry_content_sha256,
                        admission.resolver_input_bundle_sha256,
                        admission.resolution_context_sha256,
                        Jsonb(admission_payload),
                        outcome.created_at,
                    ),
                    payload=admission_payload,
                    label="measurement resolution admission",
                )
            return stored

    def record_evidence_obligation(
        self,
        obligation: ResolvedEvidenceObligation,
        *,
        expected_head_version: int,
        event_id: str,
    ) -> ResolvedEvidenceObligation:
        def validate(
            cursor: Cursor[Mapping[str, Any]],
            record: ResolvedEvidenceObligation,
        ) -> None:
            case = self._lock_case(
                cursor,
                record.case_id,
                expected_head_version,
            )
            if record.frame_revision_id != case.accepted_frame_revision_id:
                raise InvalidAuthorityTransition(
                    "obligation must bind the accepted frame"
                )
            outcome = self._get_payload(
                cursor,
                table="measurement_resolution_outcomes",
                id_column="resolution_outcome_id",
                record_id=record.resolution_outcome_id,
                label="measurement resolution",
                decoder=decode_measurement_resolution,
            )
            if (
                outcome.case_id != record.case_id
                or outcome.frame_revision_id != record.frame_revision_id
                or outcome.estimand_id != record.estimand_id
            ):
                raise InvalidAuthorityTransition(
                    "obligation resolution binding is inconsistent"
                )
            frame = self._get_frame(cursor, record.frame_revision_id)
            requirement = next(
                (
                    item
                    for item in frame.measurement_design.evidence_requirements
                    if item.evidence_requirement_id
                    == record.evidence_requirement_id
                ),
                None,
            )
            if (
                requirement is None
                or record.estimand_id not in requirement.target_estimand_ids
                or record.evidence_requirement_sha256
                != content_sha256(requirement)
            ):
                raise InvalidAuthorityTransition(
                    "obligation changes its evidence requirement"
                )

        return self._record_subordinate(
            record=obligation,
            record_id=obligation.obligation_id,
            case_id=obligation.case_id,
            table="resolved_evidence_obligations",
            id_column="obligation_id",
            columns=(
                "case_id",
                "frame_revision_id",
                "estimand_id",
                "evidence_requirement_id",
                "resolution_outcome_id",
                "content_sha256",
                "payload",
                "created_at",
                "schema_epoch",
            ),
            values=(
                obligation.case_id,
                obligation.frame_revision_id,
                obligation.estimand_id,
                obligation.evidence_requirement_id,
                obligation.resolution_outcome_id,
                obligation.content_sha256,
                Jsonb(encode_record(obligation)),
                obligation.created_at,
                obligation.schema_epoch,
            ),
            event_id=event_id,
            event_type=JournalEventType.EVIDENCE_OBLIGATION_RECORDED,
            action_id=None,
            recorded_at=obligation.created_at,
            validator=validate,
            label="evidence obligation",
        )

    def record_evidence_validity(
        self,
        validity: EvidenceValidityRecord,
        *,
        event_id: str,
    ) -> EvidenceValidityRecord:
        def validate(
            cursor: Cursor[Mapping[str, Any]],
            record: EvidenceValidityRecord,
        ) -> None:
            self._get_evidence(cursor, record.evidence_record_id)
            cursor.execute(
                """
                SELECT current.payload
                FROM waje_vnext.evidence_validity_records AS current
                WHERE current.evidence_record_id = %s
                  AND NOT EXISTS (
                    SELECT 1
                    FROM waje_vnext.evidence_validity_records AS successor
                    WHERE successor.prior_validity_record_id
                        = current.evidence_validity_record_id
                  )
                FOR UPDATE
                """,
                (record.evidence_record_id,),
            )
            rows = cursor.fetchall()
            if len(rows) > 1:
                raise AuthorityConflict(
                    "evidence validity chain has multiple heads"
                )
            current = (
                None
                if not rows
                else decode_evidence_validity(rows[0]["payload"])
            )
            if current is None:
                if record.prior_validity_record_id is not None:
                    raise InvalidAuthorityTransition(
                        "first validity record cannot have a prior"
                    )
            elif (
                record.prior_validity_record_id
                != current.evidence_validity_record_id
                or record.expected_prior_content_sha256
                != current.content_sha256
            ):
                raise InvalidAuthorityTransition(
                    "validity record does not extend current disposition"
                )

        evidence = self.get_evidence(validity.evidence_record_id)
        return self._record_subordinate(
            record=validity,
            record_id=validity.evidence_validity_record_id,
            case_id=evidence.case_id,
            table="evidence_validity_records",
            id_column="evidence_validity_record_id",
            columns=(
                "evidence_record_id",
                "prior_validity_record_id",
                "disposition_status",
                "content_sha256",
                "payload",
                "created_at",
                "schema_epoch",
            ),
            values=(
                validity.evidence_record_id,
                validity.prior_validity_record_id,
                validity.status.value,
                validity.content_sha256,
                Jsonb(encode_record(validity)),
                validity.created_at,
                validity.schema_epoch,
            ),
            event_id=event_id,
            event_type=JournalEventType.EVIDENCE_VALIDITY_RECORDED,
            action_id=None,
            recorded_at=validity.created_at,
            validator=validate,
            label="evidence validity",
        )

    def record_obligation_satisfaction(
        self,
        satisfaction: ObligationSatisfactionRecord,
        *,
        event_id: str,
    ) -> ObligationSatisfactionRecord:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                obligation = self._get_payload(
                    cursor,
                    table="resolved_evidence_obligations",
                    id_column="obligation_id",
                    record_id=satisfaction.obligation_id,
                    label="evidence obligation",
                    decoder=decode_evidence_obligation,
                )
        return self._record_subordinate(
            record=satisfaction,
            record_id=satisfaction.satisfaction_record_id,
            case_id=obligation.case_id,
            table="obligation_satisfaction_records",
            id_column="satisfaction_record_id",
            columns=(
                "obligation_id",
                "satisfaction_status",
                "content_sha256",
                "payload",
                "created_at",
                "schema_epoch",
            ),
            values=(
                satisfaction.obligation_id,
                satisfaction.status.value,
                satisfaction.content_sha256,
                Jsonb(encode_record(satisfaction)),
                satisfaction.created_at,
                satisfaction.schema_epoch,
            ),
            event_id=event_id,
            event_type=(
                JournalEventType.OBLIGATION_SATISFACTION_RECORDED
            ),
            action_id=None,
            recorded_at=satisfaction.created_at,
            validator=lambda cursor, record: self._get_payload(
                cursor,
                table="resolved_evidence_obligations",
                id_column="obligation_id",
                record_id=record.obligation_id,
                label="evidence obligation",
                decoder=decode_evidence_obligation,
            ),
            label="obligation satisfaction",
        )

    def record_settlement_precondition(
        self,
        report: SettlementPreconditionReport,
        *,
        expected_head_version: int,
        event_id: str,
    ) -> SettlementPreconditionReport:
        def validate(
            cursor: Cursor[Mapping[str, Any]],
            record: SettlementPreconditionReport,
        ) -> None:
            case = self._lock_case(
                cursor,
                record.case_id,
                expected_head_version,
            )
            if (
                record.accepted_head_version != case.head_version
                or record.question_revision_id
                != case.accepted_question_revision_id
                or record.frame_revision_id
                != case.accepted_frame_revision_id
                or record.plan_revision_id
                != case.accepted_plan_revision_id
            ):
                raise InvalidAuthorityTransition(
                    "settlement precondition is stale"
                )
            frame = self._get_frame(cursor, record.frame_revision_id)
            if (
                record.semantic_measurement_ids
                != frame.semantic_measurement_ids
                or record.authority_binding_ids
                != frame.authority_binding_ids
            ):
                raise InvalidAuthorityTransition(
                    "settlement precondition changes frame identity"
                )
            for outcome_id in record.resolution_outcome_ids:
                outcome = self._get_payload(
                    cursor,
                    table="measurement_resolution_outcomes",
                    id_column="resolution_outcome_id",
                    record_id=outcome_id,
                    label="measurement resolution",
                    decoder=decode_measurement_resolution,
                )
                if outcome.frame_revision_id != record.frame_revision_id:
                    raise InvalidAuthorityTransition(
                        "settlement resolution belongs to another frame"
                    )

        return self._record_subordinate(
            record=report,
            record_id=report.settlement_precondition_report_id,
            case_id=report.case_id,
            table="settlement_precondition_reports",
            id_column="settlement_precondition_report_id",
            columns=(
                "case_id",
                "question_revision_id",
                "frame_revision_id",
                "plan_revision_id",
                "precondition_status",
                "content_sha256",
                "payload",
                "created_at",
                "schema_epoch",
            ),
            values=(
                report.case_id,
                report.question_revision_id,
                report.frame_revision_id,
                report.plan_revision_id,
                report.status.value,
                report.content_sha256,
                Jsonb(encode_record(report)),
                report.created_at,
                report.schema_epoch,
            ),
            event_id=event_id,
            event_type=JournalEventType.SETTLEMENT_PRECONDITION_RECORDED,
            action_id=None,
            recorded_at=report.created_at,
            validator=validate,
            label="settlement precondition",
        )

    def record_interpretation(
        self,
        interpretation: InterpretationRecord,
        *,
        event_id: str,
    ) -> InterpretationRecord:
        return self._record_subordinate(
            record=interpretation,
            record_id=interpretation.interpretation_id,
            case_id=interpretation.case_id,
            table="interpretation_records",
            id_column="interpretation_id",
            columns=("case_id", "frame_revision_id", "payload", "created_at"),
            values=(
                interpretation.case_id,
                interpretation.frame_revision_id,
                Jsonb(encode_record(interpretation)),
                interpretation.created_at,
            ),
            event_id=event_id,
            event_type=JournalEventType.INTERPRETATION_RECORDED,
            action_id=interpretation.created_by_action_id,
            recorded_at=interpretation.created_at,
            validator=self._validate_interpretation,
            label="interpretation",
        )

    def record_decision(
        self,
        decision: DecisionRecord,
        *,
        event_id: str,
    ) -> DecisionRecord:
        return self._record_subordinate(
            record=decision,
            record_id=decision.decision_record_id,
            case_id=decision.case_id,
            table="decision_records",
            id_column="decision_record_id",
            columns=("case_id", "payload", "created_at"),
            values=(
                decision.case_id,
                Jsonb(encode_record(decision)),
                decision.created_at,
            ),
            event_id=event_id,
            event_type=JournalEventType.USER_DECISION_RECORDED,
            action_id=None,
            recorded_at=decision.created_at,
            validator=lambda cursor, record: self._get_case(
                cursor, record.case_id
            ),
            label="decision",
        )

    def record_reviewer_objection(
        self,
        objection: ReviewerObjection,
        *,
        event_id: str,
    ) -> ReviewerObjection:
        return self._record_subordinate(
            record=objection,
            record_id=objection.objection_id,
            case_id=objection.case_id,
            table="reviewer_objections",
            id_column="objection_id",
            columns=(
                "objection_key",
                "revision_number",
                "prior_objection_id",
                "case_id",
                "answer_version_id",
                "claim_id",
                "severity",
                "status",
                "payload",
                "created_at",
            ),
            values=(
                objection.objection_key,
                objection.revision_number,
                objection.prior_objection_id,
                objection.case_id,
                objection.answer_version_id,
                objection.claim_id,
                objection.severity.value,
                objection.status.value,
                Jsonb(encode_record(objection)),
                objection.created_at,
            ),
            event_id=event_id,
            event_type=JournalEventType.REVIEWER_OBJECTION_RECORDED,
            action_id=None,
            recorded_at=objection.created_at,
            validator=self._validate_objection,
            label="reviewer objection",
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
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                idempotent = self._idempotent_head_event(
                    cursor,
                    event_id=event_id,
                    event_type=event_type,
                    authority_ref=case_id,
                    case_id=case_id,
                )
                if idempotent is not None:
                    return idempotent
                case = self._lock_case(
                    cursor,
                    case_id,
                    expected_head_version,
                )
                if case.lifecycle in {
                    CaseLifecycle.STOPPED,
                    CaseLifecycle.CLOSED,
                }:
                    raise InvalidAuthorityTransition(
                        "case is already terminal"
                    )
                cursor.execute(
                    """
                    UPDATE waje_vnext.investigation_cases
                    SET
                        lifecycle = %s,
                        head_version = head_version + 1,
                        updated_at = %s
                    WHERE case_id = %s AND head_version = %s
                    RETURNING *
                    """,
                    (
                        lifecycle.value,
                        recorded_at,
                        case_id,
                        case.head_version,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise StaleHead("case head changed during transaction")
                updated = _case_from_row(row)
                self._append_authority_event(
                    cursor,
                    case_id=case_id,
                    event_id=event_id,
                    event_type=event_type,
                    recorded_at=recorded_at,
                    action_id=action_id,
                    authority_ref=case_id,
                    payload={
                        "lifecycle": lifecycle.value,
                        "head_version": updated.head_version,
                    },
                    operation=operation,
                )
                return updated

    def record_action(self, action: PersistedAction) -> PersistedAction:
        payload = encode_record(action)
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                self._get_case(cursor, action.action.case_id)
                try:
                    self._insert_idempotent_immutable(
                        cursor,
                        table="action_records",
                        id_column="action_id",
                        record_id=action.action.action_id,
                        columns=(
                            "case_id",
                            "expected_head_version",
                            "idempotency_key",
                            "operation_id",
                            "causation_id",
                            "correlation_id",
                            "authority_revision",
                            "payload_sha256",
                            "proposal_sha256",
                            "payload",
                            "recorded_at",
                        ),
                        values=(
                            action.action.case_id,
                            action.action.expected_head_version,
                            action.action.idempotency_key,
                            action.action.operation.operation_id,
                            action.action.operation.causation_id,
                            action.action.operation.correlation_id,
                            action.action.operation.authority_revision,
                            action.action.operation.payload_sha256,
                            action.proposal_sha256,
                            Jsonb(payload),
                            action.recorded_at,
                        ),
                        payload=payload,
                        label="action",
                    )
                except errors.UniqueViolation as error:
                    raise AuthorityConflict(
                        "action idempotency key already has different content"
                    ) from error
                return action

    def get_action(self, action_id: str) -> PersistedAction:
        return self._get_authority(
            table="action_records",
            id_column="action_id",
            record_id=action_id,
            label="action",
            decoder=decode_persisted_action,
        )

    def record_context_packet(self, packet: ContextPacket) -> ContextPacket:
        payload = encode_record(packet)
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                case = self._get_case(cursor, packet.case_id)
                if packet.head_version != case.head_version:
                    raise StaleHead(
                        "ContextPacket was built from a stale case head"
                    )
                self._insert_idempotent_immutable(
                    cursor,
                    table="context_packets",
                    id_column="packet_id",
                    record_id=packet.packet_id,
                    columns=(
                        "case_id",
                        "head_version",
                        "content_sha256",
                        "payload",
                        "built_at",
                    ),
                    values=(
                        packet.case_id,
                        packet.head_version,
                        packet.content_sha256,
                        Jsonb(payload),
                        packet.built_at,
                    ),
                    payload=payload,
                    label="ContextPacket",
                )
                return packet

    def get_context_packet(self, packet_id: str) -> ContextPacket:
        return self._get_authority(
            table="context_packets",
            id_column="packet_id",
            record_id=packet_id,
            label="ContextPacket",
            decoder=decode_context_packet,
        )

    def record_action_receipt(
        self,
        receipt: ActionReceipt,
    ) -> ActionReceipt:
        payload = encode_record(receipt)
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                action = self._get_payload(
                    cursor,
                    table="action_records",
                    id_column="action_id",
                    record_id=receipt.action_id,
                    label="action",
                    decoder=decode_persisted_action,
                ).action
                if action.case_id != receipt.case_id:
                    raise InvalidAuthorityTransition(
                        "action receipt case does not match action"
                    )
                if action.content_sha256 != receipt.request_sha256:
                    raise AuthorityConflict(
                        "action receipt request hash does not match action"
                    )
                self._require_event_cursor(
                    cursor,
                    receipt.case_id,
                    receipt.event_cursor,
                )
                cursor.execute(
                    """
                    INSERT INTO waje_vnext.action_receipts (
                        case_id,
                        idempotency_key,
                        action_id,
                        request_sha256,
                        result_sha256,
                        event_cursor,
                        payload,
                        recorded_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (case_id, idempotency_key) DO NOTHING
                    RETURNING action_id
                    """,
                    (
                        receipt.case_id,
                        receipt.idempotency_key,
                        receipt.action_id,
                        receipt.request_sha256,
                        receipt.result_sha256,
                        receipt.event_cursor,
                        Jsonb(payload),
                        receipt.recorded_at,
                    ),
                )
                if cursor.fetchone() is None:
                    existing = self._get_action_receipt(
                        cursor,
                        receipt.case_id,
                        receipt.idempotency_key,
                    )
                    if existing != receipt:
                        raise AuthorityConflict(
                            "idempotency key already has a different receipt"
                        )
                return receipt

    def get_action_receipt(
        self,
        case_id: str,
        idempotency_key: str,
    ) -> ActionReceipt | None:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                return self._get_action_receipt(
                    cursor,
                    case_id,
                    idempotency_key,
                )

    def record_checkpoint(
        self,
        checkpoint: CheckpointRecord,
    ) -> CheckpointRecord:
        payload = encode_record(checkpoint)
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                case = self._get_case(cursor, checkpoint.case_id)
                if checkpoint.head_version != case.head_version:
                    raise StaleHead(
                        "checkpoint head does not match current case"
                    )
                packet = self._get_payload(
                    cursor,
                    table="context_packets",
                    id_column="packet_id",
                    record_id=checkpoint.context_packet_id,
                    label="ContextPacket",
                    decoder=decode_context_packet,
                )
                if (
                    packet.case_id != checkpoint.case_id
                    or packet.content_sha256 != checkpoint.context_sha256
                ):
                    raise InvalidAuthorityTransition(
                        "checkpoint context binding is invalid"
                    )
                event = self._require_event_cursor(
                    cursor,
                    checkpoint.case_id,
                    checkpoint.event_cursor,
                )
                if event.event_type is not JournalEventType.CHECKPOINT_RECORDED:
                    raise InvalidAuthorityTransition(
                        "checkpoint must bind a checkpoint event"
                    )
                self._insert_idempotent_immutable(
                    cursor,
                    table="checkpoint_records",
                    id_column="checkpoint_id",
                    record_id=checkpoint.checkpoint_id,
                    columns=(
                        "case_id",
                        "head_version",
                        "event_cursor",
                        "context_packet_id",
                        "context_sha256",
                        "state_sha256",
                        "payload",
                        "created_at",
                    ),
                    values=(
                        checkpoint.case_id,
                        checkpoint.head_version,
                        checkpoint.event_cursor,
                        checkpoint.context_packet_id,
                        checkpoint.context_sha256,
                        checkpoint.state_sha256,
                        Jsonb(payload),
                        checkpoint.created_at,
                    ),
                    payload=payload,
                    label="checkpoint",
                )
                return checkpoint

    def latest_checkpoint(self, case_id: str) -> CheckpointRecord | None:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                self._get_case(cursor, case_id)
                cursor.execute(
                    """
                    SELECT payload
                    FROM waje_vnext.checkpoint_records
                    WHERE case_id = %s
                    ORDER BY event_cursor DESC
                    LIMIT 1
                    """,
                    (case_id,),
                )
                row = cursor.fetchone()
                return (
                    None
                    if row is None
                    else decode_checkpoint(row["payload"])
                )

    def enqueue_outbox(self, message: OutboxMessage) -> OutboxMessage:
        payload = encode_record(message)
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                case = self._get_case(cursor, message.case_id)
                if message.expected_head_version != case.head_version:
                    raise StaleHead("outbox expected case head is stale")
                cursor.execute(
                    """
                    SELECT authority_epoch
                    FROM waje_vnext.case_mailbox_heads
                    WHERE case_id = %s
                    """,
                    (message.case_id,),
                )
                mailbox_epoch = cursor.fetchone()["authority_epoch"]
                if message.expected_authority_epoch != mailbox_epoch:
                    raise StaleHead(
                        "outbox expected mailbox authority is stale"
                    )
                if (
                    message.authority_snapshot
                    != self._authority_snapshot_from_cursor(
                        cursor,
                        message.case_id,
                    )
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
                    cursor,
                    message.case_id,
                    message.source_event_cursor,
                )
                if message.action_id is not None:
                    action = self._get_payload(
                        cursor,
                        table="action_records",
                        id_column="action_id",
                        record_id=message.action_id,
                        label="action",
                        decoder=decode_persisted_action,
                    )
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
                        cursor.execute(
                            """
                            SELECT frame_candidate_id
                            FROM waje_vnext.active_frame_candidate_heads
                            WHERE case_id = %s
                            """,
                            (message.case_id,),
                        )
                        active = cursor.fetchone()
                        if (
                            active is None
                            or message.payload.get("frame_candidate_id")
                            != active["frame_candidate_id"]
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
                try:
                    self._insert_idempotent_immutable(
                        cursor,
                        table="outbox_messages",
                        id_column="outbox_message_id",
                        record_id=message.outbox_message_id,
                        columns=(
                            "case_id",
                            "source_event_cursor",
                            "action_id",
                            "job_kind",
                            "operation_id",
                            "idempotency_key",
                            "causation_id",
                            "correlation_id",
                            "authority_revision",
                            "expected_head_version",
                            "expected_authority_epoch",
                            "destination",
                            "contract_ref",
                            "payload_sha256",
                            "payload",
                            "created_at",
                        ),
                        values=(
                            message.case_id,
                            message.source_event_cursor,
                            message.action_id,
                            message.job_kind.value,
                            message.operation.operation_id,
                            message.idempotency_key,
                            message.operation.causation_id,
                            message.operation.correlation_id,
                            message.operation.authority_revision,
                            message.expected_head_version,
                            message.expected_authority_epoch,
                            message.destination,
                            message.contract_ref,
                            message.payload_sha256,
                            Jsonb(payload),
                            message.created_at,
                        ),
                        payload=payload,
                        label="outbox message",
                    )
                except errors.UniqueViolation as error:
                    raise AuthorityConflict(
                        "outbox idempotency key already has different content"
                    ) from error
                return message

    def get_outbox_message(self, message_id: str) -> OutboxMessage:
        return self._get_authority(
            table="outbox_messages",
            id_column="outbox_message_id",
            record_id=message_id,
            label="outbox message",
            decoder=decode_outbox_message,
        )

    def list_outbox_messages(
        self,
        *,
        case_id: str | None = None,
    ) -> tuple[OutboxMessage, ...]:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                if case_id is None:
                    cursor.execute(
                        """
                        SELECT payload
                        FROM waje_vnext.outbox_messages
                        ORDER BY created_at, source_event_cursor,
                                 outbox_message_id
                        """
                    )
                else:
                    self._get_case(cursor, case_id)
                    cursor.execute(
                        """
                        SELECT payload
                        FROM waje_vnext.outbox_messages
                        WHERE case_id = %s
                        ORDER BY created_at, source_event_cursor,
                                 outbox_message_id
                        """,
                        (case_id,),
                    )
                return tuple(
                    decode_outbox_message(row["payload"])
                    for row in cursor.fetchall()
                )

    def list_pending_outbox_messages(
        self,
        *,
        case_id: str | None = None,
    ) -> tuple[OutboxMessage, ...]:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                where = sql.SQL("")
                parameters: tuple[object, ...] = ()
                if case_id is not None:
                    self._get_case(cursor, case_id)
                    where = sql.SQL("AND o.case_id = %s")
                    parameters = (case_id,)
                cursor.execute(
                    sql.SQL(
                        """
                        SELECT o.payload
                        FROM waje_vnext.outbox_messages o
                        WHERE NOT EXISTS (
                            SELECT 1
                            FROM waje_vnext.job_disposition_records d
                            WHERE d.outbox_message_id =
                                o.outbox_message_id
                        )
                        {}
                        ORDER BY o.created_at, o.source_event_cursor,
                                 o.outbox_message_id
                        """
                    ).format(where),
                    parameters,
                )
                return tuple(
                    decode_outbox_message(row["payload"])
                    for row in cursor.fetchall()
                )

    def record_job_disposition(
        self,
        disposition: JobDispositionRecord,
    ) -> JobDispositionRecord:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                message = self._get_payload(
                    cursor,
                    table="outbox_messages",
                    id_column="outbox_message_id",
                    record_id=disposition.outbox_message_id,
                    label="outbox message",
                    decoder=decode_outbox_message,
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
                    else:
                        cursor.execute(
                            """
                            SELECT authority_epoch
                            FROM waje_vnext.case_mailbox_heads
                            WHERE case_id = %s
                            """,
                            (message.case_id,),
                        )
                        current_epoch = cursor.fetchone()[
                            "authority_epoch"
                        ]
                        if (
                            disposition.observed_authority_epoch
                            != current_epoch
                        ):
                            raise InvalidAuthorityTransition(
                                "completed disposition observed stale "
                                "authority"
                            )
                if disposition.fencing_token is not None:
                    cursor.execute(
                        "SELECT clock_timestamp() AS database_now"
                    )
                    database_now = cursor.fetchone()["database_now"]
                    cursor.execute(
                        """
                        SELECT *
                        FROM waje_vnext.outbox_delivery_leases
                        WHERE outbox_message_id = %s
                        FOR UPDATE
                        """,
                        (disposition.outbox_message_id,),
                    )
                    lease = cursor.fetchone()
                    if (
                        lease is None
                        or not lease["active"]
                        or lease["owner_id"] != disposition.owner_id
                        or lease["fencing_token"]
                        != disposition.fencing_token
                        or lease["expires_at"] <= database_now
                    ):
                        raise LeaseFenceLost(
                            "job disposition uses a stale delivery fence"
                        )
                payload = encode_record(disposition)
                try:
                    self._insert_idempotent_immutable(
                        cursor,
                        table="job_disposition_records",
                        id_column="job_disposition_record_id",
                        record_id=(
                            disposition.job_disposition_record_id
                        ),
                        columns=(
                            "outbox_message_id",
                            "case_id",
                            "disposition",
                            "owner_id",
                            "fencing_token",
                            "payload",
                        ),
                        values=(
                            disposition.outbox_message_id,
                            disposition.case_id,
                            disposition.disposition.value,
                            disposition.owner_id,
                            disposition.fencing_token,
                            Jsonb(payload),
                        ),
                        payload=payload,
                        label="job disposition",
                    )
                except errors.UniqueViolation as error:
                    raise AuthorityConflict(
                        "outbox job already has another terminal disposition"
                    ) from error
                return disposition

    def get_job_disposition(
        self,
        outbox_message_id: str,
    ) -> JobDispositionRecord | None:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                self._get_payload(
                    cursor,
                    table="outbox_messages",
                    id_column="outbox_message_id",
                    record_id=outbox_message_id,
                    label="outbox message",
                    decoder=decode_outbox_message,
                )
                cursor.execute(
                    """
                    SELECT payload
                    FROM waje_vnext.job_disposition_records
                    WHERE outbox_message_id = %s
                    """,
                    (outbox_message_id,),
                )
                row = cursor.fetchone()
                return (
                    None
                    if row is None
                    else decode_job_disposition(row["payload"])
                )

    def list_job_dispositions(
        self,
        case_id: str,
    ) -> tuple[JobDispositionRecord, ...]:
        return self._list_payloads(
            table="job_disposition_records",
            case_id=case_id,
            order_by=("job_disposition_record_id",),
            decoder=decode_job_disposition,
        )

    def advance_dispatcher_recovery_cursor(
        self,
        recovery_cursor: DispatcherRecoveryCursor,
    ) -> DispatcherRecoveryCursor:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO waje_vnext.dispatcher_recovery_cursors (
                        dispatcher_id,
                        last_outbox_created_at,
                        last_source_event_cursor,
                        last_outbox_message_id,
                        updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (dispatcher_id) DO UPDATE
                    SET last_outbox_created_at =
                            EXCLUDED.last_outbox_created_at,
                        last_outbox_message_id =
                            EXCLUDED.last_outbox_message_id,
                        last_source_event_cursor =
                            EXCLUDED.last_source_event_cursor,
                        updated_at = EXCLUDED.updated_at
                    WHERE
                        (
                            waje_vnext.dispatcher_recovery_cursors
                                .last_outbox_created_at IS NULL
                            AND EXCLUDED.last_outbox_created_at IS NOT NULL
                        )
                        OR (
                            (
                                waje_vnext.dispatcher_recovery_cursors
                                    .last_outbox_created_at,
                                waje_vnext.dispatcher_recovery_cursors
                                    .last_source_event_cursor,
                                waje_vnext.dispatcher_recovery_cursors
                                    .last_outbox_message_id
                            )
                            < (
                                EXCLUDED.last_outbox_created_at,
                                EXCLUDED.last_source_event_cursor,
                                EXCLUDED.last_outbox_message_id
                            )
                        )
                    RETURNING *
                    """,
                    (
                        recovery_cursor.dispatcher_id,
                        recovery_cursor.last_outbox_created_at,
                        recovery_cursor.last_source_event_cursor,
                        recovery_cursor.last_outbox_message_id,
                        recovery_cursor.updated_at,
                    ),
                )
                row = cursor.fetchone()
                if row is not None:
                    return _dispatcher_recovery_cursor_from_row(row)
                cursor.execute(
                    """
                    SELECT *
                    FROM waje_vnext.dispatcher_recovery_cursors
                    WHERE dispatcher_id = %s
                    """,
                    (recovery_cursor.dispatcher_id,),
                )
                persisted = _dispatcher_recovery_cursor_from_row(
                    cursor.fetchone()
                )
                if persisted.position == recovery_cursor.position:
                    return persisted
                raise InvalidAuthorityTransition(
                    "dispatcher recovery cursor cannot move backwards"
                )

    def get_dispatcher_recovery_cursor(
        self,
        dispatcher_id: str,
    ) -> DispatcherRecoveryCursor | None:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                cursor.execute(
                    """
                    SELECT *
                    FROM waje_vnext.dispatcher_recovery_cursors
                    WHERE dispatcher_id = %s
                    """,
                    (dispatcher_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                return _dispatcher_recovery_cursor_from_row(row)

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
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                cursor.execute(
                    """
                    SELECT outbox_message_id
                    FROM waje_vnext.outbox_messages
                    WHERE outbox_message_id = %s
                    FOR UPDATE
                    """,
                    (outbox_message_id,),
                )
                if cursor.fetchone() is None:
                    raise AuthorityNotFound(
                        "outbox message {!r} does not exist".format(
                            outbox_message_id
                        )
                    )
                cursor.execute(
                    """
                    SELECT 1
                    FROM waje_vnext.job_disposition_records
                    WHERE outbox_message_id = %s
                    """,
                    (outbox_message_id,),
                )
                if cursor.fetchone() is not None:
                    raise LeaseConflict(
                        "terminally disposed job cannot be claimed"
                    )
                cursor.execute(
                    "SELECT clock_timestamp() AS database_now"
                )
                database_now = cursor.fetchone()["database_now"]
                cursor.execute(
                    """
                    SELECT *
                    FROM waje_vnext.outbox_delivery_leases
                    WHERE outbox_message_id = %s
                    FOR UPDATE
                    """,
                    (outbox_message_id,),
                )
                row = cursor.fetchone()
                current = (
                    None
                    if row is None
                    else _job_lease_from_row(row)
                )
                active = False if row is None else row["active"]
                if (
                    current is not None
                    and active
                    and current.expires_at > database_now
                ):
                    raise LeaseConflict(
                        "job already has an active delivery lease"
                    )
                token = (
                    1
                    if current is None
                    else current.fencing_token + 1
                )
                lease = JobLease(
                    outbox_message_id=outbox_message_id,
                    owner_id=owner_id,
                    fencing_token=token,
                    acquired_at=database_now,
                    heartbeat_at=database_now,
                    expires_at=database_now + lease_duration,
                )
                cursor.execute(
                    """
                    INSERT INTO waje_vnext.outbox_delivery_leases (
                        outbox_message_id,
                        owner_id,
                        fencing_token,
                        active,
                        acquired_at,
                        heartbeat_at,
                        expires_at
                    ) VALUES (%s, %s, %s, true, %s, %s, %s)
                    ON CONFLICT (outbox_message_id) DO UPDATE SET
                        owner_id = EXCLUDED.owner_id,
                        fencing_token = EXCLUDED.fencing_token,
                        active = true,
                        acquired_at = EXCLUDED.acquired_at,
                        heartbeat_at = EXCLUDED.heartbeat_at,
                        expires_at = EXCLUDED.expires_at
                    """,
                    (
                        lease.outbox_message_id,
                        lease.owner_id,
                        lease.fencing_token,
                        lease.acquired_at,
                        lease.heartbeat_at,
                        lease.expires_at,
                    ),
                )
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
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                cursor.execute(
                    "SELECT clock_timestamp() AS database_now"
                )
                database_now = cursor.fetchone()["database_now"]
                renewed = JobLease(
                    outbox_message_id=lease.outbox_message_id,
                    owner_id=lease.owner_id,
                    fencing_token=lease.fencing_token,
                    acquired_at=lease.acquired_at,
                    heartbeat_at=database_now,
                    expires_at=database_now + lease_duration,
                )
                cursor.execute(
                    """
                    UPDATE waje_vnext.outbox_delivery_leases
                    SET heartbeat_at = %s, expires_at = %s
                    WHERE outbox_message_id = %s
                      AND owner_id = %s
                      AND fencing_token = %s
                      AND active = true
                      AND heartbeat_at = %s
                      AND expires_at = %s
                      AND expires_at > %s
                    """,
                    (
                        renewed.heartbeat_at,
                        renewed.expires_at,
                        renewed.outbox_message_id,
                        renewed.owner_id,
                        renewed.fencing_token,
                        lease.heartbeat_at,
                        lease.expires_at,
                        database_now,
                    ),
                )
                if cursor.rowcount != 1:
                    raise LeaseFenceLost(
                        "job delivery lease fencing token was lost"
                    )
                return renewed

    def release_job_lease(self, lease: JobLease) -> None:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE waje_vnext.outbox_delivery_leases
                    SET active = false
                    WHERE outbox_message_id = %s
                      AND owner_id = %s
                      AND fencing_token = %s
                      AND active = true
                      AND heartbeat_at = %s
                      AND expires_at = %s
                    """,
                    (
                        lease.outbox_message_id,
                        lease.owner_id,
                        lease.fencing_token,
                        lease.heartbeat_at,
                        lease.expires_at,
                    ),
                )
                if cursor.rowcount != 1:
                    raise LeaseFenceLost(
                        "job delivery lease fencing token was lost"
                    )

    def assert_job_lease(
        self,
        lease: JobLease,
        *,
        checked_at: datetime,
    ) -> JobLease:
        del checked_at
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                cursor.execute(
                    "SELECT clock_timestamp() AS database_now"
                )
                database_now = cursor.fetchone()["database_now"]
                cursor.execute(
                    """
                    SELECT *
                    FROM waje_vnext.outbox_delivery_leases
                    WHERE outbox_message_id = %s
                    FOR UPDATE
                    """,
                    (lease.outbox_message_id,),
                )
                row = cursor.fetchone()
                if (
                    row is None
                    or not row["active"]
                    or row["owner_id"] != lease.owner_id
                    or row["fencing_token"] != lease.fencing_token
                    or row["heartbeat_at"] != lease.heartbeat_at
                    or row["expires_at"] != lease.expires_at
                    or row["expires_at"] <= database_now
                ):
                    raise LeaseFenceLost(
                        "job delivery lease is stale, expired, or superseded"
                    )
                return _job_lease_from_row(row)

    def record_decision_request(
        self,
        request: UserDecisionRequest,
    ) -> UserDecisionRequest:
        payload = encode_record(request)
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                action = self._get_payload(
                    cursor,
                    table="action_records",
                    id_column="action_id",
                    record_id=request.action_id,
                    label="action",
                    decoder=decode_persisted_action,
                )
                if action.action.case_id != request.case_id:
                    raise InvalidAuthorityTransition(
                        "decision request case does not match action"
                    )
                action_payload = action.action.payload
                if (
                    action.action.kind is not ActionKind.ASK_USER
                    or not isinstance(action_payload, AskUserPayload)
                ):
                    raise InvalidAuthorityTransition(
                        "decision request requires an ask_user action"
                    )
                if (
                    request.question != action_payload.question
                    or request.options != action_payload.options
                    or request.recommended_option_id
                    != action_payload.recommended_option_id
                    or request.allow_freeform
                    != action_payload.allow_freeform
                ):
                    raise AuthorityConflict(
                        "decision request does not match ask_user action"
                    )
                try:
                    self._insert_idempotent_immutable(
                        cursor,
                        table="user_decision_requests",
                        id_column="decision_request_id",
                        record_id=request.decision_request_id,
                        columns=(
                            "case_id",
                            "action_id",
                            "payload",
                            "requested_at",
                        ),
                        values=(
                            request.case_id,
                            request.action_id,
                            Jsonb(payload),
                            request.requested_at,
                        ),
                        payload=payload,
                        label="decision request",
                    )
                except errors.UniqueViolation as error:
                    raise AuthorityConflict(
                        "action already has a decision request"
                    ) from error
                return request

    def get_decision_request(
        self,
        request_id: str,
    ) -> UserDecisionRequest:
        return self._get_authority(
            table="user_decision_requests",
            id_column="decision_request_id",
            record_id=request_id,
            label="decision request",
            decoder=decode_decision_request,
        )

    def record_effect_attempt(
        self,
        attempt: EffectAttemptRecord,
    ) -> EffectAttemptRecord:
        payload = encode_record(attempt)
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                message = self._get_payload(
                    cursor,
                    table="outbox_messages",
                    id_column="outbox_message_id",
                    record_id=attempt.outbox_message_id,
                    label="outbox message",
                    decoder=decode_outbox_message,
                )
                if message.case_id != attempt.case_id:
                    raise InvalidAuthorityTransition(
                        "effect attempt case does not match outbox"
                    )
                cursor.execute(
                    """
                    SELECT payload
                    FROM waje_vnext.effect_attempts
                    WHERE outbox_message_id = %s
                    ORDER BY attempt_number DESC
                    LIMIT 1
                    FOR UPDATE
                    """,
                    (attempt.outbox_message_id,),
                )
                row = cursor.fetchone()
                current = (
                    None
                    if row is None
                    else decode_effect_attempt(row["payload"])
                )
                if (
                    current is not None
                    and current.effect_attempt_id == attempt.effect_attempt_id
                ):
                    if current == attempt:
                        return current
                    raise AuthorityConflict(
                        "effect attempt ID already has different content"
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
                try:
                    self._insert_idempotent_immutable(
                        cursor,
                        table="effect_attempts",
                        id_column="effect_attempt_id",
                        record_id=attempt.effect_attempt_id,
                        columns=(
                            "outbox_message_id",
                            "case_id",
                            "attempt_number",
                            "prior_attempt_id",
                            "status",
                            "payload",
                            "started_at",
                            "completed_at",
                        ),
                        values=(
                            attempt.outbox_message_id,
                            attempt.case_id,
                            attempt.attempt_number,
                            attempt.prior_attempt_id,
                            attempt.status.value,
                            Jsonb(payload),
                            attempt.started_at,
                            attempt.completed_at,
                        ),
                        payload=payload,
                        label="effect attempt",
                    )
                except errors.UniqueViolation as error:
                    raise AuthorityConflict(
                        "effect attempt number already exists"
                    ) from error
                return attempt

    def list_effect_attempts(
        self,
        outbox_message_id: str,
    ) -> tuple[EffectAttemptRecord, ...]:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                self._get_payload(
                    cursor,
                    table="outbox_messages",
                    id_column="outbox_message_id",
                    record_id=outbox_message_id,
                    label="outbox message",
                    decoder=decode_outbox_message,
                )
                cursor.execute(
                    """
                    SELECT payload
                    FROM waje_vnext.effect_attempts
                    WHERE outbox_message_id = %s
                    ORDER BY attempt_number
                    """,
                    (outbox_message_id,),
                )
                return tuple(
                    decode_effect_attempt(row["payload"])
                    for row in cursor.fetchall()
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
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                self._get_case(cursor, case_id, for_update=True)
                cursor.execute(
                    "SELECT clock_timestamp() AS database_now"
                )
                database_now = cursor.fetchone()["database_now"]
                cursor.execute(
                    """
                    SELECT *
                    FROM waje_vnext.controller_leases
                    WHERE case_id = %s
                    FOR UPDATE
                    """,
                    (case_id,),
                )
                row = cursor.fetchone()
                current = None if row is None else _lease_from_row(row)
                active = False if row is None else row["active"]
                if (
                    current is not None
                    and active
                    and current.expires_at > database_now
                ):
                    raise LeaseConflict(
                        "case already has an active controller lease"
                    )
                token = (
                    1
                    if current is None
                    else current.fencing_token + 1
                )
                lease = ControllerLease(
                    case_id=case_id,
                    run_id=run_id,
                    owner_id=owner_id,
                    fencing_token=token,
                    acquired_at=database_now,
                    expires_at=database_now + lease_duration,
                )
                cursor.execute(
                    """
                    INSERT INTO waje_vnext.controller_leases (
                        case_id,
                        run_id,
                        owner_id,
                        fencing_token,
                        active,
                        acquired_at,
                        expires_at
                    ) VALUES (%s, %s, %s, %s, true, %s, %s)
                    ON CONFLICT (case_id) DO UPDATE SET
                        run_id = EXCLUDED.run_id,
                        owner_id = EXCLUDED.owner_id,
                        fencing_token = EXCLUDED.fencing_token,
                        active = true,
                        acquired_at = EXCLUDED.acquired_at,
                        expires_at = EXCLUDED.expires_at
                    """,
                    (
                        lease.case_id,
                        lease.run_id,
                        lease.owner_id,
                        lease.fencing_token,
                        lease.acquired_at,
                        lease.expires_at,
                    ),
                )
                return lease

    def release_lease(self, lease: ControllerLease) -> None:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE waje_vnext.controller_leases
                    SET active = false
                    WHERE case_id = %s
                        AND run_id = %s
                        AND owner_id = %s
                        AND fencing_token = %s
                        AND active = true
                        AND acquired_at = %s
                        AND expires_at = %s
                    """,
                    (
                        lease.case_id,
                        lease.run_id,
                        lease.owner_id,
                        lease.fencing_token,
                        lease.acquired_at,
                        lease.expires_at,
                    ),
                )
                if cursor.rowcount != 1:
                    raise LeaseFenceLost(
                        "controller lease fencing token was lost"
                    )

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
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                self._get_case(cursor, case_id)
                return self._append_event(
                    cursor,
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
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                self._get_case(cursor, case_id)
                cursor.execute(
                    """
                    SELECT *
                    FROM waje_vnext.event_journal
                    WHERE case_id = %s AND cursor > %s
                    ORDER BY cursor
                    """,
                    (case_id, after_cursor),
                )
                return tuple(self._event_from_row(row) for row in cursor.fetchall())

    def _cursor(self) -> Cursor[Mapping[str, Any]]:
        return self._connection.cursor(row_factory=dict_row)

    def _get_case(
        self,
        cursor: Cursor[Mapping[str, Any]],
        case_id: str,
        *,
        for_update: bool = False,
    ) -> InvestigationCase:
        suffix = sql.SQL(" FOR UPDATE") if for_update else sql.SQL("")
        cursor.execute(
            sql.SQL(
                """
                SELECT *
                FROM waje_vnext.investigation_cases
                WHERE case_id = %s
                """
            )
            + suffix,
            (case_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise AuthorityNotFound("case {!r} does not exist".format(case_id))
        return _case_from_row(row)

    def _lock_case(
        self,
        cursor: Cursor[Mapping[str, Any]],
        case_id: str,
        expected_head_version: int,
    ) -> InvestigationCase:
        case = self._get_case(cursor, case_id, for_update=True)
        if case.head_version != expected_head_version:
            raise StaleHead(
                "expected head version {}, current is {}".format(
                    expected_head_version, case.head_version
                )
            )
        return case

    def _authority_snapshot_from_cursor(
        self,
        cursor: Cursor[Mapping[str, Any]],
        case_id: str,
    ) -> AuthoritySnapshot:
        case = self._get_case(cursor, case_id)
        cursor.execute(
            """
            SELECT authority_epoch
            FROM waje_vnext.case_mailbox_heads
            WHERE case_id = %s
            """,
            (case_id,),
        )
        mailbox = cursor.fetchone()
        if mailbox is None:
            raise AuthorityNotFound("case mailbox head does not exist")
        cursor.execute(
            """
            SELECT
                h.candidate_generation,
                h.proposed_frame_content_sha256
            FROM waje_vnext.active_frame_candidate_heads h
            WHERE h.case_id = %s
            """,
            (case_id,),
        )
        active_candidate = cursor.fetchone()
        cursor.execute(
            """
            SELECT
                (
                    SELECT count(*)
                    FROM waje_vnext.resolved_evidence_obligations
                    WHERE case_id = %s
                ) + (
                    SELECT count(*)
                    FROM waje_vnext.obligation_satisfaction_records s
                    JOIN waje_vnext.resolved_evidence_obligations o
                      ON o.obligation_id = s.obligation_id
                    WHERE o.case_id = %s
                ) AS obligation_version,
                (
                    SELECT count(*)
                    FROM waje_vnext.evidence_records
                    WHERE case_id = %s
                ) + (
                    SELECT count(*)
                    FROM waje_vnext.evidence_validity_records v
                    JOIN waje_vnext.evidence_records e
                      ON e.evidence_record_id = v.evidence_record_id
                    WHERE e.case_id = %s
                ) AS evidence_version,
                (
                    SELECT count(*)
                    FROM waje_vnext.reviewer_objections
                    WHERE case_id = %s
                ) AS contradiction_version
            """,
            (case_id, case_id, case_id, case_id, case_id),
        )
        versions = cursor.fetchone()
        return AuthoritySnapshot(
            case_id=case_id,
            head_version=case.head_version,
            mailbox_authority_epoch=mailbox["authority_epoch"],
            accepted_question_revision_id=(
                case.accepted_question_revision_id
            ),
            accepted_frame_revision_id=case.accepted_frame_revision_id,
            accepted_plan_revision_id=case.accepted_plan_revision_id,
            active_frame_candidate_generation=(
                0
                if active_candidate is None
                else active_candidate["candidate_generation"]
            ),
            active_frame_candidate_sha256=(
                None
                if active_candidate is None
                else active_candidate[
                    "proposed_frame_content_sha256"
                ]
            ),
            obligation_state_version=versions["obligation_version"],
            evidence_admission_state_version=versions["evidence_version"],
            contradiction_state_version=versions[
                "contradiction_version"
            ],
        )

    def _get_authority(
        self,
        *,
        table: str,
        id_column: str,
        record_id: str,
        label: str,
        decoder: Callable[[Mapping[str, Any]], RecordT],
    ) -> RecordT:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                return self._get_payload(
                    cursor,
                    table=table,
                    id_column=id_column,
                    record_id=record_id,
                    label=label,
                    decoder=decoder,
                )

    def _list_payloads(
        self,
        *,
        table: str,
        case_id: str,
        order_by: tuple[str, ...],
        decoder: Callable[[Mapping[str, Any]], RecordT],
    ) -> tuple[RecordT, ...]:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                self._get_case(cursor, case_id)
                cursor.execute(
                    sql.SQL(
                        "SELECT payload FROM waje_vnext.{} "
                        "WHERE case_id = %s ORDER BY {}"
                    ).format(
                        sql.Identifier(table),
                        sql.SQL(", ").join(
                            sql.Identifier(column)
                            for column in order_by
                        ),
                    ),
                    (case_id,),
                )
                return tuple(
                    decoder(row["payload"])
                    for row in cursor.fetchall()
                )

    def _get_payload(
        self,
        cursor: Cursor[Mapping[str, Any]],
        *,
        table: str,
        id_column: str,
        record_id: str,
        label: str,
        decoder: Callable[[Mapping[str, Any]], RecordT],
    ) -> RecordT:
        cursor.execute(
            sql.SQL("SELECT payload FROM waje_vnext.{} WHERE {} = %s").format(
                sql.Identifier(table), sql.Identifier(id_column)
            ),
            (record_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise AuthorityNotFound(
                "{} {!r} does not exist".format(label, record_id)
            )
        return decoder(row["payload"])

    def _get_frame(
        self,
        cursor: Cursor[Mapping[str, Any]],
        record_id: str,
    ) -> AnalysisFrameRevision:
        return self._get_payload(
            cursor,
            table="analysis_frame_revisions",
            id_column="frame_revision_id",
            record_id=record_id,
            label="frame",
            decoder=decode_frame,
        )

    def _get_question(
        self,
        cursor: Cursor[Mapping[str, Any]],
        record_id: str,
    ) -> QuestionRevision:
        return self._get_payload(
            cursor,
            table="question_revisions",
            id_column="question_revision_id",
            record_id=record_id,
            label="question",
            decoder=decode_question,
        )

    def _get_plan(
        self,
        cursor: Cursor[Mapping[str, Any]],
        record_id: str,
    ) -> WorkPlanRevision:
        return self._get_payload(
            cursor,
            table="work_plan_revisions",
            id_column="plan_revision_id",
            record_id=record_id,
            label="plan",
            decoder=decode_plan,
        )

    def _get_evidence(
        self,
        cursor: Cursor[Mapping[str, Any]],
        record_id: str,
    ) -> EvidenceRecord:
        return self._get_payload(
            cursor,
            table="evidence_records",
            id_column="evidence_record_id",
            record_id=record_id,
            label="evidence",
            decoder=decode_evidence,
        )

    def _get_answer(
        self,
        cursor: Cursor[Mapping[str, Any]],
        record_id: str,
    ) -> AnswerVersion:
        return self._get_payload(
            cursor,
            table="answer_versions",
            id_column="answer_version_id",
            record_id=record_id,
            label="answer",
            decoder=decode_answer,
        )

    def _latest_plan_for_case(
        self,
        cursor: Cursor[Mapping[str, Any]],
        case_id: str,
    ) -> WorkPlanRevision | None:
        cursor.execute(
            """
            SELECT payload
            FROM waje_vnext.work_plan_revisions
            WHERE case_id = %s
            ORDER BY revision_number DESC
            LIMIT 1
            """,
            (case_id,),
        )
        row = cursor.fetchone()
        return None if row is None else decode_plan(row["payload"])

    def _latest_answer_for_case(
        self,
        cursor: Cursor[Mapping[str, Any]],
        case_id: str,
    ) -> AnswerVersion | None:
        cursor.execute(
            """
            SELECT payload
            FROM waje_vnext.answer_versions
            WHERE case_id = %s
            ORDER BY version_number DESC
            LIMIT 1
            """,
            (case_id,),
        )
        row = cursor.fetchone()
        return None if row is None else decode_answer(row["payload"])

    def _insert_immutable(
        self,
        cursor: Cursor[Mapping[str, Any]],
        *,
        table: str,
        id_column: str,
        record_id: str,
        columns: tuple[str, ...],
        values: tuple[object, ...],
        payload: Mapping[str, Any],
        label: str,
    ) -> None:
        all_columns = (id_column,) + columns
        placeholders = sql.SQL(", ").join(sql.Placeholder() for _ in all_columns)
        cursor.execute(
            sql.SQL(
                "INSERT INTO waje_vnext.{} ({}) VALUES ({}) "
                "ON CONFLICT ({}) DO NOTHING RETURNING {}"
            ).format(
                sql.Identifier(table),
                sql.SQL(", ").join(sql.Identifier(name) for name in all_columns),
                placeholders,
                sql.Identifier(id_column),
                sql.Identifier(id_column),
            ),
            (record_id,) + values,
        )
        if cursor.fetchone() is not None:
            return
        cursor.execute(
            sql.SQL("SELECT payload FROM waje_vnext.{} WHERE {} = %s").format(
                sql.Identifier(table), sql.Identifier(id_column)
            ),
            (record_id,),
        )
        existing = cursor.fetchone()
        if existing is not None and existing["payload"] == payload:
            raise AuthorityConflict(
                "{} was already persisted under another event".format(label)
            )
        raise AuthorityConflict("{} ID already has different content".format(label))

    def _insert_idempotent_immutable(
        self,
        cursor: Cursor[Mapping[str, Any]],
        *,
        table: str,
        id_column: str,
        record_id: str,
        columns: tuple[str, ...],
        values: tuple[object, ...],
        payload: Mapping[str, Any],
        label: str,
    ) -> None:
        all_columns = (id_column,) + columns
        placeholders = sql.SQL(", ").join(
            sql.Placeholder() for _ in all_columns
        )
        cursor.execute(
            sql.SQL(
                "INSERT INTO waje_vnext.{} ({}) VALUES ({}) "
                "ON CONFLICT ({}) DO NOTHING RETURNING {}"
            ).format(
                sql.Identifier(table),
                sql.SQL(", ").join(
                    sql.Identifier(name) for name in all_columns
                ),
                placeholders,
                sql.Identifier(id_column),
                sql.Identifier(id_column),
            ),
            (record_id,) + values,
        )
        if cursor.fetchone() is not None:
            return
        cursor.execute(
            sql.SQL(
                "SELECT payload FROM waje_vnext.{} WHERE {} = %s"
            ).format(
                sql.Identifier(table),
                sql.Identifier(id_column),
            ),
            (record_id,),
        )
        existing = cursor.fetchone()
        if existing is not None and existing["payload"] == payload:
            return
        raise AuthorityConflict(
            "{} ID already has different content".format(label)
        )

    def _get_action_receipt(
        self,
        cursor: Cursor[Mapping[str, Any]],
        case_id: str,
        idempotency_key: str,
    ) -> ActionReceipt | None:
        cursor.execute(
            """
            SELECT payload
            FROM waje_vnext.action_receipts
            WHERE case_id = %s AND idempotency_key = %s
            """,
            (case_id, idempotency_key),
        )
        row = cursor.fetchone()
        return (
            None
            if row is None
            else decode_action_receipt(row["payload"])
        )

    def _require_event_cursor(
        self,
        cursor: Cursor[Mapping[str, Any]],
        case_id: str,
        event_cursor: int,
    ) -> EventJournalEntry:
        cursor.execute(
            """
            SELECT *
            FROM waje_vnext.event_journal
            WHERE case_id = %s AND cursor = %s
            """,
            (case_id, event_cursor),
        )
        row = cursor.fetchone()
        if row is None:
            raise AuthorityNotFound(
                "event cursor {} does not exist for case {!r}".format(
                    event_cursor,
                    case_id,
                )
            )
        return self._event_from_row(row)

    def _move_heads(
        self,
        cursor: Cursor[Mapping[str, Any]],
        *,
        case: InvestigationCase,
        recorded_at: datetime,
        frame_id: str | None,
        plan_id: str | None,
        answer_id: str | None,
    ) -> InvestigationCase:
        cursor.execute(
            """
            UPDATE waje_vnext.investigation_cases
            SET
                head_version = head_version + 1,
                accepted_frame_revision_id = %s,
                accepted_plan_revision_id = %s,
                accepted_answer_version_id = %s,
                updated_at = %s
            WHERE case_id = %s AND head_version = %s
            RETURNING *
            """,
            (
                frame_id,
                plan_id,
                answer_id,
                recorded_at,
                case.case_id,
                case.head_version,
            ),
        )
        row = cursor.fetchone()
        if row is None:
            raise StaleHead("case head changed during transaction")
        return _case_from_row(row)

    def _record_subordinate(
        self,
        *,
        record: RecordT,
        record_id: str,
        case_id: str,
        table: str,
        id_column: str,
        columns: tuple[str, ...],
        values: tuple[object, ...],
        event_id: str,
        event_type: JournalEventType,
        action_id: str | None,
        recorded_at: datetime,
        validator: Callable[[Cursor[Mapping[str, Any]], RecordT], object],
        label: str,
    ) -> RecordT:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                existing_event = self._event_by_id(cursor, event_id)
                if existing_event is not None:
                    if (
                        existing_event.event_type is event_type
                        and existing_event.authority_ref == record_id
                        and existing_event.case_id == case_id
                    ):
                        cursor.execute(
                            sql.SQL(
                                "SELECT payload FROM waje_vnext.{} WHERE {} = %s"
                            ).format(
                                sql.Identifier(table),
                                sql.Identifier(id_column),
                            ),
                            (record_id,),
                        )
                        existing_record = cursor.fetchone()
                        if (
                            existing_record is not None
                            and existing_record["payload"] == encode_record(record)
                        ):
                            return record
                    raise AuthorityConflict(
                        "event ID already has different content"
                    )
                validator(cursor, record)
                payload = encode_record(record)
                self._insert_immutable(
                    cursor,
                    table=table,
                    id_column=id_column,
                    record_id=record_id,
                    columns=columns,
                    values=values,
                    payload=payload,
                    label=label,
                )
                self._append_authority_event(
                    cursor,
                    case_id=case_id,
                    event_id=event_id,
                    event_type=event_type,
                    recorded_at=recorded_at,
                    action_id=action_id,
                    authority_ref=record_id,
                    payload={},
                )
                return record

    def _validate_interpretation(
        self,
        cursor: Cursor[Mapping[str, Any]],
        interpretation: InterpretationRecord,
    ) -> None:
        case = self._get_case(cursor, interpretation.case_id)
        if interpretation.frame_revision_id != case.accepted_frame_revision_id:
            raise InvalidAuthorityTransition(
                "interpretation must bind the accepted frame"
            )
        for evidence_id in interpretation.evidence_record_ids:
            evidence = self._get_evidence(cursor, evidence_id)
            if (
                evidence.case_id != interpretation.case_id
                or evidence.frame_revision_id
                != interpretation.frame_revision_id
            ):
                raise InvalidAuthorityTransition(
                    "interpretation evidence is incompatible with frame"
                )

    def _validate_objection(
        self,
        cursor: Cursor[Mapping[str, Any]],
        objection: ReviewerObjection,
    ) -> None:
        self._get_case(cursor, objection.case_id)
        answer = self._get_answer(cursor, objection.answer_version_id)
        if answer.status is not AnswerStatus.PROVISIONAL:
            raise InvalidAuthorityTransition(
                "reviewer objection must bind a provisional answer"
            )
        if objection.claim_id not in {claim.claim_id for claim in answer.claims}:
            raise InvalidAuthorityTransition(
                "reviewer objection claim is not present in answer"
            )
        cursor.execute(
            """
            SELECT payload
            FROM waje_vnext.reviewer_objections
            WHERE case_id = %s AND objection_key = %s
            ORDER BY revision_number DESC
            LIMIT 1
            """,
            (objection.case_id, objection.objection_key),
        )
        row = cursor.fetchone()
        current = None if row is None else decode_objection(row["payload"])
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

    def _idempotent_head_event(
        self,
        cursor: Cursor[Mapping[str, Any]],
        *,
        event_id: str,
        event_type: JournalEventType,
        authority_ref: str,
        case_id: str,
    ) -> InvestigationCase | None:
        existing = self._event_by_id(cursor, event_id)
        if existing is None:
            return None
        if (
            existing.event_type is event_type
            and existing.authority_ref == authority_ref
            and existing.case_id == case_id
        ):
            return self._get_case(cursor, case_id)
        raise AuthorityConflict("event ID already has different content")

    def _append_authority_event(
        self,
        cursor: Cursor[Mapping[str, Any]],
        *,
        case_id: str,
        event_id: str,
        event_type: JournalEventType,
        recorded_at: datetime,
        action_id: str | None,
        authority_ref: str,
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
        cursor.execute(
            """
            SELECT last_cursor + 1 AS next_cursor
            FROM waje_vnext.event_stream_heads
            WHERE case_id = %s
            """,
            (case_id,),
        )
        next_cursor = cursor.fetchone()["next_cursor"]
        return self._append_event(
            cursor,
            case_id=case_id,
            expected_next_cursor=next_cursor,
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

    def _append_event(
        self,
        cursor: Cursor[Mapping[str, Any]],
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
        existing = self._event_by_id(cursor, event_id)
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
        cursor.execute(
            """
            UPDATE waje_vnext.event_stream_heads
            SET last_cursor = last_cursor + 1
            WHERE case_id = %s AND last_cursor + 1 = %s
            RETURNING last_cursor
            """,
            (case_id, expected_next_cursor),
        )
        updated = cursor.fetchone()
        if updated is None:
            raise AuthorityConflict(
                "event cursor does not match expected next cursor"
            )
        entry = EventJournalEntry(
            event_id=event_id,
            case_id=case_id,
            cursor=updated["last_cursor"],
            event_type=event_type,
            recorded_at=recorded_at,
            operation=resolved_operation,
            action_id=action_id,
            authority_ref=authority_ref,
            payload=payload,
            customer_projection=customer_projection,
        )
        cursor.execute(
            """
            INSERT INTO waje_vnext.event_journal (
                case_id,
                cursor,
                event_id,
                event_type,
                recorded_at,
                operation_id,
                idempotency_key,
                causation_id,
                correlation_id,
                authority_revision,
                payload_sha256,
                action_id,
                authority_ref,
                payload,
                customer_projection
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                entry.case_id,
                entry.cursor,
                entry.event_id,
                entry.event_type.value,
                entry.recorded_at,
                entry.operation.operation_id,
                entry.operation.idempotency_key,
                entry.operation.causation_id,
                entry.operation.correlation_id,
                entry.operation.authority_revision,
                entry.operation.payload_sha256,
                entry.action_id,
                entry.authority_ref,
                Jsonb(encode_record(entry)["payload"]),
                (
                    None
                    if entry.customer_projection is None
                    else Jsonb(encode_record(entry)["customer_projection"])
                ),
            ),
        )
        return entry

    def _event_by_id(
        self,
        cursor: Cursor[Mapping[str, Any]],
        event_id: str,
    ) -> EventJournalEntry | None:
        cursor.execute(
            """
            SELECT *
            FROM waje_vnext.event_journal
            WHERE event_id = %s
            """,
            (event_id,),
        )
        row = cursor.fetchone()
        return None if row is None else self._event_from_row(row)

    @staticmethod
    def _event_from_row(row: Mapping[str, Any]) -> EventJournalEntry:
        return EventJournalEntry(
            event_id=row["event_id"],
            case_id=row["case_id"],
            cursor=row["cursor"],
            event_type=JournalEventType(row["event_type"]),
            recorded_at=row["recorded_at"],
            operation=OperationIdentity(
                operation_id=row["operation_id"],
                idempotency_key=row["idempotency_key"],
                causation_id=row["causation_id"],
                correlation_id=row["correlation_id"],
                authority_revision=row["authority_revision"],
                payload_sha256=row["payload_sha256"],
            ),
            action_id=row["action_id"],
            authority_ref=row["authority_ref"],
            payload=row["payload"],
            customer_projection=row["customer_projection"],
        )


def _case_from_row(row: Mapping[str, Any]) -> InvestigationCase:
    return InvestigationCase(
        case_id=row["case_id"],
        thread_id=row["thread_id"],
        lifecycle=CaseLifecycle(row["lifecycle"]),
        head_version=row["head_version"],
        accepted_question_revision_id=(
            row["accepted_question_revision_id"]
        ),
        accepted_frame_revision_id=row["accepted_frame_revision_id"],
        accepted_plan_revision_id=row["accepted_plan_revision_id"],
        accepted_answer_version_id=row["accepted_answer_version_id"],
        analysis_cycle_id=row["analysis_cycle_id"],
        opened_at=row["opened_at"],
        updated_at=row["updated_at"],
    )


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


def _mailbox_message_from_row(
    row: Mapping[str, Any],
) -> MailboxMessage:
    return MailboxMessage(
        message_id=row["message_id"],
        case_id=row["case_id"],
        sequence=row["sequence"],
        authority_epoch=row["authority_epoch"],
        kind=MailboxMessageKind(row["message_kind"]),
        operation=OperationIdentity(
            operation_id=row["operation_id"],
            idempotency_key=row["idempotency_key"],
            causation_id=row["causation_id"],
            correlation_id=row["correlation_id"],
            authority_revision=row["authority_revision"],
            payload_sha256=row["payload_sha256"],
        ),
        payload=row["payload"],
        created_at=row["created_at"],
    )


def _lease_from_row(row: Mapping[str, Any]) -> ControllerLease:
    return ControllerLease(
        case_id=row["case_id"],
        run_id=row["run_id"],
        owner_id=row["owner_id"],
        fencing_token=row["fencing_token"],
        acquired_at=row["acquired_at"],
        expires_at=row["expires_at"],
    )


def _job_lease_from_row(row: Mapping[str, Any]) -> JobLease:
    return JobLease(
        outbox_message_id=row["outbox_message_id"],
        owner_id=row["owner_id"],
        fencing_token=row["fencing_token"],
        acquired_at=row["acquired_at"],
        heartbeat_at=row["heartbeat_at"],
        expires_at=row["expires_at"],
    )


def _dispatcher_recovery_cursor_from_row(
    row: Mapping[str, Any],
) -> DispatcherRecoveryCursor:
    return DispatcherRecoveryCursor(
        dispatcher_id=row["dispatcher_id"],
        last_outbox_created_at=row["last_outbox_created_at"],
        last_source_event_cursor=row["last_source_event_cursor"],
        last_outbox_message_id=row["last_outbox_message_id"],
        updated_at=row["updated_at"],
    )
