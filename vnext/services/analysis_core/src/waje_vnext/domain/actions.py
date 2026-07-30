"""Typed business proposals and controller-bound action envelopes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Mapping

from .authority import DecisionOption, WorkTask
from .canonical import (
    FrozenJson,
    content_sha256,
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
    revision_reason: str
    estimand: str
    observation_unit: str
    numerator: str
    denominator: str
    exposure: str
    comparison: str
    assumptions: tuple[str, ...]
    alternatives: tuple[str, ...]
    falsification_conditions: tuple[str, ...]
    reversal_conditions: tuple[str, ...]
    success_conditions: tuple[str, ...]
    stop_conditions: tuple[str, ...]
    decision_record_ids: tuple[str, ...] = ()
    semantic_contract_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_dataclass_strings(self)
        for name in (
            "assumptions",
            "alternatives",
            "falsification_conditions",
            "reversal_conditions",
            "success_conditions",
            "stop_conditions",
            "decision_record_ids",
            "semantic_contract_refs",
        ):
            _validate_string_tuple(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class RevisePlanPayload:
    revision_reason: str
    tasks: tuple[WorkTask, ...]

    def __post_init__(self) -> None:
        require_nonempty(self.revision_reason, "revision_reason")
        _validate_typed_tuple(self.tasks, WorkTask, "tasks")
        if not self.tasks:
            raise ValueError("revise_plan requires at least one task")


@dataclass(frozen=True, slots=True)
class InspectSemanticsPayload:
    question: str
    contract_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        require_nonempty(self.question, "question")
        _validate_string_tuple(self.contract_refs, "contract_refs")


@dataclass(frozen=True, slots=True)
class RunProbePayload:
    probe_kind: str
    parameters: Mapping[str, FrozenJson]

    def __post_init__(self) -> None:
        require_nonempty(self.probe_kind, "probe_kind")
        _freeze_parameters(self)


@dataclass(frozen=True, slots=True)
class CallCapabilityPayload:
    task_id: str
    capability_name: str
    parameters: Mapping[str, FrozenJson]

    def __post_init__(self) -> None:
        require_nonempty(self.task_id, "task_id")
        require_nonempty(self.capability_name, "capability_name")
        _freeze_parameters(self)


@dataclass(frozen=True, slots=True)
class RunSensitivityPayload:
    task_id: str
    variant_label: str
    parameters: Mapping[str, FrozenJson]

    def __post_init__(self) -> None:
        require_nonempty(self.task_id, "task_id")
        require_nonempty(self.variant_label, "variant_label")
        _freeze_parameters(self)


@dataclass(frozen=True, slots=True)
class RecordInterpretationPayload:
    evidence_record_ids: tuple[str, ...]
    interpretation: str

    def __post_init__(self) -> None:
        _validate_string_tuple(
            self.evidence_record_ids,
            "evidence_record_ids",
        )
        if not self.evidence_record_ids:
            raise ValueError("interpretation requires evidence")
        require_nonempty(self.interpretation, "interpretation")


@dataclass(frozen=True, slots=True)
class AskUserPayload:
    question: str
    options: tuple[DecisionOption, ...]
    recommended_option_id: str
    allow_freeform: bool = True

    def __post_init__(self) -> None:
        require_nonempty(self.question, "question")
        require_nonempty(
            self.recommended_option_id,
            "recommended_option_id",
        )
        _validate_typed_tuple(self.options, DecisionOption, "options")
        if not 2 <= len(self.options) <= 3:
            raise ValueError("ask_user requires two or three options")
        option_ids = tuple(option.option_id for option in self.options)
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("ask_user option IDs must be unique")
        if self.recommended_option_id not in option_ids:
            raise ValueError("recommended option must be present")
        if not self.allow_freeform:
            raise ValueError("ask_user must retain a freeform correction path")


@dataclass(frozen=True, slots=True)
class ProposedClaim:
    claim_id: str
    statement: str
    applicability: str
    evidence_record_ids: tuple[str, ...]
    boundary_ref: str | None
    limitations: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("claim_id", "statement", "applicability"):
            require_nonempty(getattr(self, name), name)
        _validate_string_tuple(
            self.evidence_record_ids,
            "evidence_record_ids",
        )
        _validate_string_tuple(self.limitations, "limitations")
        if self.boundary_ref is not None:
            require_nonempty(self.boundary_ref, "boundary_ref")
        if not self.evidence_record_ids and not self.boundary_ref:
            raise ValueError(
                "proposed claim requires evidence or an explicit boundary"
            )


@dataclass(frozen=True, slots=True)
class ProposeAnswerPayload:
    claims: tuple[ProposedClaim, ...]
    narrative_markdown: str

    def __post_init__(self) -> None:
        require_nonempty(self.narrative_markdown, "narrative_markdown")
        _validate_typed_tuple(self.claims, ProposedClaim, "claims")
        if not self.claims:
            raise ValueError("propose_answer requires at least one claim")
        claim_ids = tuple(claim.claim_id for claim in self.claims)
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("proposed claim IDs must be unique")


@dataclass(frozen=True, slots=True)
class StopPayload:
    reason: str
    terminal_state: str

    def __post_init__(self) -> None:
        require_nonempty(self.reason, "reason")
        if self.terminal_state not in {"stopped", "closed"}:
            raise ValueError("terminal_state must be stopped or closed")


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
class AgentActionProposal:
    kind: ActionKind
    payload: ActionPayload

    def __post_init__(self) -> None:
        _validate_kind_payload(self.kind, self.payload)

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)


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
        _validate_kind_payload(self.kind, self.payload)

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)


def _validate_kind_payload(
    kind: ActionKind,
    payload: ActionPayload,
) -> None:
    if not isinstance(kind, ActionKind):
        raise TypeError("kind must be ActionKind")
    expected_type = _PAYLOAD_TYPES[kind]
    if not isinstance(payload, expected_type):
        raise TypeError(
            "action {!r} requires payload {!r}".format(
                kind.value,
                expected_type.__name__,
            )
        )


def _freeze_parameters(payload: object) -> None:
    frozen = freeze_json(getattr(payload, "parameters"))
    if not isinstance(frozen, Mapping):
        raise TypeError("action parameters must be a JSON object")
    object.__setattr__(payload, "parameters", frozen)


def _validate_dataclass_strings(value: object) -> None:
    for name in getattr(value, "__dataclass_fields__", {}):
        member = getattr(value, name)
        if isinstance(member, str):
            require_nonempty(member, name)


def _validate_string_tuple(
    values: tuple[str, ...],
    field_name: str,
) -> None:
    if not isinstance(values, tuple):
        raise TypeError("{} must be a tuple".format(field_name))
    for index, value in enumerate(values):
        if not isinstance(value, str):
            raise TypeError("{}[{}] must be a string".format(field_name, index))
        require_nonempty(value, "{}[{}]".format(field_name, index))


def _validate_typed_tuple(
    values: tuple[object, ...],
    expected_type: type[object],
    field_name: str,
) -> None:
    if not isinstance(values, tuple):
        raise TypeError("{} must be a tuple".format(field_name))
    for index, value in enumerate(values):
        if not isinstance(value, expected_type):
            raise TypeError(
                "{}[{}] must be {}".format(
                    field_name,
                    index,
                    expected_type.__name__,
                )
            )
