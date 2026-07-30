"""Read-only Workflow projection from accepted plan and event journal."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from waje_vnext.domain.authority import (
    AnalysisFrameRevision,
    AnswerVersion,
    EvidenceRecord,
    InvestigationCase,
    WorkPlanRevision,
)
from waje_vnext.domain.canonical import require_nonempty
from waje_vnext.domain.events import EventJournalEntry


class WorkflowProjectionMode(StrEnum):
    REPLAY = "replay"
    STATIC = "static"


class WorkflowTaskStatus(StrEnum):
    PENDING = "pending"
    INVESTIGATING = "investigating"
    COMPLETED = "completed"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class WorkflowTaskProjection:
    task_id: str
    business_purpose: str
    capability_intent: str
    status: WorkflowTaskStatus
    depends_on_task_ids: tuple[str, ...]
    requirement_ids: tuple[str, ...]
    evidence_record_ids: tuple[str, ...]
    business_summary: str | None

    def __post_init__(self) -> None:
        for name in ("task_id", "business_purpose", "capability_intent"):
            require_nonempty(getattr(self, name), name)
        if not isinstance(self.status, WorkflowTaskStatus):
            raise TypeError("status must be WorkflowTaskStatus")
        if self.business_summary is not None:
            require_nonempty(self.business_summary, "business_summary")


@dataclass(frozen=True, slots=True)
class WorkflowProjection:
    case_id: str
    frame_revision_id: str
    frame_revision_number: int
    plan_revision_id: str
    plan_revision_number: int
    answer_version_id: str | None
    answer_status: str | None
    mode: WorkflowProjectionMode
    event_cursor: int
    tasks: tuple[WorkflowTaskProjection, ...]

    def __post_init__(self) -> None:
        for name in ("case_id", "frame_revision_id", "plan_revision_id"):
            require_nonempty(getattr(self, name), name)
        if self.frame_revision_number < 1 or self.plan_revision_number < 1:
            raise ValueError("workflow revisions must be positive")
        if not isinstance(self.mode, WorkflowProjectionMode):
            raise TypeError("mode must be WorkflowProjectionMode")
        if self.event_cursor < 0:
            raise ValueError("event_cursor must be non-negative")
        if not self.tasks:
            raise ValueError("workflow projection requires plan tasks")


def build_workflow_projection(
    *,
    case: InvestigationCase,
    frame: AnalysisFrameRevision,
    plan: WorkPlanRevision,
    answer: AnswerVersion | None,
    events: tuple[EventJournalEntry, ...],
    evidence: tuple[EvidenceRecord, ...],
) -> WorkflowProjection:
    if (
        case.accepted_frame_revision_id != frame.frame_revision_id
        or case.accepted_plan_revision_id != plan.plan_revision_id
        or plan.frame_revision_id != frame.frame_revision_id
    ):
        raise ValueError("workflow inputs do not match accepted authority")
    if (
        answer is None
        and case.accepted_answer_version_id is not None
        or answer is not None
        and case.accepted_answer_version_id != answer.answer_version_id
    ):
        raise ValueError("workflow answer does not match accepted authority")
    complete_journal = _has_complete_chronology(events)
    status_by_task = {
        task.task_id: WorkflowTaskStatus.PENDING for task in plan.tasks
    }
    summary_by_task: dict[str, str] = {}
    evidence_by_task: dict[str, list[str]] = {
        task.task_id: [] for task in plan.tasks
    }
    for record in evidence:
        if (
            record.case_id != case.case_id
            or record.frame_revision_id != frame.frame_revision_id
            or record.plan_revision_id != plan.plan_revision_id
            or record.task_id not in evidence_by_task
        ):
            continue
        evidence_by_task[record.task_id].append(record.evidence_record_id)
        summary_by_task[record.task_id] = record.business_summary
        status_by_task[record.task_id] = WorkflowTaskStatus.COMPLETED
    if complete_journal:
        for event in events:
            projection = event.customer_projection
            if projection is None:
                continue
            task_id = projection.get("task_id")
            state = projection.get("state")
            if not isinstance(task_id, str) or task_id not in status_by_task:
                continue
            if state == "investigating":
                status_by_task[task_id] = WorkflowTaskStatus.INVESTIGATING
            elif state == "completed":
                status_by_task[task_id] = WorkflowTaskStatus.COMPLETED
            elif state == "blocked":
                status_by_task[task_id] = WorkflowTaskStatus.BLOCKED
            business_summary = projection.get("business_summary")
            if isinstance(business_summary, str) and business_summary.strip():
                summary_by_task[task_id] = business_summary
    tasks = tuple(
        WorkflowTaskProjection(
            task_id=task.task_id,
            business_purpose=task.business_purpose,
            capability_intent=task.capability_intent,
            status=status_by_task[task.task_id],
            depends_on_task_ids=task.depends_on_task_ids,
            requirement_ids=task.requirement_ids,
            evidence_record_ids=tuple(evidence_by_task[task.task_id]),
            business_summary=summary_by_task.get(task.task_id),
        )
        for task in plan.tasks
    )
    return WorkflowProjection(
        case_id=case.case_id,
        frame_revision_id=frame.frame_revision_id,
        frame_revision_number=frame.revision_number,
        plan_revision_id=plan.plan_revision_id,
        plan_revision_number=plan.revision_number,
        answer_version_id=None if answer is None else answer.answer_version_id,
        answer_status=None if answer is None else answer.status.value,
        mode=(
            WorkflowProjectionMode.REPLAY
            if complete_journal
            else WorkflowProjectionMode.STATIC
        ),
        event_cursor=0 if not events else events[-1].cursor,
        tasks=tasks,
    )


def _has_complete_chronology(
    events: tuple[EventJournalEntry, ...],
) -> bool:
    if not events:
        return False
    return tuple(event.cursor for event in events) == tuple(
        range(1, events[-1].cursor + 1)
    )
