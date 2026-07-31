"""Deterministic admission for Primary Agent typed actions."""

from __future__ import annotations

from dataclasses import dataclass

from .actions import (
    ActionEnvelope,
    ActionKind,
    CallCapabilityPayload,
    ReviseFramePayload,
    RevisePlanPayload,
    RunSensitivityPayload,
)
from .async_runtime import AsyncJobKind
from .authority import CaseLifecycle, InvestigationCase, WorkPlanRevision
from .canonical import content_sha256, to_jsonable
from .events import EventJournalEntry, JournalEventType
from .planning import (
    QueryBindingEnvelope,
    get_capability_intent_contract,
)
from .runtime_state import ActionReceipt, OutboxMessage


_EFFECT_JOB_KINDS = {
    ActionKind.INSPECT_SEMANTICS: AsyncJobKind.SEMANTIC_INSPECTION,
    ActionKind.RUN_PROBE: AsyncJobKind.DATA_PROBE,
    ActionKind.CALL_CAPABILITY: AsyncJobKind.CAPABILITY,
    ActionKind.RUN_SENSITIVITY: AsyncJobKind.SENSITIVITY,
}


@dataclass(frozen=True, slots=True)
class ActionAdmission:
    accepted: bool
    reason_code: str
    head_version: int
    creates_frame_revision: bool
    creates_plan_revision: bool


def admit_action(
    *,
    case: InvestigationCase,
    action: ActionEnvelope,
    current_plan: WorkPlanRevision | None,
    current_query_bindings: tuple[QueryBindingEnvelope, ...] = (),
) -> ActionAdmission:
    if action.case_id != case.case_id:
        return _rejected(case, "case_mismatch")
    if action.expected_head_version != case.head_version:
        return _rejected(case, "stale_head")
    if case.lifecycle in {CaseLifecycle.STOPPED, CaseLifecycle.CLOSED}:
        return _rejected(case, "case_terminal")

    if action.kind is ActionKind.REVISE_FRAME:
        assert isinstance(action.payload, ReviseFramePayload)
        if case.accepted_question_revision_id is None:
            return _rejected(case, "frame_requires_question")
        if (
            action.payload.question_revision_id
            != case.accepted_question_revision_id
        ):
            return _rejected(case, "frame_question_mismatch")
        return _accepted(case, frame=True)

    if action.kind is ActionKind.REVISE_PLAN:
        if case.accepted_frame_revision_id is None:
            return _rejected(case, "plan_requires_frame")
        assert isinstance(action.payload, RevisePlanPayload)
        return _accepted(case, plan=True)

    if (
        action.kind is ActionKind.RUN_PROBE
        and case.accepted_frame_revision_id is None
    ):
        return _rejected(case, "probe_requires_frame")

    if action.kind in {
        ActionKind.CALL_CAPABILITY,
        ActionKind.RUN_SENSITIVITY,
        ActionKind.RECORD_INTERPRETATION,
        ActionKind.PROPOSE_ANSWER,
    } and case.accepted_plan_revision_id is None:
        return _rejected(case, "action_requires_plan")

    if action.kind in {
        ActionKind.CALL_CAPABILITY,
        ActionKind.RUN_SENSITIVITY,
    }:
        payload = action.payload
        assert isinstance(
            payload,
            CallCapabilityPayload | RunSensitivityPayload,
        )
        if current_plan is None or current_plan.plan_revision_id != (
            case.accepted_plan_revision_id
        ):
            return _rejected(case, "current_plan_unavailable")
        if (
            current_plan.case_id != case.case_id
            or current_plan.frame_revision_id
            != case.accepted_frame_revision_id
        ):
            return _rejected(case, "current_plan_incompatible")
        if payload.task_id not in {task.task_id for task in current_plan.tasks}:
            return _rejected(case, "unknown_plan_task")
        binding = next(
            (
                item
                for item in current_query_bindings
                if item.query_binding_id == payload.query_binding_id
            ),
            None,
        )
        if binding is None:
            return _rejected(case, "unknown_query_binding")
        if (
            binding.case_id != case.case_id
            or binding.frame_revision_id
            != case.accepted_frame_revision_id
            or binding.plan_revision_id
            != case.accepted_plan_revision_id
            or binding.task_id != payload.task_id
        ):
            return _rejected(case, "query_binding_mismatch")
        task = next(
            item
            for item in current_plan.tasks
            if item.task_id == payload.task_id
        )
        intent = get_capability_intent_contract(
            task.capability_intent_ref
        )
        if action.kind.value not in intent.allowed_action_kinds:
            return _rejected(
                case,
                "capability_intent_action_mismatch",
            )
        if payload.query_binding_id not in task.query_binding_ids:
            return _rejected(case, "query_binding_not_owned")
        if (
            isinstance(payload, RunSensitivityPayload)
            and payload.sensitivity_id
            not in binding.measurement_binding.sensitivity_ids
        ):
            return _rejected(case, "unknown_frame_sensitivity")

    return _accepted(case)


def validate_effect_outbox_binding(
    *,
    case: InvestigationCase,
    message: OutboxMessage,
    action: ActionEnvelope,
    admission_event: EventJournalEntry,
    source_event: EventJournalEntry,
    receipt: ActionReceipt,
    current_plan: WorkPlanRevision | None,
    current_query_bindings: tuple[QueryBindingEnvelope, ...] = (),
) -> None:
    """Re-admit and exactly bind one generic effect outbox message.

    The outbox is an authority commit boundary. A persisted proposal alone is
    insufficient because rejected proposals are also retained for audit.
    """

    expected_job_kind = _EFFECT_JOB_KINDS.get(action.kind)
    if expected_job_kind is None or message.job_kind is not expected_job_kind:
        raise ValueError("effect outbox job kind does not match action")
    if message.action_id != action.action_id:
        raise ValueError("effect outbox action identity does not match")

    admission = admit_action(
        case=case,
        action=action,
        current_plan=current_plan,
        current_query_bindings=current_query_bindings,
    )
    if not admission.accepted:
        raise ValueError(
            "effect outbox action is not currently admitted: {}".format(
                admission.reason_code
            )
        )

    expected_admission_payload = {
        "action_kind": action.kind.value,
        "reason_code": "accepted",
        "request_sha256": action.content_sha256,
    }
    if (
        admission_event.case_id != action.case_id
        or admission_event.event_type is not JournalEventType.ACTION_ADMITTED
        or admission_event.action_id != action.action_id
        or admission_event.authority_ref != action.action_id
        or content_sha256(admission_event.payload)
        != content_sha256(expected_admission_payload)
    ):
        raise ValueError("effect outbox lacks exact ACTION_ADMITTED proof")

    if (
        receipt.case_id != action.case_id
        or receipt.action_id != action.action_id
        or receipt.idempotency_key != action.idempotency_key
        or receipt.request_sha256 != action.content_sha256
        or receipt.result_payload.get("result_code") != "accepted"
        or receipt.event_cursor != message.source_event_cursor
    ):
        raise ValueError("effect outbox lacks successful action receipt")

    expected_payload = {
        "action_kind": action.kind.value,
        "request": to_jsonable(action.payload),
        "expected_head_version": action.expected_head_version,
    }
    expected_source_payload = {
        "destination": message.destination,
        "payload_sha256": content_sha256(expected_payload),
    }
    if (
        content_sha256(message.payload) != content_sha256(expected_payload)
        or source_event.case_id != action.case_id
        or source_event.cursor != message.source_event_cursor
        or source_event.event_type is not JournalEventType.EFFECT_ENQUEUED
        or source_event.action_id != action.action_id
        or source_event.authority_ref != message.outbox_message_id
        or content_sha256(source_event.payload)
        != content_sha256(expected_source_payload)
    ):
        raise ValueError(
            "effect outbox payload is not the exact admitted action request"
        )
    if (
        message.expected_head_version != action.expected_head_version
        or message.expected_authority_epoch
        != action.operation.authority_revision
        or message.operation.causation_id
        != action.operation.operation_id
    ):
        raise ValueError("effect outbox changes action authority identity")


def _accepted(
    case: InvestigationCase,
    *,
    frame: bool = False,
    plan: bool = False,
) -> ActionAdmission:
    return ActionAdmission(
        accepted=True,
        reason_code="accepted",
        head_version=case.head_version,
        creates_frame_revision=frame,
        creates_plan_revision=plan,
    )


def _rejected(case: InvestigationCase, reason_code: str) -> ActionAdmission:
    return ActionAdmission(
        accepted=False,
        reason_code=reason_code,
        head_version=case.head_version,
        creates_frame_revision=False,
        creates_plan_revision=False,
    )
