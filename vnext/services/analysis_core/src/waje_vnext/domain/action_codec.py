"""Strict JSON decoder for Primary Agent action proposals."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from waje_vnext.domain.actions import (
    ActionKind,
    AgentActionProposal,
    AskUserPayload,
    CallCapabilityPayload,
    InspectSemanticsPayload,
    ProposedClaim,
    ProposedObjectionClosure,
    ProposeAnswerPayload,
    RecordInterpretationPayload,
    ReviseFramePayload,
    RevisePlanPayload,
    RunProbePayload,
    RunSensitivityPayload,
    StopPayload,
)
from waje_vnext.domain.authority import DecisionOption, WorkTask
from waje_vnext.domain.measurement import MeasurementDesign
from waje_vnext.domain.typed_decode import decode_typed_dataclass


class ActionProposalDecodeError(ValueError):
    pass


def decode_agent_action_proposal(
    value: Mapping[str, Any],
) -> AgentActionProposal:
    try:
        _require_exact_keys(value, {"kind", "payload"}, "proposal")
        kind = ActionKind(_string(value, "kind"))
        payload = _object(value, "payload")
        decoder = _DECODERS[kind]
        return AgentActionProposal(kind=kind, payload=decoder(payload))
    except (KeyError, TypeError, ValueError) as error:
        raise ActionProposalDecodeError(
            "proposal violates the typed action contract"
        ) from error


def _decode_revise_frame(value: Mapping[str, Any]) -> ReviseFramePayload:
    fields = {
        "question_revision_id",
        "revision_reason_ref",
        "measurement_design",
        "objection_closures",
    }
    _require_exact_keys(value, fields, "revise_frame payload")
    return ReviseFramePayload(
        question_revision_id=_string(value, "question_revision_id"),
        revision_reason_ref=_string(value, "revision_reason_ref"),
        measurement_design=decode_typed_dataclass(
            MeasurementDesign,
            _object(value, "measurement_design"),
        ),
        objection_closures=tuple(
            decode_typed_dataclass(
                ProposedObjectionClosure,
                _mapping(item, "objection closure"),
            )
            for item in _array(value, "objection_closures")
        ),
    )


def _decode_revise_plan(value: Mapping[str, Any]) -> RevisePlanPayload:
    _require_exact_keys(
        value,
        {"revision_reason", "tasks"},
        "revise_plan payload",
    )
    tasks = _array(value, "tasks")
    return RevisePlanPayload(
        revision_reason=_string(value, "revision_reason"),
        tasks=tuple(_decode_task(_mapping(item, "task")) for item in tasks),
    )


def _decode_task(value: Mapping[str, Any]) -> WorkTask:
    _require_exact_keys(
        value,
        {
            "task_id",
            "business_purpose",
            "capability_intent",
            "target_claim_ids",
            "depends_on_task_ids",
            "success_conditions",
            "stop_conditions",
        },
        "work task",
    )
    return WorkTask(
        task_id=_string(value, "task_id"),
        business_purpose=_string(value, "business_purpose"),
        capability_intent=_string(value, "capability_intent"),
        target_claim_ids=_string_tuple(value, "target_claim_ids"),
        depends_on_task_ids=_string_tuple(value, "depends_on_task_ids"),
        success_conditions=_string_tuple(value, "success_conditions"),
        stop_conditions=_string_tuple(value, "stop_conditions"),
    )


def _decode_inspect_semantics(
    value: Mapping[str, Any],
) -> InspectSemanticsPayload:
    _require_exact_keys(
        value,
        {"question", "contract_refs"},
        "inspect_semantics payload",
    )
    return InspectSemanticsPayload(
        question=_string(value, "question"),
        contract_refs=_string_tuple(value, "contract_refs"),
    )


def _decode_run_probe(value: Mapping[str, Any]) -> RunProbePayload:
    _require_exact_keys(
        value,
        {"probe_kind", "parameters"},
        "run_probe payload",
    )
    return RunProbePayload(
        probe_kind=_string(value, "probe_kind"),
        parameters=_object(value, "parameters"),
    )


def _decode_call_capability(
    value: Mapping[str, Any],
) -> CallCapabilityPayload:
    _require_exact_keys(
        value,
        {"task_id", "capability_name", "parameters"},
        "call_capability payload",
    )
    return CallCapabilityPayload(
        task_id=_string(value, "task_id"),
        capability_name=_string(value, "capability_name"),
        parameters=_object(value, "parameters"),
    )


def _decode_run_sensitivity(
    value: Mapping[str, Any],
) -> RunSensitivityPayload:
    _require_exact_keys(
        value,
        {"task_id", "variant_label", "parameters"},
        "run_sensitivity payload",
    )
    return RunSensitivityPayload(
        task_id=_string(value, "task_id"),
        variant_label=_string(value, "variant_label"),
        parameters=_object(value, "parameters"),
    )


def _decode_record_interpretation(
    value: Mapping[str, Any],
) -> RecordInterpretationPayload:
    _require_exact_keys(
        value,
        {"evidence_record_ids", "interpretation"},
        "record_interpretation payload",
    )
    return RecordInterpretationPayload(
        evidence_record_ids=_string_tuple(
            value,
            "evidence_record_ids",
        ),
        interpretation=_string(value, "interpretation"),
    )


def _decode_ask_user(value: Mapping[str, Any]) -> AskUserPayload:
    _require_exact_keys(
        value,
        {
            "question",
            "options",
            "recommended_option_id",
            "allow_freeform",
        },
        "ask_user payload",
    )
    options = tuple(
        _decode_decision_option(_mapping(item, "decision option"))
        for item in _array(value, "options")
    )
    allow_freeform = value["allow_freeform"]
    if not isinstance(allow_freeform, bool):
        raise TypeError("allow_freeform must be boolean")
    return AskUserPayload(
        question=_string(value, "question"),
        options=options,
        recommended_option_id=_string(value, "recommended_option_id"),
        allow_freeform=allow_freeform,
    )


def _decode_decision_option(value: Mapping[str, Any]) -> DecisionOption:
    _require_exact_keys(
        value,
        {"option_id", "label", "impact"},
        "decision option",
    )
    return DecisionOption(
        option_id=_string(value, "option_id"),
        label=_string(value, "label"),
        impact=_string(value, "impact"),
    )


def _decode_propose_answer(
    value: Mapping[str, Any],
) -> ProposeAnswerPayload:
    _require_exact_keys(
        value,
        {"claims", "narrative_markdown"},
        "propose_answer payload",
    )
    return ProposeAnswerPayload(
        claims=tuple(
            _decode_proposed_claim(_mapping(item, "proposed claim"))
            for item in _array(value, "claims")
        ),
        narrative_markdown=_string(value, "narrative_markdown"),
    )


def _decode_proposed_claim(value: Mapping[str, Any]) -> ProposedClaim:
    _require_exact_keys(
        value,
        {
            "claim_id",
            "statement",
            "applicability",
            "evidence_record_ids",
            "boundary_ref",
            "limitations",
        },
        "proposed claim",
    )
    boundary_ref = value["boundary_ref"]
    if boundary_ref is not None and not isinstance(boundary_ref, str):
        raise TypeError("boundary_ref must be string or null")
    return ProposedClaim(
        claim_id=_string(value, "claim_id"),
        statement=_string(value, "statement"),
        applicability=_string(value, "applicability"),
        evidence_record_ids=_string_tuple(
            value,
            "evidence_record_ids",
        ),
        boundary_ref=boundary_ref,
        limitations=_string_tuple(value, "limitations"),
    )


def _decode_stop(value: Mapping[str, Any]) -> StopPayload:
    _require_exact_keys(
        value,
        {"reason", "terminal_state"},
        "stop payload",
    )
    return StopPayload(
        reason=_string(value, "reason"),
        terminal_state=_string(value, "terminal_state"),
    )


_DECODERS = {
    ActionKind.REVISE_FRAME: _decode_revise_frame,
    ActionKind.REVISE_PLAN: _decode_revise_plan,
    ActionKind.INSPECT_SEMANTICS: _decode_inspect_semantics,
    ActionKind.RUN_PROBE: _decode_run_probe,
    ActionKind.CALL_CAPABILITY: _decode_call_capability,
    ActionKind.RUN_SENSITIVITY: _decode_run_sensitivity,
    ActionKind.RECORD_INTERPRETATION: _decode_record_interpretation,
    ActionKind.ASK_USER: _decode_ask_user,
    ActionKind.PROPOSE_ANSWER: _decode_propose_answer,
    ActionKind.STOP: _decode_stop,
}


def _require_exact_keys(
    value: Mapping[str, Any],
    expected: set[str],
    label: str,
) -> None:
    actual = set(value)
    if actual != expected:
        raise ValueError(
            "{} fields differ; missing={}, extra={}".format(
                label,
                sorted(expected - actual),
                sorted(actual - expected),
            )
        )


def _string(value: Mapping[str, Any], field_name: str) -> str:
    item = value[field_name]
    if not isinstance(item, str):
        raise TypeError("{} must be a string".format(field_name))
    return item


def _object(
    value: Mapping[str, Any],
    field_name: str,
) -> Mapping[str, Any]:
    return _mapping(value[field_name], field_name)


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("{} must be an object".format(label))
    if any(not isinstance(key, str) for key in value):
        raise TypeError("{} keys must be strings".format(label))
    return value


def _array(value: Mapping[str, Any], field_name: str) -> list[object]:
    item = value[field_name]
    if not isinstance(item, list):
        raise TypeError("{} must be an array".format(field_name))
    return item


def _string_tuple(
    value: Mapping[str, Any],
    field_name: str,
) -> tuple[str, ...]:
    items = _array(value, field_name)
    if any(not isinstance(item, str) for item in items):
        raise TypeError("{} must contain strings".format(field_name))
    return tuple(items)
