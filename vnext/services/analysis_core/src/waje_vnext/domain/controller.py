"""Typed state owned by the WAJE vNext controller."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Mapping

from .actions import ActionEnvelope, ActionKind, AgentActionProposal
from .authority import DecisionOption
from .canonical import (
    FrozenJson,
    content_sha256,
    freeze_json,
    require_aware_datetime,
    require_nonempty,
    require_sha256,
)
from .context import ContextPacket


class ControllerPhase(StrEnum):
    READY_FOR_AGENT = "ready_for_agent"
    WAITING_FOR_USER = "waiting_for_user"
    WAITING_FOR_EFFECT = "waiting_for_effect"
    COMPLETED = "completed"
    STOPPED = "stopped"


class EffectAttemptStatus(StrEnum):
    SUCCEEDED = "succeeded"
    RETRYABLE_FAILURE = "retryable_failure"
    TERMINAL_FAILURE = "terminal_failure"


@dataclass(frozen=True, slots=True)
class ControllerState:
    run_id: str
    case_id: str
    phase: ControllerPhase
    step_number: int
    head_version: int
    last_event_cursor: int
    context_packet_id: str
    latest_user_message: str
    pending_action_id: str | None
    pending_outbox_message_id: str | None
    pending_decision_request_id: str | None
    accepted_answer_version_id: str | None
    consecutive_rejections: int
    updated_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "run_id",
            "case_id",
            "context_packet_id",
            "latest_user_message",
        ):
            require_nonempty(getattr(self, name), name)
        if not isinstance(self.phase, ControllerPhase):
            raise TypeError("phase must be ControllerPhase")
        if self.step_number < 0:
            raise ValueError("step_number must be non-negative")
        if self.head_version < 0:
            raise ValueError("head_version must be non-negative")
        if self.last_event_cursor < 1:
            raise ValueError("last_event_cursor must be positive")
        if self.consecutive_rejections < 0:
            raise ValueError("consecutive_rejections must be non-negative")
        for field_name in (
            "pending_action_id",
            "pending_outbox_message_id",
            "pending_decision_request_id",
            "accepted_answer_version_id",
        ):
            value = getattr(self, field_name)
            if value is not None:
                require_nonempty(value, field_name)
        require_aware_datetime(self.updated_at, "updated_at")
        _validate_phase_bindings(self)

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)


@dataclass(frozen=True, slots=True)
class PrimaryAgentRequest:
    turn_id: str
    run_id: str
    context_packet: ContextPacket
    allowed_actions: tuple[ActionKind, ...]
    action_contract_ref: str
    requested_at: datetime

    def __post_init__(self) -> None:
        for name in ("turn_id", "run_id", "action_contract_ref"):
            require_nonempty(getattr(self, name), name)
        if not isinstance(self.context_packet, ContextPacket):
            raise TypeError("context_packet must be ContextPacket")
        if not isinstance(self.allowed_actions, tuple):
            raise TypeError("allowed_actions must be a tuple")
        if not self.allowed_actions:
            raise ValueError("allowed_actions cannot be empty")
        if any(
            not isinstance(action, ActionKind)
            for action in self.allowed_actions
        ):
            raise TypeError("allowed_actions must contain ActionKind values")
        if len(self.allowed_actions) != len(set(self.allowed_actions)):
            raise ValueError("allowed_actions must be unique")
        require_aware_datetime(self.requested_at, "requested_at")


@dataclass(frozen=True, slots=True)
class UserDecisionRequest:
    decision_request_id: str
    case_id: str
    action_id: str
    question: str
    options: tuple[DecisionOption, ...]
    recommended_option_id: str
    allow_freeform: bool
    requested_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "decision_request_id",
            "case_id",
            "action_id",
            "question",
            "recommended_option_id",
        ):
            require_nonempty(getattr(self, name), name)
        if not isinstance(self.options, tuple):
            raise TypeError("options must be a tuple")
        if not 2 <= len(self.options) <= 3:
            raise ValueError("decision request requires two or three options")
        if any(not isinstance(option, DecisionOption) for option in self.options):
            raise TypeError("options must contain DecisionOption values")
        option_ids = tuple(option.option_id for option in self.options)
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("decision request option IDs must be unique")
        if self.recommended_option_id not in option_ids:
            raise ValueError("recommended option must be present")
        if not self.allow_freeform:
            raise ValueError("decision request requires freeform correction")
        require_aware_datetime(self.requested_at, "requested_at")


@dataclass(frozen=True, slots=True)
class EffectAttemptRecord:
    effect_attempt_id: str
    outbox_message_id: str
    case_id: str
    attempt_number: int
    prior_attempt_id: str | None
    status: EffectAttemptStatus
    result_payload: Mapping[str, FrozenJson] | None
    result_sha256: str | None
    error_code: str | None
    error_message: str | None
    started_at: datetime
    completed_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "effect_attempt_id",
            "outbox_message_id",
            "case_id",
        ):
            require_nonempty(getattr(self, name), name)
        if self.attempt_number < 1:
            raise ValueError("attempt_number must be positive")
        if self.attempt_number == 1 and self.prior_attempt_id is not None:
            raise ValueError("first effect attempt cannot have a prior attempt")
        if self.attempt_number > 1 and not self.prior_attempt_id:
            raise ValueError("later effect attempt requires prior_attempt_id")
        if not isinstance(self.status, EffectAttemptStatus):
            raise TypeError("status must be EffectAttemptStatus")
        require_aware_datetime(self.started_at, "started_at")
        require_aware_datetime(self.completed_at, "completed_at")
        if self.completed_at < self.started_at:
            raise ValueError("completed_at cannot precede started_at")
        _validate_effect_result(self)


@dataclass(frozen=True, slots=True)
class ControllerLease:
    case_id: str
    run_id: str
    owner_id: str
    fencing_token: int
    acquired_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        for name in ("case_id", "run_id", "owner_id"):
            require_nonempty(getattr(self, name), name)
        if self.fencing_token < 1:
            raise ValueError("fencing_token must be positive")
        require_aware_datetime(self.acquired_at, "acquired_at")
        require_aware_datetime(self.expires_at, "expires_at")
        if self.expires_at <= self.acquired_at:
            raise ValueError("lease expiry must follow acquisition")


@dataclass(frozen=True, slots=True)
class PersistedAction:
    action: ActionEnvelope
    proposal_sha256: str
    recorded_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.action, ActionEnvelope):
            raise TypeError("action must be ActionEnvelope")
        require_sha256(self.proposal_sha256, "proposal_sha256")
        proposal = AgentActionProposal(
            kind=self.action.kind,
            payload=self.action.payload,
        )
        if proposal.content_sha256 != self.proposal_sha256:
            raise ValueError(
                "proposal_sha256 does not match the action business proposal"
            )
        require_aware_datetime(self.recorded_at, "recorded_at")


def _validate_phase_bindings(state: ControllerState) -> None:
    if state.phase is ControllerPhase.WAITING_FOR_USER:
        if not state.pending_decision_request_id:
            raise ValueError("waiting_for_user requires a decision request")
        if state.pending_outbox_message_id is not None:
            raise ValueError("waiting_for_user cannot carry pending outbox")
    elif state.phase is ControllerPhase.WAITING_FOR_EFFECT:
        if not state.pending_outbox_message_id:
            raise ValueError("waiting_for_effect requires pending outbox")
        if state.pending_decision_request_id is not None:
            raise ValueError("waiting_for_effect cannot carry decision request")
    elif (
        state.pending_outbox_message_id is not None
        or state.pending_decision_request_id is not None
    ):
        raise ValueError("current phase cannot carry pending interruption state")


def _validate_effect_result(record: EffectAttemptRecord) -> None:
    succeeded = record.status is EffectAttemptStatus.SUCCEEDED
    if succeeded:
        if record.result_payload is None or record.result_sha256 is None:
            raise ValueError("successful effect attempt requires result payload")
        if record.error_code is not None or record.error_message is not None:
            raise ValueError("successful effect attempt cannot carry an error")
        frozen = freeze_json(record.result_payload)
        if not isinstance(frozen, Mapping):
            raise TypeError("result_payload must be a JSON object")
        require_sha256(record.result_sha256, "result_sha256")
        if content_sha256(frozen) != record.result_sha256:
            raise ValueError("result_payload does not match result_sha256")
        object.__setattr__(record, "result_payload", frozen)
        return
    if record.result_payload is not None or record.result_sha256 is not None:
        raise ValueError("failed effect attempt cannot carry result payload")
    if not record.error_code or not record.error_message:
        raise ValueError("failed effect attempt requires error details")
    require_nonempty(record.error_code, "error_code")
    require_nonempty(record.error_message, "error_message")
