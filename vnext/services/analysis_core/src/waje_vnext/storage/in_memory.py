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
from waje_vnext.domain.runtime_state import (
    ActionReceipt,
    CheckpointRecord,
    OutboxMessage,
)

from .ports import (
    AuthorityConflict,
    AuthorityNotFound,
    InvalidAuthorityTransition,
    LeaseConflict,
    LeaseFenceLost,
    StaleHead,
)


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
        self._actions: dict[str, PersistedAction] = {}
        self._action_idempotency_keys: dict[tuple[str, str], str] = {}
        self._contexts: dict[str, ContextPacket] = {}
        self._receipts: dict[tuple[str, str], ActionReceipt] = {}
        self._receipt_action_ids: dict[str, tuple[str, str]] = {}
        self._checkpoints: dict[str, CheckpointRecord] = {}
        self._checkpoint_event_keys: dict[tuple[str, int], str] = {}
        self._outbox: dict[str, OutboxMessage] = {}
        self._decision_requests: dict[str, UserDecisionRequest] = {}
        self._decision_request_action_keys: dict[tuple[str, str], str] = {}
        self._effect_attempts: dict[str, EffectAttemptRecord] = {}
        self._leases: dict[str, ControllerLease] = {}
        self._lease_tokens: dict[str, int] = {}
        self._job_leases: dict[str, JobLease] = {}
        self._job_lease_tokens: dict[str, int] = {}
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
                accepted_frame_revision_id=None,
                accepted_plan_revision_id=None,
                accepted_answer_version_id=None,
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
            for decision_id in frame.decision_record_ids:
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

    def transition_case_lifecycle(
        self,
        *,
        case_id: str,
        lifecycle: CaseLifecycle,
        expected_head_version: int,
        event_id: str,
        action_id: str,
        recorded_at: datetime,
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
                if action.action.kind not in _EFFECT_ACTION_KINDS:
                    raise InvalidAuthorityTransition(
                        "outbox requires an effect action"
                    )
                if message.job_kind is not _ACTION_JOB_KINDS[
                    action.action.kind
                ]:
                    raise InvalidAuthorityTransition(
                        "outbox job kind does not match action"
                    )
                if (
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
        operation: OperationIdentity | None = None,
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

    def _snapshot(self) -> dict[str, object]:
        return {
            "_cases": self._cases.copy(),
            "_frames": self._frames.copy(),
            "_plans": self._plans.copy(),
            "_evidence": self._evidence.copy(),
            "_answers": self._answers.copy(),
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
            "_decision_requests": self._decision_requests.copy(),
            "_decision_request_action_keys": (
                self._decision_request_action_keys.copy()
            ),
            "_effect_attempts": self._effect_attempts.copy(),
            "_leases": self._leases.copy(),
            "_lease_tokens": self._lease_tokens.copy(),
            "_job_leases": self._job_leases.copy(),
            "_job_lease_tokens": self._job_lease_tokens.copy(),
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
