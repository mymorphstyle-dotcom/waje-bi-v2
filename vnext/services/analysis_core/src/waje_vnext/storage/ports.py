"""Storage port for the Gate 1 authority contract."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from waje_vnext.domain.authority import (
    AnalysisFrameRevision,
    AnswerVersion,
    DecisionRecord,
    EvidenceRecord,
    InvestigationCase,
    InterpretationRecord,
    ReviewerObjection,
    WorkPlanRevision,
)
from waje_vnext.domain.events import EventJournalEntry, JournalEventType


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


class AuthorityStore(Protocol):
    def open_case(
        self,
        *,
        case_id: str,
        thread_id: str,
        event_id: str,
        opened_at: datetime,
    ) -> InvestigationCase: ...

    def get_case(self, case_id: str) -> InvestigationCase: ...

    def get_frame(self, frame_revision_id: str) -> AnalysisFrameRevision: ...

    def get_plan(self, plan_revision_id: str) -> WorkPlanRevision: ...

    def get_evidence(self, evidence_record_id: str) -> EvidenceRecord: ...

    def get_answer(self, answer_version_id: str) -> AnswerVersion: ...

    def accept_frame(
        self,
        frame: AnalysisFrameRevision,
        *,
        expected_head_version: int,
        event_id: str,
        recorded_at: datetime,
    ) -> InvestigationCase: ...

    def accept_plan(
        self,
        plan: WorkPlanRevision,
        *,
        expected_head_version: int,
        event_id: str,
        recorded_at: datetime,
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
    ) -> EventJournalEntry: ...

    def list_events(
        self,
        case_id: str,
        *,
        after_cursor: int = 0,
    ) -> tuple[EventJournalEntry, ...]: ...
