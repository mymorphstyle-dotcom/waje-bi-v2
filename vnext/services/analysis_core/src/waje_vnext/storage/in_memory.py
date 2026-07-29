"""In-memory conformance adapter for the authority storage contract."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from threading import RLock
from typing import Any, TypeVar

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

from .ports import (
    AuthorityConflict,
    AuthorityNotFound,
    InvalidAuthorityTransition,
    StaleHead,
)


RecordT = TypeVar("RecordT")


class InMemoryAuthorityStore:
    """Reference adapter used by contract tests and deterministic runtime tests."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._cases: dict[str, InvestigationCase] = {}
        self._frames: dict[str, AnalysisFrameRevision] = {}
        self._plans: dict[str, WorkPlanRevision] = {}
        self._evidence: dict[str, EvidenceRecord] = {}
        self._answers: dict[str, AnswerVersion] = {}
        self._interpretations: dict[str, InterpretationRecord] = {}
        self._decisions: dict[str, DecisionRecord] = {}
        self._objections: dict[str, ReviewerObjection] = {}
        self._events: dict[str, list[EventJournalEntry]] = {}
        self._events_by_id: dict[str, EventJournalEntry] = {}

    def open_case(
        self,
        *,
        case_id: str,
        thread_id: str,
        event_id: str,
        opened_at: datetime,
    ) -> InvestigationCase:
        with self._lock:
            existing_event = self._events_by_id.get(event_id)
            if existing_event is not None:
                if (
                    existing_event.event_type is JournalEventType.CASE_OPENED
                    and existing_event.case_id == case_id
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
                accepted_frame_revision_id=None,
                accepted_plan_revision_id=None,
                accepted_answer_version_id=None,
                opened_at=opened_at,
                updated_at=opened_at,
            )
            self._cases[case_id] = case
            self._events[case_id] = []
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
            )
            return case

    def get_case(self, case_id: str) -> InvestigationCase:
        with self._lock:
            return _get(self._cases, case_id, "case")

    def get_frame(self, frame_revision_id: str) -> AnalysisFrameRevision:
        with self._lock:
            return _get(self._frames, frame_revision_id, "frame")

    def get_plan(self, plan_revision_id: str) -> WorkPlanRevision:
        with self._lock:
            return _get(self._plans, plan_revision_id, "plan")

    def get_evidence(self, evidence_record_id: str) -> EvidenceRecord:
        with self._lock:
            return _get(self._evidence, evidence_record_id, "evidence")

    def get_answer(self, answer_version_id: str) -> AnswerVersion:
        with self._lock:
            return _get(self._answers, answer_version_id, "answer")

    def accept_frame(
        self,
        frame: AnalysisFrameRevision,
        *,
        expected_head_version: int,
        event_id: str,
        recorded_at: datetime,
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
    ) -> InvestigationCase:
        with self._lock:
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
                self.get_evidence(evidence_id)
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
    ) -> EventJournalEntry:
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
    ) -> EventJournalEntry:
        existing = self._events_by_id.get(event_id)
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
            action_id=action_id,
            authority_ref=authority_ref,
            payload=payload,
            customer_projection=customer_projection,
        )
        self._events[case_id].append(entry)
        self._events_by_id[event_id] = entry
        return entry


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
