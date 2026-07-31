"""Durable identities and envelopes for the case-scoped async runtime."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Mapping

from .canonical import (
    FrozenJson,
    content_sha256,
    freeze_json,
    require_aware_datetime,
    require_nonempty,
    require_sha256,
)


class MailboxMessageKind(StrEnum):
    USER_MESSAGE = "user_message"
    USER_CORRECTION = "user_correction"
    USER_CHALLENGE = "user_challenge"
    USER_SCOPE_REVISION = "user_scope_revision"


class AsyncJobKind(StrEnum):
    CONTROLLER_WAKE = "controller_wake"
    MESSAGE_BINDING = "message_binding"
    PRIMARY_AGENT = "primary_agent"
    SEMANTIC_INSPECTION = "semantic_inspection"
    DATA_PROBE = "data_probe"
    CAPABILITY = "capability"
    SENSITIVITY = "sensitivity"
    REVIEWER = "reviewer"
    OBLIGATION = "obligation"
    PROJECTION = "projection"


@dataclass(frozen=True, slots=True)
class OperationIdentity:
    operation_id: str
    idempotency_key: str
    causation_id: str
    correlation_id: str
    authority_revision: int
    payload_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "operation_id",
            "idempotency_key",
            "causation_id",
            "correlation_id",
        ):
            require_nonempty(getattr(self, name), name)
        if self.authority_revision < 0:
            raise ValueError("authority_revision must be non-negative")
        require_sha256(self.payload_sha256, "payload_sha256")


@dataclass(frozen=True, slots=True)
class MailboxMessage:
    message_id: str
    case_id: str
    sequence: int
    authority_epoch: int
    kind: MailboxMessageKind
    operation: OperationIdentity
    payload: Mapping[str, FrozenJson]
    created_at: datetime

    def __post_init__(self) -> None:
        require_nonempty(self.message_id, "message_id")
        require_nonempty(self.case_id, "case_id")
        if self.sequence < 1:
            raise ValueError("sequence must be positive")
        if self.authority_epoch < 1:
            raise ValueError("authority_epoch must be positive")
        if not isinstance(self.kind, MailboxMessageKind):
            raise TypeError("kind must be MailboxMessageKind")
        if not isinstance(self.operation, OperationIdentity):
            raise TypeError("operation must be OperationIdentity")
        require_aware_datetime(self.created_at, "created_at")
        frozen = freeze_json(self.payload)
        if not isinstance(frozen, Mapping):
            raise TypeError("payload must be a JSON object")
        if content_sha256(frozen) != self.operation.payload_sha256:
            raise ValueError("payload does not match operation payload_sha256")
        object.__setattr__(self, "payload", frozen)


@dataclass(frozen=True, slots=True)
class MailboxHead:
    case_id: str
    last_sequence: int
    authority_epoch: int
    updated_at: datetime

    def __post_init__(self) -> None:
        require_nonempty(self.case_id, "case_id")
        if self.last_sequence < 0:
            raise ValueError("last_sequence must be non-negative")
        if self.authority_epoch < 0:
            raise ValueError("authority_epoch must be non-negative")
        require_aware_datetime(self.updated_at, "updated_at")


@dataclass(frozen=True, slots=True)
class MessageIngressReceipt:
    case_id: str
    run_id: str
    message_id: str
    operation_id: str
    mailbox_sequence: int
    authority_epoch: int
    event_cursor: int

    def __post_init__(self) -> None:
        for name in ("case_id", "run_id", "message_id", "operation_id"):
            require_nonempty(getattr(self, name), name)
        for name in ("mailbox_sequence", "authority_epoch", "event_cursor"):
            if getattr(self, name) < 1:
                raise ValueError("{} must be positive".format(name))


@dataclass(frozen=True, slots=True)
class JobLease:
    outbox_message_id: str
    owner_id: str
    fencing_token: int
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        require_nonempty(self.outbox_message_id, "outbox_message_id")
        require_nonempty(self.owner_id, "owner_id")
        if self.fencing_token < 1:
            raise ValueError("fencing_token must be positive")
        for field_name in ("acquired_at", "heartbeat_at", "expires_at"):
            require_aware_datetime(getattr(self, field_name), field_name)
        if self.heartbeat_at < self.acquired_at:
            raise ValueError("heartbeat cannot precede acquisition")
        if self.expires_at <= self.heartbeat_at:
            raise ValueError("job lease expiry must follow heartbeat")


@dataclass(frozen=True, slots=True)
class AuthoritySnapshot:
    case_id: str
    head_version: int
    mailbox_authority_epoch: int
    accepted_question_revision_id: str | None
    accepted_frame_revision_id: str | None
    accepted_plan_revision_id: str | None
    active_frame_candidate_generation: int
    active_frame_candidate_sha256: str | None
    obligation_state_version: int
    evidence_admission_state_version: int
    contradiction_state_version: int

    def __post_init__(self) -> None:
        require_nonempty(self.case_id, "case_id")
        for field_name in (
            "head_version",
            "mailbox_authority_epoch",
            "active_frame_candidate_generation",
            "obligation_state_version",
            "evidence_admission_state_version",
            "contradiction_state_version",
        ):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} must be non-negative")
        for field_name in (
            "accepted_question_revision_id",
            "accepted_frame_revision_id",
            "accepted_plan_revision_id",
        ):
            value = getattr(self, field_name)
            if value is not None:
                require_nonempty(value, field_name)
        if self.active_frame_candidate_generation == 0:
            if self.active_frame_candidate_sha256 is not None:
                raise ValueError(
                    "empty candidate generation cannot carry a digest"
                )
        elif self.active_frame_candidate_sha256 is None:
            raise ValueError("active candidate generation requires a digest")
        if self.active_frame_candidate_sha256 is not None:
            require_sha256(
                self.active_frame_candidate_sha256,
                "active_frame_candidate_sha256",
            )

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)
