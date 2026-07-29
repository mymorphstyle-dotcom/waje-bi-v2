"""Append-only event journal contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Mapping

from .canonical import (
    FrozenJson,
    freeze_json,
    require_aware_datetime,
    require_nonempty,
)


class JournalEventType(StrEnum):
    CASE_OPENED = "case_opened"
    ACTION_ADMITTED = "action_admitted"
    ACTION_REJECTED = "action_rejected"
    USER_DECISION_REQUESTED = "user_decision_requested"
    FRAME_ACCEPTED = "frame_accepted"
    PLAN_ACCEPTED = "plan_accepted"
    EVIDENCE_RECORDED = "evidence_recorded"
    INTERPRETATION_RECORDED = "interpretation_recorded"
    USER_DECISION_RECORDED = "user_decision_recorded"
    REVIEWER_OBJECTION_RECORDED = "reviewer_objection_recorded"
    ANSWER_ACCEPTED = "answer_accepted"
    CHECKPOINT_RECORDED = "checkpoint_recorded"
    EFFECT_ENQUEUED = "effect_enqueued"
    EFFECT_ATTEMPT_FAILED = "effect_attempt_failed"
    EFFECT_COMPLETED = "effect_completed"
    RUN_RESUMED = "run_resumed"
    CASE_STOPPED = "case_stopped"
    CASE_CLOSED = "case_closed"


@dataclass(frozen=True, slots=True)
class EventJournalEntry:
    event_id: str
    case_id: str
    cursor: int
    event_type: JournalEventType
    recorded_at: datetime
    action_id: str | None
    authority_ref: str | None
    payload: Mapping[str, FrozenJson]
    customer_projection: Mapping[str, FrozenJson] | None

    def __post_init__(self) -> None:
        require_nonempty(self.event_id, "event_id")
        require_nonempty(self.case_id, "case_id")
        if not isinstance(self.event_type, JournalEventType):
            raise TypeError("event_type must be JournalEventType")
        if self.cursor < 1:
            raise ValueError("cursor must be positive")
        require_aware_datetime(self.recorded_at, "recorded_at")
        frozen_payload = freeze_json(self.payload)
        if not isinstance(frozen_payload, Mapping):
            raise TypeError("event payload must be a JSON object")
        object.__setattr__(self, "payload", frozen_payload)
        if self.customer_projection is not None:
            frozen_projection = freeze_json(self.customer_projection)
            if not isinstance(frozen_projection, Mapping):
                raise TypeError("customer_projection must be a JSON object")
            object.__setattr__(self, "customer_projection", frozen_projection)
