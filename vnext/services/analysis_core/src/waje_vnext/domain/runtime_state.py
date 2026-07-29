"""Immutable runtime envelopes used at controller persistence boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from .canonical import (
    FrozenJson,
    content_sha256,
    freeze_json,
    require_aware_datetime,
    require_nonempty,
    require_sha256,
)


@dataclass(frozen=True, slots=True)
class ActionReceipt:
    case_id: str
    idempotency_key: str
    action_id: str
    request_sha256: str
    result_schema_ref: str
    result_payload: Mapping[str, FrozenJson]
    result_sha256: str
    event_cursor: int
    recorded_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "case_id",
            "idempotency_key",
            "action_id",
            "result_schema_ref",
        ):
            require_nonempty(getattr(self, name), name)
        require_sha256(self.request_sha256, "request_sha256")
        require_sha256(self.result_sha256, "result_sha256")
        if self.event_cursor < 1:
            raise ValueError("event_cursor must be positive")
        require_aware_datetime(self.recorded_at, "recorded_at")
        frozen = _freeze_object(self.result_payload, "result_payload")
        if content_sha256(frozen) != self.result_sha256:
            raise ValueError("result_payload does not match result_sha256")
        object.__setattr__(self, "result_payload", frozen)


@dataclass(frozen=True, slots=True)
class CheckpointRecord:
    checkpoint_id: str
    case_id: str
    head_version: int
    event_cursor: int
    context_packet_id: str
    context_sha256: str
    state_schema_ref: str
    state_payload: Mapping[str, FrozenJson]
    state_sha256: str
    created_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "checkpoint_id",
            "case_id",
            "context_packet_id",
            "state_schema_ref",
        ):
            require_nonempty(getattr(self, name), name)
        if self.head_version < 0:
            raise ValueError("head_version must be non-negative")
        if self.event_cursor < 1:
            raise ValueError("event_cursor must be positive")
        require_sha256(self.context_sha256, "context_sha256")
        require_sha256(self.state_sha256, "state_sha256")
        require_aware_datetime(self.created_at, "created_at")
        frozen = _freeze_object(self.state_payload, "state_payload")
        if content_sha256(frozen) != self.state_sha256:
            raise ValueError("state_payload does not match state_sha256")
        object.__setattr__(self, "state_payload", frozen)


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    outbox_message_id: str
    case_id: str
    source_event_cursor: int
    action_id: str | None
    idempotency_key: str
    destination: str
    contract_ref: str
    payload: Mapping[str, FrozenJson]
    payload_sha256: str
    created_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "outbox_message_id",
            "case_id",
            "idempotency_key",
            "destination",
            "contract_ref",
        ):
            require_nonempty(getattr(self, name), name)
        if self.action_id is not None:
            require_nonempty(self.action_id, "action_id")
        if self.source_event_cursor < 1:
            raise ValueError("source_event_cursor must be positive")
        require_sha256(self.payload_sha256, "payload_sha256")
        require_aware_datetime(self.created_at, "created_at")
        frozen = _freeze_object(self.payload, "payload")
        if content_sha256(frozen) != self.payload_sha256:
            raise ValueError("payload does not match payload_sha256")
        object.__setattr__(self, "payload", frozen)


def _freeze_object(
    value: Mapping[str, FrozenJson],
    field_name: str,
) -> Mapping[str, FrozenJson]:
    frozen = freeze_json(value)
    if not isinstance(frozen, Mapping):
        raise TypeError("{} must be a JSON object".format(field_name))
    return frozen
