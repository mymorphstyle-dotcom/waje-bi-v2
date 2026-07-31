"""Plan-bound obligation fan-out and fan-in rules for the durable runtime."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Mapping

from .async_runtime import AsyncJobKind, AuthoritySnapshot
from .canonical import (
    content_sha256,
    freeze_json,
    require_aware_datetime,
    require_nonempty,
    require_sha256,
    to_jsonable,
)
from .measurement import (
    ObligationExecutionDisposition,
    ResolvedEvidenceObligation,
)
from .authority import WorkPlanRevision
from .planning import PlanAdoptionRecord, QueryBindingEnvelope
from .runtime_state import OutboxMessage


OBLIGATION_JOB_CONTRACT_REF = (
    "waje-vnext://runtime/resolved-evidence-obligation-job.v1"
)
OBLIGATION_JOB_DESTINATION = "obligation-worker"


class ObligationTerminalStatus(StrEnum):
    EXECUTION_SUCCEEDED = "execution_succeeded"
    TYPED_BOUNDARY = "typed_boundary"
    FAILED = "failed"
    SUPERSEDED = "superseded"


def same_obligation_business_authority(
    expected: AuthoritySnapshot,
    current: AuthoritySnapshot,
) -> bool:
    return all(
        getattr(expected, field_name) == getattr(current, field_name)
        for field_name in (
            "case_id",
            "mailbox_authority_epoch",
            "accepted_question_revision_id",
            "accepted_frame_revision_id",
            "accepted_plan_revision_id",
            "active_frame_candidate_generation",
            "active_frame_candidate_sha256",
        )
    )


@dataclass(frozen=True, slots=True)
class ObligationDependency:
    obligation_id: str
    depends_on_obligation_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        require_nonempty(self.obligation_id, "obligation_id")
        _require_string_tuple(
            self.depends_on_obligation_ids,
            "depends_on_obligation_ids",
        )
        if self.obligation_id in self.depends_on_obligation_ids:
            raise ValueError("obligation cannot depend on itself")


@dataclass(frozen=True, slots=True)
class ObligationPlanBinding:
    obligation_id: str
    task_id: str
    query_binding_id: str | None

    def __post_init__(self) -> None:
        require_sha256(self.obligation_id, "obligation_id")
        require_sha256(self.task_id, "task_id")
        if self.query_binding_id is not None:
            require_sha256(
                self.query_binding_id,
                "query_binding_id",
            )


@dataclass(frozen=True, slots=True)
class ObligationDispatch:
    dispatch_id: str
    obligation_id: str
    plan_revision_id: str
    task_id: str
    query_binding_id: str
    obligation_definition_sha256: str
    authority_snapshot: AuthoritySnapshot
    authority_snapshot_sha256: str

    def __post_init__(self) -> None:
        for field_name in (
            "dispatch_id",
            "obligation_id",
            "task_id",
            "query_binding_id",
        ):
            require_sha256(getattr(self, field_name), field_name)
        require_nonempty(
            self.plan_revision_id,
            "plan_revision_id",
        )
        require_sha256(
            self.obligation_definition_sha256,
            "obligation_definition_sha256",
        )
        require_sha256(
            self.authority_snapshot_sha256,
            "authority_snapshot_sha256",
        )
        if (
            self.authority_snapshot.content_sha256
            != self.authority_snapshot_sha256
        ):
            raise ValueError("dispatch authority snapshot hash is stale")


@dataclass(frozen=True, slots=True)
class ObligationCompletion:
    """Terminal execution receipt.

    ``EXECUTION_SUCCEEDED`` confirms that the dispatched work produced a
    result. It does not satisfy the Evidence obligation. Evidence admission
    owns that later transition.
    """

    obligation_id: str
    dispatch_id: str
    status: ObligationTerminalStatus
    result_sha256: str

    def __post_init__(self) -> None:
        for field_name in ("obligation_id", "dispatch_id"):
            require_nonempty(getattr(self, field_name), field_name)
        if not isinstance(self.status, ObligationTerminalStatus):
            raise TypeError("status must be ObligationTerminalStatus")
        require_sha256(self.result_sha256, "result_sha256")


@dataclass(frozen=True, slots=True)
class ObligationScheduleRecord:
    schedule_id: str
    case_id: str
    correlation_id: str
    frame_revision_id: str
    plan_revision_id: str
    plan_adoption_id: str
    plan_adoption_content_sha256: str
    obligations: tuple[ResolvedEvidenceObligation, ...]
    plan_bindings: tuple[ObligationPlanBinding, ...]
    dependencies: tuple[ObligationDependency, ...]
    authority_snapshot: AuthoritySnapshot
    authority_snapshot_sha256: str
    created_at: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "schedule_id",
            "case_id",
            "correlation_id",
            "frame_revision_id",
            "plan_revision_id",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        require_sha256(self.plan_adoption_id, "plan_adoption_id")
        require_sha256(
            self.plan_adoption_content_sha256,
            "plan_adoption_content_sha256",
        )
        if not self.obligations:
            raise ValueError("obligation schedule requires obligations")
        _validate_graph(
            obligations=self.obligations,
            dependencies=self.dependencies,
        )
        if not isinstance(self.plan_bindings, tuple):
            raise TypeError("plan_bindings must be a tuple")
        binding_by_id = {
            item.obligation_id: item for item in self.plan_bindings
        }
        if len(binding_by_id) != len(self.plan_bindings):
            raise ValueError("plan obligation bindings must be unique")
        if set(binding_by_id) != {
            item.obligation_id for item in self.obligations
        }:
            raise ValueError(
                "schedule must bind every adopted obligation"
            )
        for obligation in self.obligations:
            binding = binding_by_id[obligation.obligation_id]
            if (
                obligation.execution_disposition
                is ObligationExecutionDisposition.EXECUTABLE
            ) != (binding.query_binding_id is not None):
                raise ValueError(
                    "query binding presence must follow disposition"
                )
        if any(
            item.case_id != self.case_id
            or item.frame_revision_id != self.frame_revision_id
            for item in self.obligations
        ):
            raise ValueError(
                "obligation schedule crosses case or Frame authority"
            )
        if (
            self.authority_snapshot.case_id != self.case_id
            or self.authority_snapshot.accepted_frame_revision_id
            != self.frame_revision_id
            or self.authority_snapshot.accepted_plan_revision_id
            != self.plan_revision_id
        ):
            raise ValueError(
                "obligation schedule authority does not accept its Frame"
            )
        require_sha256(
            self.authority_snapshot_sha256,
            "authority_snapshot_sha256",
        )
        if (
            self.authority_snapshot.content_sha256
            != self.authority_snapshot_sha256
        ):
            raise ValueError("obligation schedule snapshot hash is stale")
        require_aware_datetime(self.created_at, "created_at")

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)


@dataclass(frozen=True, slots=True)
class ObligationDispatchRecord:
    dispatch_record_id: str
    schedule_id: str
    outbox_message_id: str
    dispatch: ObligationDispatch
    created_at: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "dispatch_record_id",
            "schedule_id",
            "outbox_message_id",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        if not isinstance(self.dispatch, ObligationDispatch):
            raise TypeError("dispatch must be ObligationDispatch")
        require_aware_datetime(self.created_at, "created_at")


def validate_obligation_dispatch_admission(
    *,
    schedule: ObligationScheduleRecord,
    record: ObligationDispatchRecord,
    message: OutboxMessage,
) -> None:
    """Validate the complete schedule -> dispatch -> outbox authority chain."""

    obligation = next(
        (
            item
            for item in schedule.obligations
            if item.obligation_id == record.dispatch.obligation_id
        ),
        None,
    )
    plan_binding = next(
        (
            item
            for item in schedule.plan_bindings
            if item.obligation_id == record.dispatch.obligation_id
        ),
        None,
    )
    if obligation is None or plan_binding is None:
        raise ValueError(
            "obligation dispatch is outside the accepted schedule"
        )
    expected_dispatch = build_obligation_dispatch(
        obligation=obligation,
        plan_binding=plan_binding,
        plan_revision_id=schedule.plan_revision_id,
        current_authority=schedule.authority_snapshot,
    )
    expected_payload = {
        "schedule_id": schedule.schedule_id,
        "obligation_id": obligation.obligation_id,
        "plan_revision_id": schedule.plan_revision_id,
        "task_id": expected_dispatch.task_id,
        "query_binding_id": expected_dispatch.query_binding_id,
        "obligation": to_jsonable(obligation),
        "dispatch_id": expected_dispatch.dispatch_id,
    }
    if (
        record.schedule_id != schedule.schedule_id
        or record.dispatch != expected_dispatch
        or record.outbox_message_id != message.outbox_message_id
        or record.created_at != message.created_at
        or message.case_id != schedule.case_id
        or message.action_id is not None
        or message.job_kind is not AsyncJobKind.OBLIGATION
        or message.destination != OBLIGATION_JOB_DESTINATION
        or message.contract_ref != OBLIGATION_JOB_CONTRACT_REF
        or message.authority_snapshot != schedule.authority_snapshot
        or message.authority_snapshot_sha256
        != schedule.authority_snapshot_sha256
        or message.expected_head_version
        != schedule.authority_snapshot.head_version
        or message.expected_authority_epoch
        != schedule.authority_snapshot.mailbox_authority_epoch
        or message.operation.authority_revision
        != schedule.authority_snapshot.mailbox_authority_epoch
        or message.operation.correlation_id != schedule.correlation_id
        or message.payload != freeze_json(expected_payload)
    ):
        raise ValueError(
            "obligation dispatch does not exactly bind schedule outbox"
        )


@dataclass(frozen=True, slots=True)
class ObligationCompletionRecord:
    completion_record_id: str
    schedule_id: str
    completion: ObligationCompletion
    admitted_authority_snapshot_sha256: str
    created_at: datetime

    def __post_init__(self) -> None:
        for field_name in ("completion_record_id", "schedule_id"):
            require_nonempty(getattr(self, field_name), field_name)
        if not isinstance(self.completion, ObligationCompletion):
            raise TypeError("completion must be ObligationCompletion")
        require_sha256(
            self.admitted_authority_snapshot_sha256,
            "admitted_authority_snapshot_sha256",
        )
        require_aware_datetime(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class ObligationScheduleCheckpoint:
    checkpoint_id: str
    schedule_id: str
    checkpoint_number: int
    prior_checkpoint_id: str | None
    schedule_sha256: str
    dispatched_obligation_ids: tuple[str, ...]
    completed_obligation_ids: tuple[str, ...]
    pending_obligation_ids: tuple[str, ...]
    authority_snapshot_sha256: str
    created_at: datetime

    def __post_init__(self) -> None:
        for field_name in ("checkpoint_id", "schedule_id"):
            require_nonempty(getattr(self, field_name), field_name)
        if self.checkpoint_number < 1:
            raise ValueError("checkpoint_number must be positive")
        if self.checkpoint_number == 1:
            if self.prior_checkpoint_id is not None:
                raise ValueError(
                    "first obligation checkpoint cannot have a prior"
                )
        elif self.prior_checkpoint_id is None:
            raise ValueError(
                "later obligation checkpoint requires a prior"
            )
        for field_name in (
            "schedule_sha256",
            "authority_snapshot_sha256",
        ):
            require_sha256(getattr(self, field_name), field_name)
        for field_name in (
            "dispatched_obligation_ids",
            "completed_obligation_ids",
            "pending_obligation_ids",
        ):
            _require_string_tuple(
                getattr(self, field_name),
                field_name,
            )
        sets = (
            set(self.dispatched_obligation_ids),
            set(self.completed_obligation_ids),
            set(self.pending_obligation_ids),
        )
        if any(
            sets[index] & sets[other]
            for index in range(len(sets))
            for other in range(index + 1, len(sets))
        ):
            raise ValueError(
                "obligation checkpoint states must be disjoint"
            )
        require_aware_datetime(self.created_at, "created_at")


def validate_schedule_plan_binding(
    *,
    schedule: ObligationScheduleRecord,
    plan: WorkPlanRevision,
    adoption: PlanAdoptionRecord,
    query_bindings: tuple[QueryBindingEnvelope, ...],
    obligations: tuple[ResolvedEvidenceObligation, ...],
) -> None:
    """Replay the schedule from the accepted Plan adoption."""

    if (
        schedule.plan_revision_id != plan.plan_revision_id
        or schedule.frame_revision_id != plan.frame_revision_id
        or adoption.plan_revision_id != plan.plan_revision_id
        or schedule.plan_adoption_id != adoption.plan_adoption_id
        or schedule.plan_adoption_content_sha256
        != adoption.content_sha256
    ):
        raise ValueError("schedule does not bind accepted Plan adoption")
    obligation_by_id = {
        item.obligation_id: item for item in obligations
    }
    if tuple(item.obligation_id for item in schedule.obligations) != (
        adoption.obligation_ids
    ):
        raise ValueError("schedule obligation order differs from adoption")
    if any(
        obligation_by_id.get(item.obligation_id) != item
        for item in schedule.obligations
    ):
        raise ValueError("schedule carries forged obligation content")
    task_by_obligation = {
        obligation_id: task
        for task in plan.tasks
        for obligation_id in task.obligation_ids
    }
    query_by_obligation = {
        item.obligation_id: item for item in query_bindings
    }
    expected_bindings = tuple(
        ObligationPlanBinding(
            obligation_id=obligation.obligation_id,
            task_id=task_by_obligation[
                obligation.obligation_id
            ].task_id,
            query_binding_id=(
                None
                if obligation.execution_disposition
                is not ObligationExecutionDisposition.EXECUTABLE
                else query_by_obligation[
                    obligation.obligation_id
                ].query_binding_id
            ),
        )
        for obligation in schedule.obligations
    )
    if schedule.plan_bindings != expected_bindings:
        raise ValueError("schedule task/query bindings were not Plan-derived")
    obligations_by_task = {
        task.task_id: task.obligation_ids for task in plan.tasks
    }
    expected_dependencies = tuple(
        ObligationDependency(
            obligation_id=obligation.obligation_id,
            depends_on_obligation_ids=tuple(
                dependency_obligation_id
                for dependency_task_id in task_by_obligation[
                    obligation.obligation_id
                ].depends_on_task_ids
                for dependency_obligation_id in obligations_by_task[
                    dependency_task_id
                ]
            ),
        )
        for obligation in schedule.obligations
    )
    if schedule.dependencies != expected_dependencies:
        raise ValueError("schedule dependencies were not Plan-derived")


def select_runnable_obligations(
    *,
    obligations: tuple[ResolvedEvidenceObligation, ...],
    dependencies: tuple[ObligationDependency, ...],
    completions: tuple[ObligationCompletion, ...],
    current_authority: AuthoritySnapshot,
) -> tuple[ResolvedEvidenceObligation, ...]:
    """Return every currently runnable independent obligation."""

    obligation_by_id = _validate_graph(
        obligations=obligations,
        dependencies=dependencies,
    )
    if any(
        obligation.case_id != current_authority.case_id
        or obligation.frame_revision_id
        != current_authority.accepted_frame_revision_id
        for obligation in obligations
    ):
        raise ValueError("obligation graph is stale for accepted authority")
    completion_by_id = _completion_index(completions)
    dependency_by_id = {
        item.obligation_id: item.depends_on_obligation_ids
        for item in dependencies
    }
    return tuple(
        obligation
        for obligation in obligations
        if obligation.obligation_id not in completion_by_id
        and obligation.execution_disposition
        is ObligationExecutionDisposition.EXECUTABLE
        and all(
            dependency_id in completion_by_id
            and completion_by_id[dependency_id].status
            is ObligationTerminalStatus.EXECUTION_SUCCEEDED
            for dependency_id in dependency_by_id[
                obligation.obligation_id
            ]
        )
        and obligation.obligation_id in obligation_by_id
    )


def build_obligation_dispatch(
    *,
    obligation: ResolvedEvidenceObligation,
    plan_binding: ObligationPlanBinding,
    plan_revision_id: str,
    current_authority: AuthoritySnapshot,
) -> ObligationDispatch:
    if (
        obligation.case_id != current_authority.case_id
        or obligation.frame_revision_id
        != current_authority.accepted_frame_revision_id
    ):
        raise ValueError("cannot dispatch a stale obligation")
    if (
        current_authority.accepted_plan_revision_id
        != plan_revision_id
        or plan_binding.obligation_id != obligation.obligation_id
        or plan_binding.query_binding_id is None
    ):
        raise ValueError(
            "dispatch requires accepted Plan task and query binding"
        )
    definition_sha256 = content_sha256(obligation)
    dispatch_id = content_sha256(
        {
            "kind": "obligation-dispatch.v1",
            "obligation_id": obligation.obligation_id,
            "plan_revision_id": plan_revision_id,
            "task_id": plan_binding.task_id,
            "query_binding_id": plan_binding.query_binding_id,
            "definition_sha256": definition_sha256,
            "authority_snapshot_sha256": current_authority.content_sha256,
        }
    )
    return ObligationDispatch(
        dispatch_id=dispatch_id,
        obligation_id=obligation.obligation_id,
        plan_revision_id=plan_revision_id,
        task_id=plan_binding.task_id,
        query_binding_id=plan_binding.query_binding_id,
        obligation_definition_sha256=definition_sha256,
        authority_snapshot=current_authority,
        authority_snapshot_sha256=current_authority.content_sha256,
    )


def admit_obligation_completion(
    *,
    dispatch: ObligationDispatch,
    obligation: ResolvedEvidenceObligation,
    status: ObligationTerminalStatus,
    result_sha256: str,
    current_authority: AuthoritySnapshot,
    prior_completions: tuple[ObligationCompletion, ...],
) -> tuple[ObligationCompletion, ...]:
    """Admit an idempotent terminal result under a sibling-tolerant fence."""

    if (
        obligation.execution_disposition
        is not ObligationExecutionDisposition.EXECUTABLE
    ):
        raise ValueError(
            "only executable obligations accept worker completion"
        )
    if status not in {
        ObligationTerminalStatus.EXECUTION_SUCCEEDED,
        ObligationTerminalStatus.FAILED,
    }:
        raise ValueError(
            "worker completion status is incompatible with executable "
            "obligation"
        )
    if (
        dispatch.obligation_id != obligation.obligation_id
        or dispatch.obligation_definition_sha256
        != content_sha256(obligation)
    ):
        raise ValueError("completion changes obligation identity")
    _assert_completion_authority(
        expected=dispatch.authority_snapshot,
        current=current_authority,
    )
    candidate = ObligationCompletion(
        obligation_id=obligation.obligation_id,
        dispatch_id=dispatch.dispatch_id,
        status=status,
        result_sha256=result_sha256,
    )
    by_id = _completion_index(prior_completions)
    prior = by_id.get(candidate.obligation_id)
    if prior is not None:
        if prior != candidate:
            raise ValueError(
                "obligation already has a different terminal completion"
            )
        return prior_completions
    return prior_completions + (candidate,)


def propagate_dependency_terminals(
    *,
    obligations: tuple[ResolvedEvidenceObligation, ...],
    dependencies: tuple[ObligationDependency, ...],
    completions: tuple[ObligationCompletion, ...],
) -> tuple[ObligationCompletion, ...]:
    """Close every dependent whose prerequisite cannot complete execution."""

    _validate_graph(
        obligations=obligations,
        dependencies=dependencies,
    )
    completion_by_id = _completion_index(completions)
    dependency_by_id = {
        item.obligation_id: item.depends_on_obligation_ids
        for item in dependencies
    }
    changed = True
    while changed:
        changed = False
        for obligation in obligations:
            obligation_id = obligation.obligation_id
            if obligation_id in completion_by_id:
                continue
            prerequisite_completions = tuple(
                completion_by_id.get(dependency_id)
                for dependency_id in dependency_by_id[obligation_id]
            )
            blocking = tuple(
                item
                for item in prerequisite_completions
                if item is not None
                and item.status
                is not ObligationTerminalStatus.EXECUTION_SUCCEEDED
            )
            if not blocking:
                continue
            statuses = {item.status for item in blocking}
            if ObligationTerminalStatus.SUPERSEDED in statuses:
                status = ObligationTerminalStatus.SUPERSEDED
            elif ObligationTerminalStatus.FAILED in statuses:
                status = ObligationTerminalStatus.FAILED
            else:
                status = ObligationTerminalStatus.TYPED_BOUNDARY
            completion = ObligationCompletion(
                obligation_id=obligation_id,
                dispatch_id="system-prerequisite:{}".format(obligation_id),
                status=status,
                result_sha256=content_sha256(
                    {
                        "kind": "obligation-prerequisite-terminal.v1",
                        "obligation_id": obligation_id,
                        "blocking_prerequisites": tuple(
                            sorted(
                                (
                                    item.obligation_id,
                                    item.status.value,
                                    item.result_sha256,
                                )
                                for item in blocking
                            )
                        ),
                    }
                ),
            )
            completion_by_id[obligation_id] = completion
            changed = True
    return tuple(
        completion_by_id[item.obligation_id]
        for item in obligations
        if item.obligation_id in completion_by_id
    )


def validate_persisted_obligation_completion(
    *,
    schedule: ObligationScheduleRecord,
    completion: ObligationCompletion,
    prior_completions: tuple[ObligationCompletion, ...],
    dispatch: ObligationDispatch | None,
    current_authority: AuthoritySnapshot,
) -> None:
    """Recompute the only legal worker or system completion at storage."""

    obligation = next(
        (
            item
            for item in schedule.obligations
            if item.obligation_id == completion.obligation_id
        ),
        None,
    )
    if obligation is None:
        raise ValueError("completion references an unknown obligation")

    if completion.dispatch_id.startswith("system-terminal:"):
        expected_status = (
            ObligationTerminalStatus.TYPED_BOUNDARY
            if obligation.execution_disposition
            is ObligationExecutionDisposition.TYPED_BOUNDARY
            else ObligationTerminalStatus.FAILED
        )
        expected = ObligationCompletion(
            obligation_id=obligation.obligation_id,
            dispatch_id="system-terminal:{}".format(
                obligation.obligation_id
            ),
            status=expected_status,
            result_sha256=content_sha256(
                {
                    "execution_disposition": (
                        obligation.execution_disposition.value
                    ),
                    "boundary_code": obligation.boundary_code,
                }
            ),
        )
        if (
            obligation.execution_disposition
            is ObligationExecutionDisposition.EXECUTABLE
            or completion != expected
        ):
            raise ValueError(
                "system terminal is not derived from obligation disposition"
            )
        return

    if completion.dispatch_id.startswith("system-prerequisite:"):
        propagated = propagate_dependency_terminals(
            obligations=schedule.obligations,
            dependencies=schedule.dependencies,
            completions=prior_completions,
        )
        expected = next(
            (
                item
                for item in propagated
                if item.obligation_id == completion.obligation_id
                and item.obligation_id
                not in {
                    prior.obligation_id
                    for prior in prior_completions
                }
            ),
            None,
        )
        if expected is None or completion != expected:
            raise ValueError(
                "system prerequisite terminal is not graph-derived"
            )
        return

    if completion.status is ObligationTerminalStatus.SUPERSEDED:
        if same_obligation_business_authority(
            schedule.authority_snapshot,
            current_authority,
        ):
            raise ValueError(
                "obligation supersession requires authority drift"
            )
        expected = ObligationCompletion(
            obligation_id=obligation.obligation_id,
            dispatch_id=(
                dispatch.dispatch_id
                if dispatch is not None
                else "system-superseded:{}".format(
                    obligation.obligation_id
                )
            ),
            status=ObligationTerminalStatus.SUPERSEDED,
            result_sha256=content_sha256(
                {
                    "kind": "obligation-authority-superseded.v1",
                    "schedule_authority": (
                        schedule.authority_snapshot_sha256
                    ),
                    "current_authority": (
                        current_authority.content_sha256
                    ),
                    "obligation_id": obligation.obligation_id,
                }
            ),
        )
        if completion != expected:
            raise ValueError(
                "supersession is not derived from authority drift"
            )
        return

    if dispatch is None:
        raise ValueError("worker completion lacks its durable dispatch")
    if (
        obligation.execution_disposition
        is not ObligationExecutionDisposition.EXECUTABLE
        or completion.status
        not in {
            ObligationTerminalStatus.EXECUTION_SUCCEEDED,
            ObligationTerminalStatus.FAILED,
        }
        or completion.dispatch_id != dispatch.dispatch_id
        or completion.obligation_id != dispatch.obligation_id
        or dispatch.obligation_definition_sha256
        != content_sha256(obligation)
    ):
        raise ValueError(
            "worker completion is incompatible with durable dispatch"
        )


def _assert_completion_authority(
    *,
    expected: AuthoritySnapshot,
    current: AuthoritySnapshot,
) -> None:
    identity_fields = (
        "case_id",
        "mailbox_authority_epoch",
        "accepted_question_revision_id",
        "accepted_frame_revision_id",
        "accepted_plan_revision_id",
        "active_frame_candidate_generation",
        "active_frame_candidate_sha256",
    )
    if any(
        getattr(expected, field_name) != getattr(current, field_name)
        for field_name in identity_fields
    ):
        raise ValueError("obligation completion authority is stale")
    for field_name in (
        "head_version",
        "obligation_state_version",
        "evidence_admission_state_version",
        "contradiction_state_version",
    ):
        if getattr(current, field_name) < getattr(expected, field_name):
            raise ValueError("authority state version cannot regress")


def _validate_graph(
    *,
    obligations: tuple[ResolvedEvidenceObligation, ...],
    dependencies: tuple[ObligationDependency, ...],
) -> Mapping[str, ResolvedEvidenceObligation]:
    if not obligations:
        raise ValueError("obligation graph cannot be empty")
    obligation_by_id = {
        item.obligation_id: item for item in obligations
    }
    if len(obligation_by_id) != len(obligations):
        raise ValueError("obligation IDs must be unique")
    dependency_by_id = {
        item.obligation_id: item for item in dependencies
    }
    if set(dependency_by_id) != set(obligation_by_id):
        raise ValueError("every obligation requires one dependency record")
    for dependency in dependencies:
        if not set(dependency.depends_on_obligation_ids) <= set(
            obligation_by_id
        ):
            raise ValueError("dependency references an unknown obligation")
    _assert_acyclic(dependency_by_id)
    return obligation_by_id


def _assert_acyclic(
    dependencies: Mapping[str, ObligationDependency],
) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(obligation_id: str) -> None:
        if obligation_id in visited:
            return
        if obligation_id in visiting:
            raise ValueError("obligation dependency graph contains a cycle")
        visiting.add(obligation_id)
        for dependency_id in (
            dependencies[obligation_id].depends_on_obligation_ids
        ):
            visit(dependency_id)
        visiting.remove(obligation_id)
        visited.add(obligation_id)

    for obligation_id in dependencies:
        visit(obligation_id)


def _completion_index(
    completions: tuple[ObligationCompletion, ...],
) -> dict[str, ObligationCompletion]:
    by_id: dict[str, ObligationCompletion] = {}
    for completion in completions:
        prior = by_id.get(completion.obligation_id)
        if prior is not None and prior != completion:
            raise ValueError(
                "obligation has conflicting terminal completions"
            )
        by_id[completion.obligation_id] = completion
    return by_id


def _require_string_tuple(
    value: tuple[str, ...],
    field_name: str,
) -> None:
    if not isinstance(value, tuple) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{field_name} must contain non-empty strings")
    if len(value) != len(set(value)):
        raise ValueError(f"{field_name} must be unique")
