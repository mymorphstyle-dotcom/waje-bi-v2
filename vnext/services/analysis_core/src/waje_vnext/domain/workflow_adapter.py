"""Fail-closed translation from journal events to Workflow facts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .answering import AnswerStatus, AnswerVersion
from .authority import AnalysisFrameRevision, WorkPlanRevision
from .canonical import freeze_json
from .events import (
    EventJournalEntry,
    JournalEventType,
    journal_event_sha256,
)
from .evidence import (
    ObligationSatisfactionRecord,
    ObligationSatisfactionStatus,
)
from .measurement import QuestionRevision, ResolvedEvidenceObligation
from .obligation_scheduler import (
    ObligationCompletionRecord,
    ObligationDispatchRecord,
    ObligationScheduleCheckpoint,
    ObligationScheduleRecord,
    ObligationTerminalStatus,
)
from .planning import PlanAdoptionRecord
from .workflow import (
    AuthoritySupersededFact,
    ExecutionState,
    ObligationDispositionFact,
    ObligationState,
    PlanAcceptedFact,
    PublicationDispositionFact,
    PublicationState,
    TaskExecutionBatchFact,
    TaskExecutionFact,
    TaskExecutionUpdate,
    WorkflowFact,
    WorkflowNoChangeFact,
    WorkflowObligationDefinition,
    WorkflowReadModel,
    WorkflowTaskDefinition,
)


class WorkflowJournalAdapterError(ValueError):
    """The journal event cannot safely enter the customer Workflow."""


class WorkflowJournalEventUnsupported(WorkflowJournalAdapterError):
    """An event type has no declared Workflow policy."""


@dataclass(frozen=True, slots=True)
class AcceptedPlanAuthority:
    plan: WorkPlanRevision
    adoption: PlanAdoptionRecord
    question: QuestionRevision
    frame: AnalysisFrameRevision
    obligations: tuple[ResolvedEvidenceObligation, ...]


@dataclass(frozen=True, slots=True)
class SupersedingAuthority:
    question: QuestionRevision | None = None
    frame: AnalysisFrameRevision | None = None

    def __post_init__(self) -> None:
        if (self.question is None) == (self.frame is None):
            raise ValueError(
                "superseding authority requires exactly one revision"
            )


@dataclass(frozen=True, slots=True)
class DispatchAuthority:
    schedule: ObligationScheduleRecord
    dispatch: ObligationDispatchRecord


@dataclass(frozen=True, slots=True)
class CheckpointAuthority:
    schedule: ObligationScheduleRecord
    checkpoint: ObligationScheduleCheckpoint
    completions: tuple[ObligationCompletionRecord, ...]


@dataclass(frozen=True, slots=True)
class SatisfactionAuthority:
    satisfaction: ObligationSatisfactionRecord


@dataclass(frozen=True, slots=True)
class AnswerAuthority:
    answer: AnswerVersion


type WorkflowEventAuthority = (
    AcceptedPlanAuthority
    | SupersedingAuthority
    | DispatchAuthority
    | CheckpointAuthority
    | SatisfactionAuthority
    | AnswerAuthority
    | None
)


class WorkflowAuthorityResolver(Protocol):
    def resolve_workflow_event_authority(
        self,
        event: EventJournalEntry,
    ) -> WorkflowEventAuthority: ...


class _EventPolicy(StrEnum):
    NO_CHANGE = "no_change"
    ACCEPT_PLAN = "accept_plan"
    SUPERSEDE_QUESTION = "supersede_question"
    SUPERSEDE_FRAME = "supersede_frame"
    DISPATCH = "dispatch"
    CHECKPOINT = "checkpoint"
    SATISFACTION = "satisfaction"
    ANSWER = "answer"


_NO_CHANGE_EVENTS = {
    JournalEventType.CASE_OPENED,
    JournalEventType.MESSAGE_INGRESSED,
    JournalEventType.ACTION_ADMITTED,
    JournalEventType.ACTION_REJECTED,
    JournalEventType.USER_DECISION_REQUESTED,
    JournalEventType.CAPABILITY_RESULT_LANDED,
    JournalEventType.EVIDENCE_RECORDED,
    JournalEventType.EVIDENCE_ADMISSION_RECORDED,
    JournalEventType.MEASUREMENT_RESOLUTION_RECORDED,
    JournalEventType.EVIDENCE_OBLIGATION_RECORDED,
    JournalEventType.EVIDENCE_VALIDITY_RECORDED,
    JournalEventType.EVIDENCE_USE_BOUND,
    JournalEventType.OBLIGATION_SCHEDULE_CREATED,
    JournalEventType.OBLIGATION_COMPLETION_ADMITTED,
    JournalEventType.SETTLEMENT_PRECONDITION_RECORDED,
    JournalEventType.ANSWER_CANDIDATE_RECORDED,
    JournalEventType.CLAIM_PRECHECK_RECORDED,
    JournalEventType.INTERPRETATION_RECORDED,
    JournalEventType.USER_DECISION_RECORDED,
    JournalEventType.REVIEWER_OBJECTION_RECORDED,
    JournalEventType.WORKFLOW_PROJECTION_APPLIED,
    JournalEventType.CHECKPOINT_RECORDED,
    JournalEventType.EFFECT_ENQUEUED,
    JournalEventType.EFFECT_ATTEMPT_FAILED,
    JournalEventType.EFFECT_COMPLETED,
    JournalEventType.LLM_JOB_ENQUEUED,
    JournalEventType.LLM_JOB_COMPLETED,
    JournalEventType.MESSAGE_BINDING_JOB_ENQUEUED,
    JournalEventType.MESSAGE_BINDING_COMPLETED,
    JournalEventType.REVIEWER_JOB_ENQUEUED,
    JournalEventType.REVIEWER_JOB_COMPLETED,
    JournalEventType.JOB_SUPERSEDED,
    JournalEventType.JOB_TERMINALLY_FAILED,
    JournalEventType.RUN_RESUMED,
    JournalEventType.CASE_STOPPED,
    JournalEventType.CASE_CLOSED,
}
_EVENT_POLICIES = {
    event_type: _EventPolicy.NO_CHANGE
    for event_type in _NO_CHANGE_EVENTS
}
_EVENT_POLICIES.update(
    {
        JournalEventType.PLAN_ACCEPTED: _EventPolicy.ACCEPT_PLAN,
        JournalEventType.QUESTION_ACCEPTED: (
            _EventPolicy.SUPERSEDE_QUESTION
        ),
        JournalEventType.FRAME_ACCEPTED: _EventPolicy.SUPERSEDE_FRAME,
        JournalEventType.OBLIGATION_DISPATCH_ENQUEUED: (
            _EventPolicy.DISPATCH
        ),
        JournalEventType.OBLIGATION_SCHEDULE_CHECKPOINTED: (
            _EventPolicy.CHECKPOINT
        ),
        JournalEventType.OBLIGATION_SATISFACTION_RECORDED: (
            _EventPolicy.SATISFACTION
        ),
        JournalEventType.ANSWER_ACCEPTED: _EventPolicy.ANSWER,
    }
)


def journal_event_to_workflow_fact(
    event: EventJournalEntry,
    *,
    current: WorkflowReadModel,
    authority_resolver: WorkflowAuthorityResolver,
) -> WorkflowFact:
    """Translate one event using immutable authority records, never UI payloads."""

    if not isinstance(event, EventJournalEntry):
        raise TypeError("event must be EventJournalEntry")
    if event.case_id != current.head.case_id:
        raise WorkflowJournalAdapterError(
            "journal event belongs to another Workflow case"
        )
    policy = _EVENT_POLICIES.get(event.event_type)
    if policy is None:
        raise WorkflowJournalEventUnsupported(
            f"journal event type {event.event_type!r} has no Workflow policy"
        )
    if policy is _EventPolicy.NO_CHANGE:
        return _no_change(event)

    authority = authority_resolver.resolve_workflow_event_authority(
        event
    )
    if policy is _EventPolicy.ACCEPT_PLAN:
        return _accepted_plan_fact(
            event,
            _require_authority(authority, AcceptedPlanAuthority),
        )
    if policy in {
        _EventPolicy.SUPERSEDE_QUESTION,
        _EventPolicy.SUPERSEDE_FRAME,
    }:
        return _supersession_fact(
            event,
            current=current,
            authority=_require_authority(
                authority,
                SupersedingAuthority,
            ),
            policy=policy,
        )
    if policy is _EventPolicy.DISPATCH:
        return _dispatch_fact(
            event,
            _require_authority(authority, DispatchAuthority),
        )
    if policy is _EventPolicy.CHECKPOINT:
        return _checkpoint_fact(
            event,
            current=current,
            authority=_require_authority(
                authority,
                CheckpointAuthority,
            ),
        )
    if policy is _EventPolicy.SATISFACTION:
        return _satisfaction_fact(
            event,
            current=current,
            authority=_require_authority(
                authority,
                SatisfactionAuthority,
            ),
        )
    if policy is _EventPolicy.ANSWER:
        return _answer_fact(
            event,
            _require_authority(authority, AnswerAuthority),
        )
    raise WorkflowJournalEventUnsupported(
        f"journal event policy {policy!r} is unhandled"
    )


def validate_workflow_event_policy_coverage() -> None:
    if set(_EVENT_POLICIES) != set(JournalEventType):
        missing = set(JournalEventType) - set(_EVENT_POLICIES)
        extra = set(_EVENT_POLICIES) - set(JournalEventType)
        raise WorkflowJournalEventUnsupported(
            "Workflow event policy is not closed: "
            f"missing={sorted(item.value for item in missing)}, "
            f"extra={sorted(str(item) for item in extra)}"
        )


def _accepted_plan_fact(
    event: EventJournalEntry,
    authority: AcceptedPlanAuthority,
) -> PlanAcceptedFact:
    plan = authority.plan
    adoption = authority.adoption
    if (
        event.authority_ref != plan.plan_revision_id
        or event.case_id != plan.case_id
        or adoption.case_id != plan.case_id
        or adoption.plan_revision_id != plan.plan_revision_id
        or adoption.frame_revision_id != plan.frame_revision_id
        or authority.frame.frame_revision_id != plan.frame_revision_id
        or authority.frame.question_revision_id
        != adoption.question_revision_id
        or authority.question.question_revision_id
        != adoption.question_revision_id
        or adoption.plan_content_sha256 != plan.content_sha256
        or adoption.frame_content_sha256
        != authority.frame.content_sha256
        or adoption.authority_snapshot.accepted_question_revision_id
        != adoption.question_revision_id
        or adoption.authority_snapshot.accepted_frame_revision_id
        != adoption.frame_revision_id
        or adoption.authority_snapshot.accepted_plan_revision_id
        != plan.prior_plan_revision_id
    ):
        raise WorkflowJournalAdapterError(
            "accepted Plan authority chain is inconsistent"
        )
    obligation_by_id = {
        item.obligation_id: item for item in authority.obligations
    }
    if (
        len(obligation_by_id) != len(authority.obligations)
        or tuple(item.obligation_id for item in authority.obligations)
        != adoption.obligation_ids
        or tuple(item.content_sha256 for item in authority.obligations)
        != adoption.obligation_content_sha256s
    ):
        raise WorkflowJournalAdapterError(
            "accepted Plan obligations differ from adoption"
        )
    adopted_task_obligations = tuple(
        obligation_id
        for task in plan.tasks
        for obligation_id in task.obligation_ids
    )
    if set(adopted_task_obligations) != set(adoption.obligation_ids):
        raise WorkflowJournalAdapterError(
            "accepted Plan task ownership differs from adoption"
        )
    _require_payload(
        event,
        {
            "revision_number": plan.revision_number,
            "content_sha256": plan.content_sha256,
            "plan_adoption_id": adoption.plan_adoption_id,
            "plan_adoption_sha256": adoption.content_sha256,
            "query_binding_ids": adoption.query_binding_ids,
            "head_version": adoption.expected_head_version + 1,
        },
    )
    tasks = tuple(
        WorkflowTaskDefinition(
            task_id=task.task_id,
            business_label=task.business_purpose,
        )
        for task in plan.tasks
    )
    purpose_by_obligation = {
        obligation_id: task.business_purpose
        for task in plan.tasks
        for obligation_id in task.obligation_ids
    }
    task_by_obligation = {
        obligation_id: task.task_id
        for task in plan.tasks
        for obligation_id in task.obligation_ids
    }
    obligations = tuple(
        WorkflowObligationDefinition(
            obligation_id=obligation.obligation_id,
            task_id=task_by_obligation[obligation.obligation_id],
            business_label=purpose_by_obligation[
                obligation.obligation_id
            ],
        )
        for obligation in authority.obligations
    )
    return PlanAcceptedFact(
        case_id=event.case_id,
        cursor=event.cursor,
        source_event_id=event.event_id,
        source_event_sha256=journal_event_sha256(event),
        question_revision_id=authority.question.question_revision_id,
        question_content_sha256=authority.question.content_sha256,
        frame_revision_id=authority.frame.frame_revision_id,
        frame_content_sha256=authority.frame.content_sha256,
        plan_revision_id=plan.plan_revision_id,
        prior_plan_revision_id=plan.prior_plan_revision_id,
        plan_content_sha256=plan.content_sha256,
        plan_adoption_id=adoption.plan_adoption_id,
        plan_adoption_sha256=adoption.content_sha256,
        tasks=tasks,
        obligations=obligations,
    )


def _supersession_fact(
    event: EventJournalEntry,
    *,
    current: WorkflowReadModel,
    authority: SupersedingAuthority,
    policy: _EventPolicy,
) -> WorkflowFact:
    if policy is _EventPolicy.SUPERSEDE_QUESTION:
        revision = authority.question
        if revision is None or authority.frame is not None:
            raise WorkflowJournalAdapterError(
                "question event lacks its Question authority"
            )
        revision_id = revision.question_revision_id
        revision_hash = revision.content_sha256
        revision_number = revision.revision_number
        extra_payload = {"analysis_cycle_id": revision.analysis_cycle_id}
        expected_head_version = revision.accepted_head_version
    else:
        revision = authority.frame
        if revision is None or authority.question is not None:
            raise WorkflowJournalAdapterError(
                "Frame event lacks its Frame authority"
            )
        revision_id = revision.frame_revision_id
        revision_hash = revision.content_sha256
        revision_number = revision.revision_number
        extra_payload = {}
        expected_head_version = event.payload.get("head_version")
    if event.authority_ref != revision_id or revision.case_id != event.case_id:
        raise WorkflowJournalAdapterError(
            "superseding revision does not bind its journal event"
        )
    expected_payload = {
        "revision_number": revision_number,
        "content_sha256": revision_hash,
        **extra_payload,
    }
    if (
        not isinstance(expected_head_version, int)
        or expected_head_version < 1
    ):
        raise WorkflowJournalAdapterError(
            "authority acceptance event lacks a valid head version"
        )
    expected_payload["head_version"] = expected_head_version
    _require_payload(event, expected_payload)
    snapshot = current.snapshot
    if snapshot.case.active_plan_revision_id is None:
        return _no_change(event)
    if (
        snapshot.accepted_plan_revision_id is None
        or snapshot.accepted_plan_content_sha256 is None
        or snapshot.accepted_plan_adoption_id is None
        or snapshot.accepted_plan_adoption_sha256 is None
    ):
        raise WorkflowJournalAdapterError(
            "active Workflow Plan lacks accepted authority identity"
        )
    return AuthoritySupersededFact(
        case_id=event.case_id,
        cursor=event.cursor,
        source_event_id=event.event_id,
        source_event_sha256=journal_event_sha256(event),
        superseded_plan_revision_id=(
            snapshot.accepted_plan_revision_id
        ),
        superseded_plan_content_sha256=(
            snapshot.accepted_plan_content_sha256
        ),
        superseded_plan_adoption_id=(
            snapshot.accepted_plan_adoption_id
        ),
        superseded_plan_adoption_sha256=(
            snapshot.accepted_plan_adoption_sha256
        ),
        superseding_authority_revision_id=revision_id,
        superseding_authority_content_sha256=revision_hash,
    )


def _dispatch_fact(
    event: EventJournalEntry,
    authority: DispatchAuthority,
) -> TaskExecutionFact:
    schedule = authority.schedule
    dispatch = authority.dispatch
    sealed = dispatch.dispatch
    if (
        event.authority_ref != dispatch.dispatch_record_id
        or event.case_id != schedule.case_id
        or dispatch.schedule_id != schedule.schedule_id
        or sealed.plan_revision_id != schedule.plan_revision_id
        or sealed.authority_snapshot != schedule.authority_snapshot
    ):
        raise WorkflowJournalAdapterError(
            "obligation dispatch changes its schedule authority"
        )
    _require_payload(
        event,
        {
            "schedule_id": schedule.schedule_id,
            "obligation_id": sealed.obligation_id,
            "plan_revision_id": schedule.plan_revision_id,
            "task_id": sealed.task_id,
            "query_binding_id": sealed.query_binding_id,
            "dispatch_id": sealed.dispatch_id,
            "outbox_message_id": dispatch.outbox_message_id,
        },
    )
    return TaskExecutionFact(
        case_id=event.case_id,
        cursor=event.cursor,
        source_event_id=event.event_id,
        source_event_sha256=journal_event_sha256(event),
        plan_revision_id=schedule.plan_revision_id,
        task_id=sealed.task_id,
        state=ExecutionState.RUNNING,
    )


def _checkpoint_fact(
    event: EventJournalEntry,
    *,
    current: WorkflowReadModel,
    authority: CheckpointAuthority,
) -> WorkflowFact:
    schedule = authority.schedule
    checkpoint = authority.checkpoint
    if (
        event.authority_ref != checkpoint.checkpoint_id
        or event.case_id != schedule.case_id
        or checkpoint.schedule_id != schedule.schedule_id
        or checkpoint.schedule_sha256 != schedule.content_sha256
        or checkpoint.authority_snapshot_sha256
        != schedule.authority_snapshot_sha256
    ):
        raise WorkflowJournalAdapterError(
            "obligation checkpoint changes its schedule authority"
        )
    _require_payload(
        event,
        {
            "schedule_id": schedule.schedule_id,
            "checkpoint_id": checkpoint.checkpoint_id,
            "checkpoint_number": checkpoint.checkpoint_number,
            "dispatched": checkpoint.dispatched_obligation_ids,
            "completed": checkpoint.completed_obligation_ids,
            "pending": checkpoint.pending_obligation_ids,
        },
    )
    completion_by_obligation = {
        item.completion.obligation_id: item
        for item in authority.completions
        if item.completion.obligation_id
        in checkpoint.completed_obligation_ids
    }
    if set(completion_by_obligation) != set(
        checkpoint.completed_obligation_ids
    ):
        raise WorkflowJournalAdapterError(
            "checkpoint completed set lacks immutable completion records"
        )
    if any(
        item.schedule_id != schedule.schedule_id
        for item in completion_by_obligation.values()
    ):
        raise WorkflowJournalAdapterError(
            "checkpoint completion belongs to another schedule"
        )
    updates = []
    for task in current.snapshot.tasks:
        if task.plan_revision_id != schedule.plan_revision_id:
            continue
        task_definition = next(
            (
                item
                for item in _schedule_task_definitions(schedule)
                if item[0] == task.task_id
            ),
            None,
        )
        if task_definition is None:
            raise WorkflowJournalAdapterError(
                "schedule lacks a Workflow task binding"
            )
        obligation_ids = task_definition[1]
        completed = set(checkpoint.completed_obligation_ids)
        dispatched = set(checkpoint.dispatched_obligation_ids)
        if set(obligation_ids) <= completed:
            statuses = {
                completion_by_obligation[item].completion.status
                for item in obligation_ids
            }
            if ObligationTerminalStatus.SUPERSEDED in statuses:
                state = ExecutionState.SUPERSEDED
            elif ObligationTerminalStatus.FAILED in statuses:
                state = ExecutionState.FAILED
            else:
                state = ExecutionState.SUCCEEDED
        elif set(obligation_ids) & (completed | dispatched):
            state = ExecutionState.RUNNING
        else:
            continue
        updates.append(
            TaskExecutionUpdate(task_id=task.task_id, state=state)
        )
    if not updates:
        return _no_change(event)
    return TaskExecutionBatchFact(
        case_id=event.case_id,
        cursor=event.cursor,
        source_event_id=event.event_id,
        source_event_sha256=journal_event_sha256(event),
        plan_revision_id=schedule.plan_revision_id,
        updates=tuple(updates),
    )


def _satisfaction_fact(
    event: EventJournalEntry,
    *,
    current: WorkflowReadModel,
    authority: SatisfactionAuthority,
) -> WorkflowFact:
    record = authority.satisfaction
    if (
        event.authority_ref != record.obligation_satisfaction_id
        or event.payload
        != freeze_json({"content_sha256": record.content_sha256})
    ):
        raise WorkflowJournalAdapterError(
            "satisfaction event does not bind its authority record"
        )
    projection = next(
        (
            item
            for item in current.snapshot.obligations
            if item.obligation_id == record.obligation_id
        ),
        None,
    )
    if projection is None:
        raise WorkflowJournalAdapterError(
            "satisfaction targets an unknown Workflow obligation"
        )
    state = {
        ObligationSatisfactionStatus.OPEN: ObligationState.OPEN,
        ObligationSatisfactionStatus.SATISFIED: (
            ObligationState.SATISFIED
        ),
        ObligationSatisfactionStatus.BOUNDARY: ObligationState.BOUNDARY,
        ObligationSatisfactionStatus.BLOCKED: ObligationState.BLOCKED,
        ObligationSatisfactionStatus.SUPERSEDED: (
            ObligationState.SUPERSEDED
        ),
    }[record.status]
    return ObligationDispositionFact(
        case_id=event.case_id,
        cursor=event.cursor,
        source_event_id=event.event_id,
        source_event_sha256=journal_event_sha256(event),
        plan_revision_id=projection.plan_revision_id,
        obligation_id=record.obligation_id,
        state=state,
    )


def _answer_fact(
    event: EventJournalEntry,
    authority: AnswerAuthority,
) -> PublicationDispositionFact:
    answer = authority.answer
    if (
        event.authority_ref != answer.answer_version_id
        or event.case_id != answer.case_id
        or answer.status is not AnswerStatus.PROVISIONAL
    ):
        raise WorkflowJournalAdapterError(
            "Answer event does not bind a provisional Answer"
        )
    _require_payload(
        event,
        {
            "version_number": answer.version_number,
            "status": answer.status.value,
            "content_sha256": answer.content_sha256,
            "head_version": answer.accepted_head_version + 1,
        },
    )
    return PublicationDispositionFact(
        case_id=event.case_id,
        cursor=event.cursor,
        source_event_id=event.event_id,
        source_event_sha256=journal_event_sha256(event),
        state=PublicationState.PROVISIONAL,
        answer_version_id=answer.answer_version_id,
    )


def _schedule_task_definitions(
    schedule: ObligationScheduleRecord,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    obligation_ids_by_task: dict[str, list[str]] = {}
    for binding in schedule.plan_bindings:
        obligation_ids_by_task.setdefault(binding.task_id, []).append(
            binding.obligation_id
        )
    return tuple(
        (task_id, tuple(sorted(obligation_ids)))
        for task_id, obligation_ids in sorted(
            obligation_ids_by_task.items()
        )
    )


def _no_change(event: EventJournalEntry) -> WorkflowNoChangeFact:
    return WorkflowNoChangeFact(
        case_id=event.case_id,
        cursor=event.cursor,
        source_event_id=event.event_id,
        source_event_sha256=journal_event_sha256(event),
    )


def _require_payload(
    event: EventJournalEntry,
    expected: dict[str, object],
) -> None:
    if event.payload != freeze_json(expected):
        raise WorkflowJournalAdapterError(
            f"{event.event_type.value} event payload differs from authority"
        )


def _require_authority(
    authority: WorkflowEventAuthority,
    expected_type: type,
):
    if not isinstance(authority, expected_type):
        raise WorkflowJournalAdapterError(
            f"journal event lacks {expected_type.__name__}"
        )
    return authority


validate_workflow_event_policy_coverage()
