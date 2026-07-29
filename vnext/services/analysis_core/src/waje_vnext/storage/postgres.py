"""PostgreSQL adapter for the Gate 1 authority contract."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Mapping, TypeVar

import psycopg
from psycopg import Connection, Cursor, errors, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

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
from waje_vnext.domain.events import EventJournalEntry, JournalEventType

from .codec import (
    decode_answer,
    decode_decision,
    decode_evidence,
    decode_frame,
    decode_interpretation,
    decode_objection,
    decode_plan,
    encode_record,
)
from .ports import (
    AuthorityConflict,
    AuthorityNotFound,
    InvalidAuthorityTransition,
    StaleHead,
)


RecordT = TypeVar("RecordT")
ENVIRONMENT_VARIABLE = "WAJE_VNEXT_DATABASE_URL"


def apply_gate1_migration(
    dsn: str,
    *,
    migration_path: Path,
) -> str:
    """Apply schema v1 once and return the migration file checksum."""

    migration_bytes = migration_path.read_bytes()
    checksum = hashlib.sha256(migration_bytes).hexdigest()
    with psycopg.connect(dsn) as connection:
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('waje_vnext.schema_migrations')")
            registry = cursor.fetchone()[0]
            if registry is not None:
                cursor.execute(
                    """
                    SELECT checksum_sha256
                    FROM waje_vnext.schema_migrations
                    WHERE version = 1
                    """
                )
                existing = cursor.fetchone()
                if existing is not None:
                    if existing[0] != checksum:
                        raise AuthorityConflict(
                            "migration version 1 checksum does not match"
                        )
                    return checksum
            cursor.execute(migration_bytes.decode("utf-8"))
            cursor.execute(
                """
                INSERT INTO waje_vnext.schema_migrations (
                    version, name, checksum_sha256
                ) VALUES (1, 'gate1_authority', %s)
                """,
                (checksum,),
            )
    return checksum


class PostgresAuthorityStore:
    """Transactional PostgreSQL implementation of the authority storage port."""

    def __init__(self, connection: Connection[Any]) -> None:
        self._connection = connection
        self._lock = RLock()

    @classmethod
    def connect(cls, dsn: str) -> "PostgresAuthorityStore":
        return cls(psycopg.connect(dsn))

    @classmethod
    def from_env(cls) -> "PostgresAuthorityStore":
        dsn = os.environ.get(ENVIRONMENT_VARIABLE)
        if not dsn:
            raise RuntimeError("{} is required".format(ENVIRONMENT_VARIABLE))
        return cls.connect(dsn)

    def close(self) -> None:
        self._connection.close()

    def open_case(
        self,
        *,
        case_id: str,
        thread_id: str,
        event_id: str,
        opened_at: datetime,
    ) -> InvestigationCase:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                existing = self._event_by_id(cursor, event_id)
                if existing is not None:
                    if (
                        existing.event_type is JournalEventType.CASE_OPENED
                        and existing.case_id == case_id
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
                            opened_at,
                            updated_at
                        ) VALUES (%s, %s, 'open', 0, %s, %s)
                        """,
                        (case_id, thread_id, opened_at, opened_at),
                    )
                    cursor.execute(
                        """
                        INSERT INTO waje_vnext.event_stream_heads (
                            case_id, last_cursor
                        ) VALUES (%s, 0)
                        """,
                        (case_id,),
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
                )
                return self._get_case(cursor, case_id)

    def get_case(self, case_id: str) -> InvestigationCase:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
                return self._get_case(cursor, case_id)

    def get_frame(self, frame_revision_id: str) -> AnalysisFrameRevision:
        return self._get_authority(
            table="analysis_frame_revisions",
            id_column="frame_revision_id",
            record_id=frame_revision_id,
            label="frame",
            decoder=decode_frame,
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

    def accept_frame(
        self,
        frame: AnalysisFrameRevision,
        *,
        expected_head_version: int,
        event_id: str,
        recorded_at: datetime,
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
                payload = encode_record(frame)
                self._insert_immutable(
                    cursor,
                    table="analysis_frame_revisions",
                    id_column="frame_revision_id",
                    record_id=frame.frame_revision_id,
                    columns=(
                        "case_id",
                        "revision_number",
                        "prior_frame_revision_id",
                        "content_sha256",
                        "payload",
                        "created_at",
                    ),
                    values=(
                        frame.case_id,
                        frame.revision_number,
                        frame.prior_frame_revision_id,
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
                )
                return updated

    def accept_plan(
        self,
        plan: WorkPlanRevision,
        *,
        expected_head_version: int,
        event_id: str,
        recorded_at: datetime,
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
    ) -> InvestigationCase:
        with self._lock, self._connection.transaction():
            with self._cursor() as cursor:
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
                )
                return updated

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
            self._get_evidence(cursor, evidence_id)

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
    ) -> EventJournalEntry:
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
    ) -> EventJournalEntry:
        existing = self._event_by_id(cursor, event_id)
        if existing is not None:
            candidate = EventJournalEntry(
                event_id=event_id,
                case_id=case_id,
                cursor=existing.cursor,
                event_type=event_type,
                recorded_at=recorded_at,
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
                action_id,
                authority_ref,
                payload,
                customer_projection
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                entry.case_id,
                entry.cursor,
                entry.event_id,
                entry.event_type.value,
                entry.recorded_at,
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
        accepted_frame_revision_id=row["accepted_frame_revision_id"],
        accepted_plan_revision_id=row["accepted_plan_revision_id"],
        accepted_answer_version_id=row["accepted_answer_version_id"],
        opened_at=row["opened_at"],
        updated_at=row["updated_at"],
    )
