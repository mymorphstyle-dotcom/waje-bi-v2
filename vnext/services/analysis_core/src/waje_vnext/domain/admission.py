"""Deterministic admission for Primary Agent typed actions."""

from __future__ import annotations

from dataclasses import dataclass

from .actions import (
    ActionEnvelope,
    ActionKind,
    CallCapabilityPayload,
    ReviseFramePayload,
    RevisePlanPayload,
    RunProbePayload,
    RunSensitivityPayload,
)
from .authority import (
    AnalysisFrameRevision,
    CaseLifecycle,
    InvestigationCase,
    MeasurementDesignError,
    WorkPlanRevision,
    validate_measurement_design,
)


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
    current_frame: AnalysisFrameRevision | None,
    current_plan: WorkPlanRevision | None,
) -> ActionAdmission:
    if action.case_id != case.case_id:
        return _rejected(case, "case_mismatch")
    if action.expected_head_version != case.head_version:
        return _rejected(case, "stale_head")
    if case.lifecycle in {CaseLifecycle.STOPPED, CaseLifecycle.CLOSED}:
        return _rejected(case, "case_terminal")

    if action.kind is ActionKind.REVISE_FRAME:
        assert isinstance(action.payload, ReviseFramePayload)
        try:
            validate_measurement_design(
                primary_estimator=action.payload.primary_estimator,
                exposure=action.payload.exposure,
                assumptions=action.payload.assumptions,
                alternatives=action.payload.alternatives,
                requirements=action.payload.requirements,
                falsification_conditions=(
                    action.payload.falsification_conditions
                ),
                reversal_conditions=action.payload.reversal_conditions,
                success_conditions=action.payload.success_conditions,
                stop_conditions=action.payload.stop_conditions,
                semantic_contract_refs=action.payload.semantic_contract_refs,
            )
        except MeasurementDesignError as error:
            return _rejected(case, error.reason_code)
        except (TypeError, ValueError):
            return _rejected(case, "frame_contract_invalid")
        return _accepted(case, frame=True)

    if action.kind is ActionKind.REVISE_PLAN:
        if case.accepted_frame_revision_id is None:
            return _rejected(case, "plan_requires_frame")
        assert isinstance(action.payload, RevisePlanPayload)
        if (
            current_frame is None
            or current_frame.frame_revision_id
            != case.accepted_frame_revision_id
        ):
            return _rejected(case, "current_frame_unavailable")
        covered = {
            requirement_id
            for task in action.payload.tasks
            for requirement_id in task.requirement_ids
        }
        required = {
            requirement.requirement_id
            for requirement in current_frame.requirements
        }
        if covered != required:
            return _rejected(case, "plan_requirement_coverage_mismatch")
        return _accepted(case, plan=True)

    if action.kind in {
        ActionKind.RUN_PROBE,
        ActionKind.CALL_CAPABILITY,
        ActionKind.RUN_SENSITIVITY,
        ActionKind.RECORD_INTERPRETATION,
        ActionKind.PROPOSE_ANSWER,
    } and case.accepted_plan_revision_id is None:
        return _rejected(case, "action_requires_plan")

    if action.kind in {
        ActionKind.RUN_PROBE,
        ActionKind.CALL_CAPABILITY,
        ActionKind.RUN_SENSITIVITY,
    }:
        payload = action.payload
        assert isinstance(
            payload,
            CallCapabilityPayload
            | RunProbePayload
            | RunSensitivityPayload,
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

    return _accepted(case)


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
