"""The five vNext authority object families and their subordinate records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .canonical import (
    content_sha256,
    require_aware_datetime,
    require_nonempty,
)
from .measurement import AnalysisFrameRevision, QuestionRevision


class CaseLifecycle(StrEnum):
    OPEN = "open"
    WAITING_FOR_USER = "waiting_for_user"
    STOPPED = "stopped"
    CLOSED = "closed"


class ReviewerSeverity(StrEnum):
    ADVISORY = "advisory"
    BLOCKING = "blocking"


class ReviewerObjectionStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    ACCEPTED_LIMITATION = "accepted_limitation"


@dataclass(frozen=True, slots=True)
class InvestigationCase:
    case_id: str
    thread_id: str
    lifecycle: CaseLifecycle
    head_version: int
    accepted_question_revision_id: str | None
    accepted_frame_revision_id: str | None
    accepted_plan_revision_id: str | None
    accepted_answer_version_id: str | None
    analysis_cycle_id: str
    opened_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        require_nonempty(self.case_id, "case_id")
        require_nonempty(self.thread_id, "thread_id")
        require_nonempty(self.analysis_cycle_id, "analysis_cycle_id")
        _require_enum(self.lifecycle, CaseLifecycle, "lifecycle")
        if self.head_version < 0:
            raise ValueError("head_version must be non-negative")
        require_aware_datetime(self.opened_at, "opened_at")
        require_aware_datetime(self.updated_at, "updated_at")
        if self.updated_at < self.opened_at:
            raise ValueError("updated_at cannot precede opened_at")
        if self.accepted_frame_revision_id and not (
            self.accepted_question_revision_id
        ):
            raise ValueError("accepted frame requires an accepted question")
        if self.accepted_plan_revision_id and not self.accepted_frame_revision_id:
            raise ValueError("accepted plan requires an accepted frame")
        if self.accepted_answer_version_id and not self.accepted_plan_revision_id:
            raise ValueError("accepted answer requires an accepted plan")

@dataclass(frozen=True, slots=True)
class WorkTask:
    task_id: str
    proposal_task_key: str
    business_purpose: str
    capability_intent_ref: str
    target_estimand_ids: tuple[str, ...]
    obligation_ids: tuple[str, ...]
    query_binding_ids: tuple[str, ...]
    completion_spec_ids: tuple[str, ...]
    execution_success_policy_refs: tuple[str, ...]
    execution_degrade_policy_refs: tuple[str, ...]
    execution_stop_policy_refs: tuple[str, ...]
    depends_on_task_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        require_nonempty(self.task_id, "task_id")
        require_nonempty(self.proposal_task_key, "proposal_task_key")
        require_nonempty(self.business_purpose, "business_purpose")
        require_nonempty(
            self.capability_intent_ref,
            "capability_intent_ref",
        )
        if self.task_id in self.depends_on_task_ids:
            raise ValueError("task cannot depend on itself")
        _require_nonempty_members(
            self.target_estimand_ids,
            "target_estimand_ids",
        )
        _require_nonempty_members(self.obligation_ids, "obligation_ids")
        _require_nonempty_members(
            self.query_binding_ids,
            "query_binding_ids",
        )
        _require_nonempty_members(
            self.completion_spec_ids,
            "completion_spec_ids",
        )
        _require_nonempty_members(
            self.execution_success_policy_refs,
            "execution_success_policy_refs",
        )
        _require_nonempty_members(
            self.execution_degrade_policy_refs,
            "execution_degrade_policy_refs",
        )
        _require_nonempty_members(
            self.execution_stop_policy_refs,
            "execution_stop_policy_refs",
        )
        _require_nonempty_members(self.depends_on_task_ids, "depends_on_task_ids")
        if not self.obligation_ids:
            raise ValueError("work task must close at least one obligation")
        if not self.target_estimand_ids:
            raise ValueError("work task requires target estimands")
        if not self.completion_spec_ids:
            raise ValueError("work task requires completion specs")
        if not self.execution_success_policy_refs:
            raise ValueError("work task requires success policy refs")
        if not self.execution_degrade_policy_refs:
            raise ValueError("work task requires degrade policy refs")
        if not self.execution_stop_policy_refs:
            raise ValueError("work task requires stop policy refs")


@dataclass(frozen=True, slots=True)
class WorkPlanRevision:
    plan_revision_id: str
    case_id: str
    frame_revision_id: str
    revision_number: int
    prior_plan_revision_id: str | None
    created_by_action_id: str
    created_at: datetime
    revision_reason: str
    resolution_outcome_ids: tuple[str, ...]
    tasks: tuple[WorkTask, ...]

    def __post_init__(self) -> None:
        for name in (
            "plan_revision_id",
            "case_id",
            "frame_revision_id",
            "created_by_action_id",
            "revision_reason",
        ):
            require_nonempty(getattr(self, name), name)
        if self.revision_number < 1:
            raise ValueError("revision_number must be positive")
        if self.revision_number == 1 and self.prior_plan_revision_id is not None:
            raise ValueError("first plan revision cannot have a prior revision")
        if self.revision_number > 1 and not self.prior_plan_revision_id:
            raise ValueError("later plan revisions require prior_plan_revision_id")
        require_aware_datetime(self.created_at, "created_at")
        _require_nonempty_members(
            self.resolution_outcome_ids,
            "resolution_outcome_ids",
        )
        if not self.resolution_outcome_ids:
            raise ValueError(
                "work plan must adopt measurement resolution outcomes"
            )
        _require_tuple_of(self.tasks, WorkTask, "tasks")
        if not self.tasks:
            raise ValueError("work plan must contain at least one task")
        task_ids = tuple(task.task_id for task in self.tasks)
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("work plan task IDs must be unique")
        proposal_task_keys = tuple(
            task.proposal_task_key for task in self.tasks
        )
        if len(proposal_task_keys) != len(set(proposal_task_keys)):
            raise ValueError("work plan proposal task keys must be unique")
        obligation_ids = tuple(
            obligation_id
            for task in self.tasks
            for obligation_id in task.obligation_ids
        )
        if len(obligation_ids) != len(set(obligation_ids)):
            raise ValueError(
                "work plan obligations must have one closure owner"
            )
        query_binding_ids = tuple(
            query_binding_id
            for task in self.tasks
            for query_binding_id in task.query_binding_ids
        )
        if len(query_binding_ids) != len(set(query_binding_ids)):
            raise ValueError(
                "work plan query bindings must have one task owner"
            )
        known_tasks = set(task_ids)
        for task in self.tasks:
            unknown = set(task.depends_on_task_ids) - known_tasks
            if unknown:
                raise ValueError(
                    "task {!r} has unknown dependencies: {}".format(
                        task.task_id, sorted(unknown)
                    )
                )
        _require_acyclic_tasks(self.tasks)

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)


@dataclass(frozen=True, slots=True)
class InterpretationRecord:
    interpretation_id: str
    case_id: str
    frame_revision_id: str
    evidence_record_ids: tuple[str, ...]
    evidence_admission_ids: tuple[str, ...]
    evidence_validity_ids: tuple[str, ...]
    interpretation: str
    created_by_action_id: str
    created_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "interpretation_id",
            "case_id",
            "frame_revision_id",
            "interpretation",
            "created_by_action_id",
        ):
            require_nonempty(getattr(self, name), name)
        if not self.evidence_record_ids:
            raise ValueError("interpretation requires evidence")
        _require_nonempty_members(
            self.evidence_record_ids, "evidence_record_ids"
        )
        _require_nonempty_members(
            self.evidence_admission_ids,
            "evidence_admission_ids",
        )
        _require_nonempty_members(
            self.evidence_validity_ids,
            "evidence_validity_ids",
        )
        if not (
            len(self.evidence_record_ids)
            == len(self.evidence_admission_ids)
            == len(self.evidence_validity_ids)
        ):
            raise ValueError(
                "interpretation evidence authority tuples must align"
            )
        require_aware_datetime(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class DecisionOption:
    option_id: str
    label: str
    impact: str

    def __post_init__(self) -> None:
        require_nonempty(self.option_id, "option_id")
        require_nonempty(self.label, "label")
        require_nonempty(self.impact, "impact")


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    decision_record_id: str
    case_id: str
    question: str
    options: tuple[DecisionOption, ...]
    selected_option_id: str | None
    freeform_response: str | None
    source: str
    created_at: datetime

    def __post_init__(self) -> None:
        require_nonempty(self.decision_record_id, "decision_record_id")
        require_nonempty(self.case_id, "case_id")
        require_nonempty(self.question, "question")
        require_nonempty(self.source, "source")
        _require_tuple_of(self.options, DecisionOption, "options")
        if not 2 <= len(self.options) <= 3:
            raise ValueError("decision requires two or three options")
        option_ids = tuple(option.option_id for option in self.options)
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("decision option IDs must be unique")
        if (self.selected_option_id is None) == (self.freeform_response is None):
            raise ValueError(
                "decision requires exactly one selected option or freeform response"
            )
        if (
            self.selected_option_id is not None
            and self.selected_option_id not in option_ids
        ):
            raise ValueError("selected option must be present")
        if self.freeform_response is not None:
            require_nonempty(self.freeform_response, "freeform_response")
        require_aware_datetime(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class ReviewerObjection:
    objection_id: str
    objection_key: str
    revision_number: int
    prior_objection_id: str | None
    case_id: str
    answer_version_id: str
    claim_id: str
    severity: ReviewerSeverity
    status: ReviewerObjectionStatus
    risk_type: str
    evidence_gap: str
    requested_action: str
    disposition_note: str | None
    created_at: datetime
    resolved_at: datetime | None

    def __post_init__(self) -> None:
        for name in (
            "objection_id",
            "objection_key",
            "case_id",
            "answer_version_id",
            "claim_id",
            "risk_type",
            "evidence_gap",
            "requested_action",
        ):
            require_nonempty(getattr(self, name), name)
        _require_enum(self.severity, ReviewerSeverity, "severity")
        _require_enum(self.status, ReviewerObjectionStatus, "status")
        if self.revision_number < 1:
            raise ValueError("revision_number must be positive")
        if self.revision_number == 1 and self.prior_objection_id is not None:
            raise ValueError("first objection revision cannot have a prior revision")
        if self.revision_number > 1 and not self.prior_objection_id:
            raise ValueError("later objection revisions require prior_objection_id")
        require_aware_datetime(self.created_at, "created_at")
        if self.status is ReviewerObjectionStatus.OPEN:
            if self.resolved_at is not None or self.disposition_note is not None:
                raise ValueError("open objection cannot have a disposition")
        else:
            if self.resolved_at is None or not self.disposition_note:
                raise ValueError("resolved objection requires disposition and timestamp")
            require_aware_datetime(self.resolved_at, "resolved_at")
            if self.resolved_at < self.created_at:
                raise ValueError("resolved_at cannot precede created_at")

def _require_nonempty_members(values: tuple[str, ...], field_name: str) -> None:
    if not isinstance(values, tuple):
        raise TypeError("{} must be a tuple".format(field_name))
    for index, value in enumerate(values):
        if not isinstance(value, str):
            raise TypeError("{}[{}] must be a string".format(field_name, index))
        require_nonempty(value, "{}[{}]".format(field_name, index))


def _require_tuple_of(
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


def _require_enum(
    value: object,
    expected_type: type[object],
    field_name: str,
) -> None:
    if not isinstance(value, expected_type):
        raise TypeError(
            "{} must be {}".format(field_name, expected_type.__name__)
        )


def _require_acyclic_tasks(tasks: tuple[WorkTask, ...]) -> None:
    dependencies = {
        task.task_id: set(task.depends_on_task_ids)
        for task in tasks
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visited:
            return
        if task_id in visiting:
            raise ValueError("work plan task dependencies must be acyclic")
        visiting.add(task_id)
        for dependency in dependencies[task_id]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in dependencies:
        visit(task_id)
