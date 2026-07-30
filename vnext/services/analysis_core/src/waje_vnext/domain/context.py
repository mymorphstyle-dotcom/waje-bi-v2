"""Reproducible, bounded business context for Primary Agent turns."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from .authority import (
    AnalysisFrameRevision,
    AnswerVersion,
    DecisionRecord,
    EvidenceRecord,
    InvestigationCase,
    ReviewerObjection,
    WorkPlanRevision,
)
from .canonical import (
    FrozenJson,
    content_sha256,
    freeze_json,
    require_aware_datetime,
    require_nonempty,
    require_sha256,
    to_jsonable,
)
from .events import EventJournalEntry


MAX_CONTEXT_EVENTS = 100
MAX_CONTEXT_EVIDENCE = 200
MAX_CONTEXT_DECISIONS = 100
MAX_CONTEXT_OBJECTIONS = 100


@dataclass(frozen=True, slots=True)
class ContextEvidenceItem:
    evidence_record_id: str
    evidence_type: str
    strength: str
    business_summary: str
    limitation_count: int
    frame_revision_id: str
    plan_revision_id: str
    task_id: str
    snapshot_release_ref: str
    grain: str
    capability_name: str
    inline_payload: Mapping[str, FrozenJson] | None
    result_handle_id: str | None

    def __post_init__(self) -> None:
        for name in (
            "evidence_record_id",
            "evidence_type",
            "strength",
            "business_summary",
            "frame_revision_id",
            "plan_revision_id",
            "task_id",
            "snapshot_release_ref",
            "grain",
            "capability_name",
        ):
            require_nonempty(getattr(self, name), name)
        if self.limitation_count < 0:
            raise ValueError("limitation_count must be non-negative")
        if (self.inline_payload is None) == (self.result_handle_id is None):
            raise ValueError(
                "context evidence requires payload or result handle"
            )
        if self.inline_payload is not None:
            frozen = _freeze_object(self.inline_payload, "inline_payload")
            object.__setattr__(self, "inline_payload", frozen)
        if self.result_handle_id is not None:
            require_nonempty(self.result_handle_id, "result_handle_id")

    @classmethod
    def from_record(cls, evidence: EvidenceRecord) -> "ContextEvidenceItem":
        return cls(
            evidence_record_id=evidence.evidence_record_id,
            evidence_type=evidence.evidence_type.value,
            strength=evidence.strength.value,
            business_summary=evidence.business_summary,
            limitation_count=len(evidence.limitations),
            frame_revision_id=evidence.frame_revision_id,
            plan_revision_id=evidence.plan_revision_id,
            task_id=evidence.task_id,
            snapshot_release_ref=evidence.snapshot_release_ref,
            grain=evidence.grain,
            capability_name=evidence.capability_name,
            inline_payload=evidence.inline_payload,
            result_handle_id=(
                None
                if evidence.result_handle is None
                else evidence.result_handle.handle_id
            ),
        )


@dataclass(frozen=True, slots=True)
class ContextEventItem:
    cursor: int
    event_type: str
    authority_ref: str | None
    business_projection: Mapping[str, FrozenJson]
    agent_result: Mapping[str, FrozenJson] | None = None

    def __post_init__(self) -> None:
        if self.cursor < 1:
            raise ValueError("event cursor must be positive")
        require_nonempty(self.event_type, "event_type")
        if self.authority_ref is not None:
            require_nonempty(self.authority_ref, "authority_ref")
        frozen = _freeze_object(
            self.business_projection,
            "business_projection",
        )
        object.__setattr__(self, "business_projection", frozen)
        if self.agent_result is not None:
            agent_result = _freeze_object(
                self.agent_result,
                "agent_result",
            )
            object.__setattr__(self, "agent_result", agent_result)

    @classmethod
    def from_event(
        cls,
        event: EventJournalEntry,
        *,
        agent_result: Mapping[str, FrozenJson] | None = None,
    ) -> "ContextEventItem":
        return cls(
            cursor=event.cursor,
            event_type=event.event_type.value,
            authority_ref=event.authority_ref,
            business_projection=event.customer_projection or {},
            agent_result=agent_result,
        )


@dataclass(frozen=True, slots=True)
class ContextDecisionItem:
    decision_record_id: str
    question: str
    selected_option_id: str | None
    freeform_response: str | None
    source: str

    def __post_init__(self) -> None:
        for name in ("decision_record_id", "question", "source"):
            require_nonempty(getattr(self, name), name)
        if (self.selected_option_id is None) == (self.freeform_response is None):
            raise ValueError(
                "context decision requires one selected value"
            )
        if self.selected_option_id is not None:
            require_nonempty(self.selected_option_id, "selected_option_id")
        if self.freeform_response is not None:
            require_nonempty(self.freeform_response, "freeform_response")

    @classmethod
    def from_record(cls, decision: DecisionRecord) -> "ContextDecisionItem":
        return cls(
            decision_record_id=decision.decision_record_id,
            question=decision.question,
            selected_option_id=decision.selected_option_id,
            freeform_response=decision.freeform_response,
            source=decision.source,
        )


@dataclass(frozen=True, slots=True)
class ContextReviewerObjectionItem:
    objection_id: str
    objection_key: str
    revision_number: int
    answer_version_id: str
    claim_id: str
    severity: str
    status: str
    risk_type: str
    evidence_gap: str
    requested_action: str
    disposition_note: str | None

    def __post_init__(self) -> None:
        for name in (
            "objection_id",
            "objection_key",
            "answer_version_id",
            "claim_id",
            "severity",
            "status",
            "risk_type",
            "evidence_gap",
            "requested_action",
        ):
            require_nonempty(getattr(self, name), name)
        if self.revision_number < 1:
            raise ValueError("revision_number must be positive")
        if self.disposition_note is not None:
            require_nonempty(self.disposition_note, "disposition_note")

    @classmethod
    def from_record(
        cls,
        objection: ReviewerObjection,
    ) -> "ContextReviewerObjectionItem":
        return cls(
            objection_id=objection.objection_id,
            objection_key=objection.objection_key,
            revision_number=objection.revision_number,
            answer_version_id=objection.answer_version_id,
            claim_id=objection.claim_id,
            severity=objection.severity.value,
            status=objection.status.value,
            risk_type=objection.risk_type,
            evidence_gap=objection.evidence_gap,
            requested_action=objection.requested_action,
            disposition_note=objection.disposition_note,
        )


@dataclass(frozen=True, slots=True)
class ContextPacket:
    packet_id: str
    case_id: str
    head_version: int
    accepted_frame_revision_id: str | None
    accepted_plan_revision_id: str | None
    accepted_answer_version_id: str | None
    accepted_frame_payload: Mapping[str, FrozenJson] | None
    accepted_plan_payload: Mapping[str, FrozenJson] | None
    accepted_answer_payload: Mapping[str, FrozenJson] | None
    user_message: str
    relevant_event_cursor_start: int
    relevant_event_cursor_end: int
    recent_events: tuple[ContextEventItem, ...]
    evidence_index: tuple[ContextEvidenceItem, ...]
    decision_index: tuple[ContextDecisionItem, ...]
    reviewer_objection_index: tuple[ContextReviewerObjectionItem, ...]
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
        _validate_typed_index(
            self.recent_events,
            ContextEventItem,
            "recent_events",
            MAX_CONTEXT_EVENTS,
        )
        _validate_typed_index(
            self.evidence_index,
            ContextEvidenceItem,
            "evidence_index",
            MAX_CONTEXT_EVIDENCE,
        )
        _validate_typed_index(
            self.decision_index,
            ContextDecisionItem,
            "decision_index",
            MAX_CONTEXT_DECISIONS,
        )
        _validate_typed_index(
            self.reviewer_objection_index,
            ContextReviewerObjectionItem,
            "reviewer_objection_index",
            MAX_CONTEXT_OBJECTIONS,
        )
        _validate_event_window(self)
        _freeze_authority_payloads(self)
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
    accepted_frame: AnalysisFrameRevision | None,
    accepted_plan: WorkPlanRevision | None,
    accepted_answer: AnswerVersion | None,
    recent_events: tuple[ContextEventItem, ...],
    evidence_index: tuple[ContextEvidenceItem, ...],
    decision_index: tuple[ContextDecisionItem, ...],
    reviewer_objection_index: tuple[ContextReviewerObjectionItem, ...],
    built_at: datetime,
) -> ContextPacket:
    frame_payload = _record_payload(accepted_frame)
    plan_payload = _record_payload(accepted_plan)
    answer_payload = _record_payload(accepted_answer)
    content = {
        "case_id": case.case_id,
        "head_version": case.head_version,
        "accepted_frame_revision_id": case.accepted_frame_revision_id,
        "accepted_plan_revision_id": case.accepted_plan_revision_id,
        "accepted_answer_version_id": case.accepted_answer_version_id,
        "accepted_frame_payload": frame_payload,
        "accepted_plan_payload": plan_payload,
        "accepted_answer_payload": answer_payload,
        "user_message": user_message,
        "relevant_event_cursor_start": relevant_event_cursor_start,
        "relevant_event_cursor_end": relevant_event_cursor_end,
        "recent_events": recent_events,
        "evidence_index": evidence_index,
        "decision_index": decision_index,
        "reviewer_objection_index": reviewer_objection_index,
    }
    return ContextPacket(
        packet_id=packet_id,
        case_id=case.case_id,
        head_version=case.head_version,
        accepted_frame_revision_id=case.accepted_frame_revision_id,
        accepted_plan_revision_id=case.accepted_plan_revision_id,
        accepted_answer_version_id=case.accepted_answer_version_id,
        accepted_frame_payload=frame_payload,
        accepted_plan_payload=plan_payload,
        accepted_answer_payload=answer_payload,
        user_message=user_message,
        relevant_event_cursor_start=relevant_event_cursor_start,
        relevant_event_cursor_end=relevant_event_cursor_end,
        recent_events=recent_events,
        evidence_index=evidence_index,
        decision_index=decision_index,
        reviewer_objection_index=reviewer_objection_index,
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
        "accepted_frame_payload": packet.accepted_frame_payload,
        "accepted_plan_payload": packet.accepted_plan_payload,
        "accepted_answer_payload": packet.accepted_answer_payload,
        "user_message": packet.user_message,
        "relevant_event_cursor_start": packet.relevant_event_cursor_start,
        "relevant_event_cursor_end": packet.relevant_event_cursor_end,
        "recent_events": packet.recent_events,
        "evidence_index": packet.evidence_index,
        "decision_index": packet.decision_index,
        "reviewer_objection_index": packet.reviewer_objection_index,
    }


def _record_payload(value: object | None) -> Mapping[str, FrozenJson] | None:
    if value is None:
        return None
    return _freeze_object(to_jsonable(value), "authority payload")


def _freeze_object(
    value: Mapping[str, FrozenJson],
    field_name: str,
) -> Mapping[str, FrozenJson]:
    frozen = freeze_json(value)
    if not isinstance(frozen, Mapping):
        raise TypeError("{} must be a JSON object".format(field_name))
    return frozen


def _freeze_authority_payloads(packet: ContextPacket) -> None:
    bindings = (
        (
            "accepted_frame_revision_id",
            "accepted_frame_payload",
            "frame_revision_id",
        ),
        (
            "accepted_plan_revision_id",
            "accepted_plan_payload",
            "plan_revision_id",
        ),
        (
            "accepted_answer_version_id",
            "accepted_answer_payload",
            "answer_version_id",
        ),
    )
    for id_field, payload_field, payload_id_field in bindings:
        record_id = getattr(packet, id_field)
        payload = getattr(packet, payload_field)
        if (record_id is None) != (payload is None):
            raise ValueError(
                "{} and {} must be present together".format(
                    id_field,
                    payload_field,
                )
            )
        if payload is None:
            continue
        frozen = _freeze_object(payload, payload_field)
        if frozen.get(payload_id_field) != record_id:
            raise ValueError("{} ID does not match case head".format(payload_field))
        if frozen.get("case_id") != packet.case_id:
            raise ValueError("{} case does not match packet".format(payload_field))
        object.__setattr__(packet, payload_field, frozen)


def _validate_typed_index(
    values: tuple[object, ...],
    expected_type: type[object],
    field_name: str,
    maximum: int,
) -> None:
    if not isinstance(values, tuple):
        raise TypeError("{} must be a tuple".format(field_name))
    if len(values) > maximum:
        raise ValueError(
            "{} exceeds maximum size {}".format(field_name, maximum)
        )
    for index, value in enumerate(values):
        if not isinstance(value, expected_type):
            raise TypeError(
                "{}[{}] must be {}".format(
                    field_name,
                    index,
                    expected_type.__name__,
                )
            )


def _validate_event_window(packet: ContextPacket) -> None:
    cursors = tuple(event.cursor for event in packet.recent_events)
    if cursors != tuple(sorted(set(cursors))):
        raise ValueError("recent event cursors must be unique and increasing")
    if not cursors:
        return
    if cursors[0] < packet.relevant_event_cursor_start:
        raise ValueError("recent event precedes declared cursor window")
    if cursors[-1] > packet.relevant_event_cursor_end:
        raise ValueError("recent event exceeds declared cursor window")
