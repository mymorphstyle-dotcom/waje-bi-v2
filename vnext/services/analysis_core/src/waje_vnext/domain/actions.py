"""Typed actions emitted by the Primary Business Analysis Agent."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Mapping

from .authority import DecisionOption
from .canonical import (
    FrozenJson,
    freeze_json,
    require_aware_datetime,
    require_nonempty,
)


class ActionKind(StrEnum):
    REVISE_FRAME = "revise_frame"
    REVISE_PLAN = "revise_plan"
    INSPECT_SEMANTICS = "inspect_semantics"
    RUN_PROBE = "run_probe"
    CALL_CAPABILITY = "call_capability"
    RUN_SENSITIVITY = "run_sensitivity"
    RECORD_INTERPRETATION = "record_interpretation"
    ASK_USER = "ask_user"
    PROPOSE_ANSWER = "propose_answer"
    STOP = "stop"


@dataclass(frozen=True, slots=True)
class ReviseFramePayload:
    frame_revision_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class RevisePlanPayload:
    plan_revision_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class InspectSemanticsPayload:
    question: str
    contract_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RunProbePayload:
    probe_kind: str
    parameters: Mapping[str, FrozenJson]

    def __post_init__(self) -> None:
        _freeze_parameters(self)


@dataclass(frozen=True, slots=True)
class CallCapabilityPayload:
    task_id: str
    capability_name: str
    parameters: Mapping[str, FrozenJson]

    def __post_init__(self) -> None:
        _freeze_parameters(self)


@dataclass(frozen=True, slots=True)
class RunSensitivityPayload:
    task_id: str
    variant_label: str
    parameters: Mapping[str, FrozenJson]

    def __post_init__(self) -> None:
        _freeze_parameters(self)


@dataclass(frozen=True, slots=True)
class RecordInterpretationPayload:
    interpretation_id: str
    evidence_record_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AskUserPayload:
    decision_record_id: str
    question: str
    options: tuple[DecisionOption, ...]
    recommended_option_id: str
    allow_freeform: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.options, tuple):
            raise TypeError("options must be a tuple")
        if not 2 <= len(self.options) <= 3:
            raise ValueError("ask_user requires two or three options")
        if any(not isinstance(option, DecisionOption) for option in self.options):
            raise TypeError("ask_user options must be DecisionOption values")
        option_ids = tuple(option.option_id for option in self.options)
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("ask_user option IDs must be unique")
        if self.recommended_option_id not in option_ids:
            raise ValueError("recommended option must be present")
        if not self.allow_freeform:
            raise ValueError("ask_user must retain a freeform correction path")


@dataclass(frozen=True, slots=True)
class ProposeAnswerPayload:
    answer_version_id: str


@dataclass(frozen=True, slots=True)
class StopPayload:
    reason: str
    terminal_state: str


type ActionPayload = (
    ReviseFramePayload
    | RevisePlanPayload
    | InspectSemanticsPayload
    | RunProbePayload
    | CallCapabilityPayload
    | RunSensitivityPayload
    | RecordInterpretationPayload
    | AskUserPayload
    | ProposeAnswerPayload
    | StopPayload
)


_PAYLOAD_TYPES: dict[ActionKind, type[ActionPayload]] = {
    ActionKind.REVISE_FRAME: ReviseFramePayload,
    ActionKind.REVISE_PLAN: RevisePlanPayload,
    ActionKind.INSPECT_SEMANTICS: InspectSemanticsPayload,
    ActionKind.RUN_PROBE: RunProbePayload,
    ActionKind.CALL_CAPABILITY: CallCapabilityPayload,
    ActionKind.RUN_SENSITIVITY: RunSensitivityPayload,
    ActionKind.RECORD_INTERPRETATION: RecordInterpretationPayload,
    ActionKind.ASK_USER: AskUserPayload,
    ActionKind.PROPOSE_ANSWER: ProposeAnswerPayload,
    ActionKind.STOP: StopPayload,
}


@dataclass(frozen=True, slots=True)
class ActionEnvelope:
    action_id: str
    case_id: str
    kind: ActionKind
    expected_head_version: int
    idempotency_key: str
    issued_at: datetime
    payload: ActionPayload

    def __post_init__(self) -> None:
        require_nonempty(self.action_id, "action_id")
        require_nonempty(self.case_id, "case_id")
        require_nonempty(self.idempotency_key, "idempotency_key")
        if self.expected_head_version < 0:
            raise ValueError("expected_head_version must be non-negative")
        require_aware_datetime(self.issued_at, "issued_at")
        if not isinstance(self.kind, ActionKind):
            raise TypeError("kind must be ActionKind")
        expected_type = _PAYLOAD_TYPES[self.kind]
        if not isinstance(self.payload, expected_type):
            raise TypeError(
                "action {!r} requires payload {!r}".format(
                    self.kind.value, expected_type.__name__
                )
            )
        _validate_payload_strings(self.payload)
        if isinstance(self.payload, InspectSemanticsPayload):
            _validate_string_tuple(
                self.payload.contract_refs,
                "contract_refs",
            )
        if isinstance(self.payload, RecordInterpretationPayload):
            _validate_string_tuple(
                self.payload.evidence_record_ids,
                "evidence_record_ids",
            )


def _freeze_parameters(payload: object) -> None:
    frozen = freeze_json(getattr(payload, "parameters"))
    if not isinstance(frozen, Mapping):
        raise TypeError("action parameters must be a JSON object")
    object.__setattr__(payload, "parameters", frozen)


def _validate_payload_strings(payload: ActionPayload) -> None:
    for name in getattr(payload, "__dataclass_fields__", {}):
        value = getattr(payload, name)
        if isinstance(value, str):
            require_nonempty(value, name)


def _validate_string_tuple(values: tuple[str, ...], field_name: str) -> None:
    if not isinstance(values, tuple):
        raise TypeError("{} must be a tuple".format(field_name))
    for index, value in enumerate(values):
        if not isinstance(value, str):
            raise TypeError("{}[{}] must be a string".format(field_name, index))
        require_nonempty(value, "{}[{}]".format(field_name, index))
