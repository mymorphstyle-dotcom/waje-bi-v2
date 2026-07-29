"""Reproducible ContextPacket projection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .authority import InvestigationCase
from .canonical import (
    content_sha256,
    require_aware_datetime,
    require_nonempty,
    require_sha256,
)


@dataclass(frozen=True, slots=True)
class ContextEvidenceItem:
    evidence_record_id: str
    evidence_type: str
    strength: str
    business_summary: str
    limitation_count: int

    def __post_init__(self) -> None:
        require_nonempty(self.evidence_record_id, "evidence_record_id")
        require_nonempty(self.evidence_type, "evidence_type")
        require_nonempty(self.strength, "strength")
        require_nonempty(self.business_summary, "business_summary")
        if self.limitation_count < 0:
            raise ValueError("limitation_count must be non-negative")


@dataclass(frozen=True, slots=True)
class ContextPacket:
    packet_id: str
    case_id: str
    head_version: int
    accepted_frame_revision_id: str | None
    accepted_plan_revision_id: str | None
    accepted_answer_version_id: str | None
    user_message: str
    relevant_event_cursor_start: int
    relevant_event_cursor_end: int
    evidence_index: tuple[ContextEvidenceItem, ...]
    unresolved_reviewer_objection_ids: tuple[str, ...]
    decision_record_ids: tuple[str, ...]
    built_at: datetime
    content_sha256: str

    def __post_init__(self) -> None:
        require_nonempty(self.packet_id, "packet_id")
        require_nonempty(self.case_id, "case_id")
        require_nonempty(self.user_message, "user_message")
        if self.head_version < 0:
            raise ValueError("head_version must be non-negative")
        if self.relevant_event_cursor_start < 0:
            raise ValueError("cursor start must be non-negative")
        if self.relevant_event_cursor_end < self.relevant_event_cursor_start:
            raise ValueError("cursor end cannot precede cursor start")
        if not isinstance(self.evidence_index, tuple):
            raise TypeError("evidence_index must be a tuple")
        if any(
            not isinstance(item, ContextEvidenceItem)
            for item in self.evidence_index
        ):
            raise TypeError(
                "evidence_index must contain ContextEvidenceItem values"
            )
        _require_string_tuple(
            self.unresolved_reviewer_objection_ids,
            "unresolved_reviewer_objection_ids",
        )
        _require_string_tuple(self.decision_record_ids, "decision_record_ids")
        require_aware_datetime(self.built_at, "built_at")
        require_sha256(self.content_sha256, "content_sha256")
        if self.content_sha256 != content_sha256(_context_content(self)):
            raise ValueError("content_sha256 does not match ContextPacket content")


def build_context_packet(
    *,
    packet_id: str,
    case: InvestigationCase,
    user_message: str,
    relevant_event_cursor_start: int,
    relevant_event_cursor_end: int,
    evidence_index: tuple[ContextEvidenceItem, ...],
    unresolved_reviewer_objection_ids: tuple[str, ...],
    decision_record_ids: tuple[str, ...],
    built_at: datetime,
) -> ContextPacket:
    content = {
        "case_id": case.case_id,
        "head_version": case.head_version,
        "accepted_frame_revision_id": case.accepted_frame_revision_id,
        "accepted_plan_revision_id": case.accepted_plan_revision_id,
        "accepted_answer_version_id": case.accepted_answer_version_id,
        "user_message": user_message,
        "relevant_event_cursor_start": relevant_event_cursor_start,
        "relevant_event_cursor_end": relevant_event_cursor_end,
        "evidence_index": evidence_index,
        "unresolved_reviewer_objection_ids": (
            unresolved_reviewer_objection_ids
        ),
        "decision_record_ids": decision_record_ids,
    }
    return ContextPacket(
        packet_id=packet_id,
        case_id=case.case_id,
        head_version=case.head_version,
        accepted_frame_revision_id=case.accepted_frame_revision_id,
        accepted_plan_revision_id=case.accepted_plan_revision_id,
        accepted_answer_version_id=case.accepted_answer_version_id,
        user_message=user_message,
        relevant_event_cursor_start=relevant_event_cursor_start,
        relevant_event_cursor_end=relevant_event_cursor_end,
        evidence_index=evidence_index,
        unresolved_reviewer_objection_ids=unresolved_reviewer_objection_ids,
        decision_record_ids=decision_record_ids,
        built_at=built_at,
        content_sha256=content_sha256(content),
    )


def _context_content(packet: ContextPacket) -> dict[str, object]:
    return {
        "case_id": packet.case_id,
        "head_version": packet.head_version,
        "accepted_frame_revision_id": packet.accepted_frame_revision_id,
        "accepted_plan_revision_id": packet.accepted_plan_revision_id,
        "accepted_answer_version_id": packet.accepted_answer_version_id,
        "user_message": packet.user_message,
        "relevant_event_cursor_start": packet.relevant_event_cursor_start,
        "relevant_event_cursor_end": packet.relevant_event_cursor_end,
        "evidence_index": packet.evidence_index,
        "unresolved_reviewer_objection_ids": (
            packet.unresolved_reviewer_objection_ids
        ),
        "decision_record_ids": packet.decision_record_ids,
    }


def _require_string_tuple(
    values: tuple[str, ...],
    field_name: str,
) -> None:
    if not isinstance(values, tuple):
        raise TypeError("{} must be a tuple".format(field_name))
    for index, value in enumerate(values):
        if not isinstance(value, str):
            raise TypeError("{}[{}] must be a string".format(field_name, index))
        require_nonempty(value, "{}[{}]".format(field_name, index))
