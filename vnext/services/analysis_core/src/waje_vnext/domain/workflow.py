"""Pure Gate 3 Workflow read model.

The journal adapter owns translation from durable business events into the
closed ``WorkflowFact`` union below.  This module consumes only those facts;
free-form event ``customer_projection`` payloads never participate in the
reduction.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum

from .canonical import (
    FrozenJson,
    content_sha256,
    freeze_json,
    require_nonempty,
    require_sha256,
)
from .evidence import EvidenceAdmissionProfile
from .planning import ExecutionRealm

WORKFLOW_SCHEMA_VERSION = "gate3-workflow.v2"
WORKFLOW_PROJECTION_POLICY_VERSION = "workflow-projection.g3.5.v1"
WORKFLOW_PROJECTION_POLICY_SHA256 = content_sha256(
    {
        "version": WORKFLOW_PROJECTION_POLICY_VERSION,
        "schema_version": WORKFLOW_SCHEMA_VERSION,
        "axes": (
            "execution",
            "obligation",
            "publication",
            "delivery",
        ),
        "facts": (
            "PlanAcceptedFact",
            "AuthoritySupersededFact",
            "TaskExecutionFact",
            "TaskExecutionBatchFact",
            "ObligationDispositionFact",
            "PublicationDispositionFact",
            "DeliveryDispositionFact",
            "WorkflowNoChangeFact",
        ),
        "rules": {
            "authority_supersession_clears_active_plan": True,
            "authority_supersession_preserves_history": True,
            "late_superseded_plan_facts_are_audit_only": True,
            "gate3_settled_publication_allowed": False,
            "gate3_delivery_allowed": False,
        },
    }
)


class ExecutionState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class ObligationState(StrEnum):
    OPEN = "open"
    SATISFIED = "satisfied"
    BOUNDARY = "boundary"
    BLOCKED = "blocked"
    SUPERSEDED = "superseded"


class PublicationState(StrEnum):
    NOT_READY = "not_ready"
    PROVISIONAL = "provisional"
    SETTLED = "settled"
    BLOCKED = "blocked"


class DeliveryState(StrEnum):
    NOT_DELIVERED = "not_delivered"
    DELIVERED = "delivered"
    SUPERSEDED = "superseded"


class WorkflowProjectionError(ValueError):
    """Base error for fail-closed Workflow reduction."""


class WorkflowCursorGap(WorkflowProjectionError):
    """A fact skipped one or more journal cursors."""


class WorkflowFactConflict(WorkflowProjectionError):
    """A cursor or system identity was reused with different content."""


class WorkflowTransitionRejected(WorkflowProjectionError):
    """A fact requested a forbidden or invalid business transition."""


@dataclass(frozen=True, slots=True)
class WorkflowTaskDefinition:
    task_id: str
    business_label: str

    def __post_init__(self) -> None:
        require_nonempty(self.task_id, "task_id")
        require_nonempty(self.business_label, "business_label")


@dataclass(frozen=True, slots=True)
class WorkflowObligationDefinition:
    obligation_id: str
    task_id: str
    business_label: str

    def __post_init__(self) -> None:
        require_nonempty(self.obligation_id, "obligation_id")
        require_nonempty(self.task_id, "task_id")
        require_nonempty(self.business_label, "business_label")


@dataclass(frozen=True, slots=True)
class PlanAcceptedFact:
    case_id: str
    cursor: int
    source_event_id: str
    source_event_sha256: str
    question_revision_id: str
    question_content_sha256: str
    frame_revision_id: str
    frame_content_sha256: str
    plan_revision_id: str
    prior_plan_revision_id: str | None
    plan_content_sha256: str
    plan_adoption_id: str
    plan_adoption_sha256: str
    tasks: tuple[WorkflowTaskDefinition, ...]
    obligations: tuple[WorkflowObligationDefinition, ...]

    def __post_init__(self) -> None:
        _validate_fact_header(self)
        for field_name in (
            "question_revision_id",
            "frame_revision_id",
            "plan_revision_id",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        for field_name in (
            "question_content_sha256",
            "frame_content_sha256",
            "plan_content_sha256",
            "plan_adoption_id",
            "plan_adoption_sha256",
        ):
            require_sha256(getattr(self, field_name), field_name)
        if self.prior_plan_revision_id is not None:
            require_nonempty(
                self.prior_plan_revision_id,
                "prior_plan_revision_id",
            )
        _require_tuple_type(self.tasks, WorkflowTaskDefinition, "tasks")
        _require_tuple_type(
            self.obligations,
            WorkflowObligationDefinition,
            "obligations",
        )
        if not self.tasks:
            raise ValueError("accepted plan requires at least one task")
        task_ids = tuple(item.task_id for item in self.tasks)
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("plan task IDs must be unique")
        obligation_ids = tuple(item.obligation_id for item in self.obligations)
        if len(obligation_ids) != len(set(obligation_ids)):
            raise ValueError("plan obligation IDs must be unique")
        unknown_task_ids = {item.task_id for item in self.obligations} - set(task_ids)
        if unknown_task_ids:
            raise ValueError("every obligation must bind a plan task")


@dataclass(frozen=True, slots=True)
class AuthoritySupersededFact:
    """Fence the active Plan immediately after a correction or authority change."""

    case_id: str
    cursor: int
    source_event_id: str
    source_event_sha256: str
    superseded_plan_revision_id: str
    superseded_plan_content_sha256: str
    superseded_plan_adoption_id: str
    superseded_plan_adoption_sha256: str
    superseding_authority_revision_id: str
    superseding_authority_content_sha256: str

    def __post_init__(self) -> None:
        _validate_fact_header(self)
        for field_name in (
            "superseded_plan_revision_id",
            "superseding_authority_revision_id",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        for field_name in (
            "superseded_plan_content_sha256",
            "superseded_plan_adoption_id",
            "superseded_plan_adoption_sha256",
            "superseding_authority_content_sha256",
        ):
            require_sha256(getattr(self, field_name), field_name)


@dataclass(frozen=True, slots=True)
class TaskExecutionFact:
    case_id: str
    cursor: int
    source_event_id: str
    source_event_sha256: str
    plan_revision_id: str
    task_id: str
    state: ExecutionState

    def __post_init__(self) -> None:
        _validate_fact_header(self)
        require_nonempty(self.plan_revision_id, "plan_revision_id")
        require_nonempty(self.task_id, "task_id")
        _require_enum(self.state, ExecutionState, "state")


@dataclass(frozen=True, slots=True)
class TaskExecutionUpdate:
    task_id: str
    state: ExecutionState

    def __post_init__(self) -> None:
        require_nonempty(self.task_id, "task_id")
        _require_enum(self.state, ExecutionState, "state")


@dataclass(frozen=True, slots=True)
class TaskExecutionBatchFact:
    """Apply the task states derived from one immutable schedule checkpoint."""

    case_id: str
    cursor: int
    source_event_id: str
    source_event_sha256: str
    plan_revision_id: str
    updates: tuple[TaskExecutionUpdate, ...]

    def __post_init__(self) -> None:
        _validate_fact_header(self)
        require_nonempty(self.plan_revision_id, "plan_revision_id")
        _require_tuple_type(
            self.updates,
            TaskExecutionUpdate,
            "updates",
        )
        if not self.updates:
            raise ValueError("task execution batch requires updates")
        task_ids = tuple(item.task_id for item in self.updates)
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("task execution batch task IDs must be unique")


@dataclass(frozen=True, slots=True)
class ObligationDispositionFact:
    case_id: str
    cursor: int
    source_event_id: str
    source_event_sha256: str
    plan_revision_id: str
    obligation_id: str
    state: ObligationState

    def __post_init__(self) -> None:
        _validate_fact_header(self)
        require_nonempty(self.plan_revision_id, "plan_revision_id")
        require_nonempty(self.obligation_id, "obligation_id")
        _require_enum(self.state, ObligationState, "state")


@dataclass(frozen=True, slots=True)
class PublicationDispositionFact:
    case_id: str
    cursor: int
    source_event_id: str
    source_event_sha256: str
    state: PublicationState
    answer_version_id: str | None

    def __post_init__(self) -> None:
        _validate_fact_header(self)
        _require_enum(self.state, PublicationState, "state")
        if self.answer_version_id is not None:
            require_nonempty(self.answer_version_id, "answer_version_id")
        if (
            self.state is PublicationState.PROVISIONAL
            and self.answer_version_id is None
        ):
            raise ValueError("provisional publication requires answer_version_id")
        if (
            self.state is not PublicationState.PROVISIONAL
            and self.answer_version_id is not None
        ):
            raise ValueError("only provisional publication may bind an answer version")


@dataclass(frozen=True, slots=True)
class DeliveryDispositionFact:
    case_id: str
    cursor: int
    source_event_id: str
    source_event_sha256: str
    state: DeliveryState

    def __post_init__(self) -> None:
        _validate_fact_header(self)
        _require_enum(self.state, DeliveryState, "state")


@dataclass(frozen=True, slots=True)
class WorkflowNoChangeFact:
    """Consume a journal cursor that has no customer Workflow effect."""

    case_id: str
    cursor: int
    source_event_id: str
    source_event_sha256: str

    def __post_init__(self) -> None:
        _validate_fact_header(self)


type WorkflowFact = (
    PlanAcceptedFact
    | AuthoritySupersededFact
    | TaskExecutionFact
    | TaskExecutionBatchFact
    | ObligationDispositionFact
    | PublicationDispositionFact
    | DeliveryDispositionFact
    | WorkflowNoChangeFact
)


@dataclass(frozen=True, slots=True)
class WorkflowTaskProjection:
    case_id: str
    plan_revision_id: str
    task_id: str
    business_label: str
    execution_state: ExecutionState

    def __post_init__(self) -> None:
        for field_name in (
            "case_id",
            "plan_revision_id",
            "task_id",
            "business_label",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        _require_enum(
            self.execution_state,
            ExecutionState,
            "execution_state",
        )


@dataclass(frozen=True, slots=True)
class WorkflowObligationProjection:
    case_id: str
    plan_revision_id: str
    obligation_id: str
    task_id: str
    business_label: str
    obligation_state: ObligationState

    def __post_init__(self) -> None:
        for field_name in (
            "case_id",
            "plan_revision_id",
            "obligation_id",
            "task_id",
            "business_label",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        _require_enum(
            self.obligation_state,
            ObligationState,
            "obligation_state",
        )


@dataclass(frozen=True, slots=True)
class WorkflowCaseProjection:
    case_id: str
    active_plan_revision_id: str | None
    publication_state: PublicationState
    accepted_answer_version_id: str | None
    delivery_state: DeliveryState

    def __post_init__(self) -> None:
        require_nonempty(self.case_id, "case_id")
        if self.active_plan_revision_id is not None:
            require_nonempty(
                self.active_plan_revision_id,
                "active_plan_revision_id",
            )
        _require_enum(
            self.publication_state,
            PublicationState,
            "publication_state",
        )
        _require_enum(
            self.delivery_state,
            DeliveryState,
            "delivery_state",
        )
        if self.publication_state is PublicationState.SETTLED:
            raise WorkflowTransitionRejected(
                "Gate 3 cannot project settled publication"
            )
        if self.delivery_state is DeliveryState.DELIVERED:
            raise WorkflowTransitionRejected("Gate 3 cannot project delivered cases")
        if self.accepted_answer_version_id is not None:
            require_nonempty(
                self.accepted_answer_version_id,
                "accepted_answer_version_id",
            )
        if (self.publication_state is PublicationState.PROVISIONAL) != (
            self.accepted_answer_version_id is not None
        ):
            raise ValueError("only provisional publication binds an answer version")


@dataclass(frozen=True, slots=True)
class WorkflowSnapshot:
    case: WorkflowCaseProjection
    tasks: tuple[WorkflowTaskProjection, ...]
    obligations: tuple[WorkflowObligationProjection, ...]
    applied_cursor: int
    realm: ExecutionRealm
    evidence_profile: EvidenceAdmissionProfile
    accepted_plan_revision_id: str | None
    accepted_question_revision_id: str | None
    accepted_question_content_sha256: str | None
    accepted_frame_revision_id: str | None
    accepted_frame_content_sha256: str | None
    accepted_plan_content_sha256: str | None
    accepted_plan_adoption_id: str | None
    accepted_plan_adoption_sha256: str | None
    schema_version: str = WORKFLOW_SCHEMA_VERSION
    projection_policy_version: str = WORKFLOW_PROJECTION_POLICY_VERSION
    projection_policy_sha256: str = WORKFLOW_PROJECTION_POLICY_SHA256

    def __post_init__(self) -> None:
        if not isinstance(self.case, WorkflowCaseProjection):
            raise TypeError("case must be WorkflowCaseProjection")
        if not isinstance(self.realm, ExecutionRealm):
            raise TypeError("realm must be ExecutionRealm")
        if not isinstance(self.evidence_profile, EvidenceAdmissionProfile):
            raise TypeError("evidence_profile must be EvidenceAdmissionProfile")
        _require_tuple_type(
            self.tasks,
            WorkflowTaskProjection,
            "tasks",
        )
        _require_tuple_type(
            self.obligations,
            WorkflowObligationProjection,
            "obligations",
        )
        if self.applied_cursor < 0:
            raise ValueError("applied_cursor must be non-negative")
        if self.schema_version != WORKFLOW_SCHEMA_VERSION:
            raise ValueError("workflow snapshot schema version is stale")
        if (
            self.projection_policy_version != WORKFLOW_PROJECTION_POLICY_VERSION
            or self.projection_policy_sha256 != WORKFLOW_PROJECTION_POLICY_SHA256
        ):
            raise ValueError("workflow projection policy is stale")
        accepted_plan_values = (
            self.accepted_question_revision_id,
            self.accepted_question_content_sha256,
            self.accepted_frame_revision_id,
            self.accepted_frame_content_sha256,
            self.accepted_plan_revision_id,
            self.accepted_plan_content_sha256,
            self.accepted_plan_adoption_id,
            self.accepted_plan_adoption_sha256,
        )
        if any(item is None for item in accepted_plan_values) != all(
            item is None for item in accepted_plan_values
        ):
            raise ValueError(
                "accepted Plan identity must be entirely present or absent"
            )
        if self.accepted_plan_revision_id is not None:
            for field_name in (
                "accepted_question_revision_id",
                "accepted_frame_revision_id",
                "accepted_plan_revision_id",
            ):
                require_nonempty(getattr(self, field_name), field_name)
            for field_name in (
                "accepted_question_content_sha256",
                "accepted_frame_content_sha256",
                "accepted_plan_content_sha256",
                "accepted_plan_adoption_id",
                "accepted_plan_adoption_sha256",
            ):
                require_sha256(getattr(self, field_name), field_name)
        if (
            self.case.active_plan_revision_id is not None
            and self.case.active_plan_revision_id != self.accepted_plan_revision_id
        ):
            raise ValueError("active Plan must equal the latest accepted Plan")
        task_keys = tuple((item.plan_revision_id, item.task_id) for item in self.tasks)
        obligation_keys = tuple(
            (item.plan_revision_id, item.obligation_id) for item in self.obligations
        )
        if task_keys != tuple(sorted(task_keys)):
            raise ValueError("workflow tasks must use canonical order")
        if obligation_keys != tuple(sorted(obligation_keys)):
            raise ValueError("workflow obligations must use canonical order")
        if len(task_keys) != len(set(task_keys)):
            raise ValueError("workflow task projection keys must be unique")
        if len(obligation_keys) != len(set(obligation_keys)):
            raise ValueError("workflow obligation projection keys must be unique")
        if any(item.case_id != self.case.case_id for item in self.tasks):
            raise ValueError("workflow task belongs to another case")
        if any(item.case_id != self.case.case_id for item in self.obligations):
            raise ValueError("workflow obligation belongs to another case")

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)

    @property
    def snapshot_id(self) -> str:
        return content_sha256(
            {
                "kind": "workflow-snapshot.v1",
                "case_id": self.case.case_id,
                "applied_cursor": self.applied_cursor,
                "content_sha256": self.content_sha256,
            }
        )


@dataclass(frozen=True, slots=True)
class WorkflowApplicationReceipt:
    receipt_id: str
    case_id: str
    cursor: int
    source_event_id: str
    source_event_sha256: str
    fact_id: str
    fact_sha256: str
    prior_receipt_id: str | None
    prior_snapshot_sha256: str
    resulting_snapshot_id: str
    resulting_snapshot_sha256: str

    def __post_init__(self) -> None:
        require_nonempty(self.case_id, "case_id")
        require_nonempty(self.source_event_id, "source_event_id")
        require_sha256(
            self.source_event_sha256,
            "source_event_sha256",
        )
        if self.cursor < 1:
            raise ValueError("receipt cursor must be positive")
        for field_name in (
            "receipt_id",
            "fact_id",
            "fact_sha256",
            "resulting_snapshot_id",
            "prior_snapshot_sha256",
            "resulting_snapshot_sha256",
        ):
            require_sha256(getattr(self, field_name), field_name)
        if self.prior_receipt_id is not None:
            require_sha256(self.prior_receipt_id, "prior_receipt_id")
        if (self.cursor == 1) != (self.prior_receipt_id is None):
            raise ValueError("only the first application receipt may omit its prior")
        if self.receipt_id != _receipt_id(
            case_id=self.case_id,
            cursor=self.cursor,
            source_event_id=self.source_event_id,
            source_event_sha256=self.source_event_sha256,
            fact_id=self.fact_id,
            fact_sha256=self.fact_sha256,
            prior_receipt_id=self.prior_receipt_id,
            prior_snapshot_sha256=self.prior_snapshot_sha256,
            resulting_snapshot_id=self.resulting_snapshot_id,
            resulting_snapshot_sha256=self.resulting_snapshot_sha256,
        ):
            raise ValueError("receipt_id is not canonically derived")


@dataclass(frozen=True, slots=True)
class WorkflowProjectionHead:
    """Value stored in the future mutable projection-head row."""

    case_id: str
    version: int
    last_applied_cursor: int
    snapshot_id: str
    snapshot_sha256: str
    last_receipt_id: str | None

    def __post_init__(self) -> None:
        require_nonempty(self.case_id, "case_id")
        if self.version < 0:
            raise ValueError("head version must be non-negative")
        if self.last_applied_cursor < 0:
            raise ValueError("head last_applied_cursor must be non-negative")
        if self.version != self.last_applied_cursor:
            raise ValueError("workflow head version must equal its applied cursor")
        require_sha256(self.snapshot_id, "snapshot_id")
        require_sha256(self.snapshot_sha256, "snapshot_sha256")
        if self.last_receipt_id is not None:
            require_sha256(self.last_receipt_id, "last_receipt_id")
        if (self.last_applied_cursor == 0) != (self.last_receipt_id is None):
            raise ValueError("only an initial head may omit last_receipt_id")


@dataclass(frozen=True, slots=True)
class WorkflowReadModel:
    head: WorkflowProjectionHead
    snapshot: WorkflowSnapshot
    application_receipts: tuple[WorkflowApplicationReceipt, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.head, WorkflowProjectionHead):
            raise TypeError("head must be WorkflowProjectionHead")
        if not isinstance(self.snapshot, WorkflowSnapshot):
            raise TypeError("snapshot must be WorkflowSnapshot")
        _require_tuple_type(
            self.application_receipts,
            WorkflowApplicationReceipt,
            "application_receipts",
        )
        if self.head.case_id != self.snapshot.case.case_id:
            raise ValueError("workflow head and snapshot case differ")
        if (
            self.head.last_applied_cursor != self.snapshot.applied_cursor
            or self.head.snapshot_id != self.snapshot.snapshot_id
            or self.head.snapshot_sha256 != self.snapshot.content_sha256
        ):
            raise ValueError("workflow head does not identify its snapshot")
        receipt_cursors = tuple(item.cursor for item in self.application_receipts)
        if receipt_cursors != tuple(range(1, self.head.last_applied_cursor + 1)):
            raise ValueError("workflow receipts must cover every applied cursor")
        if any(item.case_id != self.head.case_id for item in self.application_receipts):
            raise ValueError("workflow receipt belongs to another case")
        source_event_ids = tuple(
            item.source_event_id for item in self.application_receipts
        )
        if len(source_event_ids) != len(set(source_event_ids)):
            raise ValueError("workflow receipts cannot reuse a source event ID")
        source_event_sha256s = tuple(
            item.source_event_sha256 for item in self.application_receipts
        )
        if len(source_event_sha256s) != len(set(source_event_sha256s)):
            raise ValueError("workflow receipts cannot reuse a source event digest")
        for index, receipt in enumerate(self.application_receipts):
            prior_receipt = None if index == 0 else self.application_receipts[index - 1]
            expected_prior_id = (
                None if prior_receipt is None else prior_receipt.receipt_id
            )
            if receipt.prior_receipt_id != expected_prior_id:
                raise ValueError("workflow receipt does not extend the prior receipt")
            expected_prior_snapshot_sha256 = (
                _initial_snapshot(
                    self.head.case_id,
                    realm=self.snapshot.realm,
                    evidence_profile=self.snapshot.evidence_profile,
                ).content_sha256
                if prior_receipt is None
                else prior_receipt.resulting_snapshot_sha256
            )
            if receipt.prior_snapshot_sha256 != expected_prior_snapshot_sha256:
                raise ValueError("workflow receipt snapshot chain is discontinuous")
        if self.application_receipts:
            last = self.application_receipts[-1]
            if (
                self.head.last_receipt_id != last.receipt_id
                or self.head.snapshot_id != last.resulting_snapshot_id
                or self.head.snapshot_sha256 != last.resulting_snapshot_sha256
            ):
                raise ValueError("workflow head does not identify its last receipt")


def initial_workflow_read_model(
    case_id: str,
    *,
    realm: ExecutionRealm,
    evidence_profile: EvidenceAdmissionProfile,
) -> WorkflowReadModel:
    require_nonempty(case_id, "case_id")
    if not isinstance(realm, ExecutionRealm):
        raise TypeError("realm must be ExecutionRealm")
    if not isinstance(evidence_profile, EvidenceAdmissionProfile):
        raise TypeError("evidence_profile must be EvidenceAdmissionProfile")
    snapshot = _initial_snapshot(
        case_id,
        realm=realm,
        evidence_profile=evidence_profile,
    )
    return WorkflowReadModel(
        head=WorkflowProjectionHead(
            case_id=case_id,
            version=0,
            last_applied_cursor=0,
            snapshot_id=snapshot.snapshot_id,
            snapshot_sha256=snapshot.content_sha256,
            last_receipt_id=None,
        ),
        snapshot=snapshot,
        application_receipts=(),
    )


def apply_workflow_fact(
    model: WorkflowReadModel,
    fact: WorkflowFact,
) -> WorkflowReadModel:
    """Apply one typed fact with exact cursor and replay semantics."""

    if not isinstance(model, WorkflowReadModel):
        raise TypeError("model must be WorkflowReadModel")
    if not isinstance(
        fact,
        (
            PlanAcceptedFact,
            AuthoritySupersededFact,
            TaskExecutionFact,
            TaskExecutionBatchFact,
            ObligationDispositionFact,
            PublicationDispositionFact,
            DeliveryDispositionFact,
            WorkflowNoChangeFact,
        ),
    ):
        raise TypeError("fact must be a closed WorkflowFact variant")
    if fact.case_id != model.head.case_id:
        raise WorkflowFactConflict("workflow fact belongs to another case")

    fact_id = workflow_fact_id(fact)
    fact_hash = content_sha256(fact)
    if fact.cursor <= model.head.last_applied_cursor:
        prior = model.application_receipts[fact.cursor - 1]
        if prior.fact_id == fact_id and prior.fact_sha256 == fact_hash:
            return model
        raise WorkflowFactConflict("workflow cursor already has different fact content")
    if any(
        receipt.source_event_id == fact.source_event_id
        or receipt.source_event_sha256 == fact.source_event_sha256
        for receipt in model.application_receipts
    ):
        raise WorkflowFactConflict(
            "source event identity already has a Workflow application receipt"
        )
    expected_cursor = model.head.last_applied_cursor + 1
    if fact.cursor != expected_cursor:
        raise WorkflowCursorGap(f"workflow fact cursor must equal {expected_cursor}")

    reduced = _reduce_snapshot(model.snapshot, fact)
    snapshot = replace(reduced, applied_cursor=fact.cursor)
    receipt = _build_receipt(
        prior_snapshot=model.snapshot,
        prior_receipt_id=model.head.last_receipt_id,
        resulting_snapshot=snapshot,
        fact=fact,
    )
    head = WorkflowProjectionHead(
        case_id=model.head.case_id,
        version=model.head.version + 1,
        last_applied_cursor=fact.cursor,
        snapshot_id=snapshot.snapshot_id,
        snapshot_sha256=snapshot.content_sha256,
        last_receipt_id=receipt.receipt_id,
    )
    return WorkflowReadModel(
        head=head,
        snapshot=snapshot,
        application_receipts=model.application_receipts + (receipt,),
    )


def replay_workflow(
    case_id: str,
    facts: tuple[WorkflowFact, ...],
    *,
    realm: ExecutionRealm,
    evidence_profile: EvidenceAdmissionProfile,
) -> WorkflowReadModel:
    _require_tuple_type(
        facts,
        (
            PlanAcceptedFact,
            AuthoritySupersededFact,
            TaskExecutionFact,
            TaskExecutionBatchFact,
            ObligationDispositionFact,
            PublicationDispositionFact,
            DeliveryDispositionFact,
            WorkflowNoChangeFact,
        ),
        "facts",
    )
    model = initial_workflow_read_model(
        case_id,
        realm=realm,
        evidence_profile=evidence_profile,
    )
    for fact in facts:
        model = apply_workflow_fact(model, fact)
    return model


def workflow_fact_id(fact: WorkflowFact) -> str:
    return content_sha256(
        {
            "kind": "workflow-fact.v1",
            "fact_type": type(fact).__name__,
            "fact_sha256": content_sha256(fact),
        }
    )


def workflow_customer_projection(
    snapshot: WorkflowSnapshot,
) -> Mapping[str, FrozenJson]:
    """Return the fixed customer-safe projection.

    Journal IDs, hashes, provider details, prompts, verifier internals, SQL,
    retries, credentials, and arbitrary source payloads have no output fields.
    """

    value = freeze_json(
        {
            "case": {
                "case_id": snapshot.case.case_id,
                "active_plan_revision_id": (snapshot.case.active_plan_revision_id),
                "publication_state": (snapshot.case.publication_state.value),
                "delivery_state": snapshot.case.delivery_state.value,
            },
            "tasks": [
                {
                    "plan_revision_id": item.plan_revision_id,
                    "task_id": item.task_id,
                    "business_label": item.business_label,
                    "execution_state": item.execution_state.value,
                }
                for item in snapshot.tasks
            ],
            "obligations": [
                {
                    "plan_revision_id": item.plan_revision_id,
                    "obligation_id": item.obligation_id,
                    "task_id": item.task_id,
                    "business_label": item.business_label,
                    "obligation_state": item.obligation_state.value,
                }
                for item in snapshot.obligations
            ],
        }
    )
    if not isinstance(value, Mapping):
        raise TypeError("customer workflow projection must be an object")
    return value


def _reduce_snapshot(
    snapshot: WorkflowSnapshot,
    fact: WorkflowFact,
) -> WorkflowSnapshot:
    if isinstance(fact, PlanAcceptedFact):
        return _accept_plan(snapshot, fact)
    if isinstance(fact, AuthoritySupersededFact):
        return _supersede_authority(snapshot, fact)
    if isinstance(fact, TaskExecutionFact):
        return _change_task_execution(snapshot, fact)
    if isinstance(fact, TaskExecutionBatchFact):
        return _change_task_execution_batch(snapshot, fact)
    if isinstance(fact, ObligationDispositionFact):
        return _change_obligation(snapshot, fact)
    if isinstance(fact, PublicationDispositionFact):
        return _change_publication(snapshot, fact)
    if isinstance(fact, DeliveryDispositionFact):
        return _change_delivery(snapshot, fact)
    if isinstance(fact, WorkflowNoChangeFact):
        return snapshot
    raise TypeError("unhandled WorkflowFact variant")


def _accept_plan(
    snapshot: WorkflowSnapshot,
    fact: PlanAcceptedFact,
) -> WorkflowSnapshot:
    active_plan_id = snapshot.case.active_plan_revision_id
    same_measurement_authority = (
        fact.question_revision_id
        == snapshot.accepted_question_revision_id
        and fact.question_content_sha256
        == snapshot.accepted_question_content_sha256
        and fact.frame_revision_id == snapshot.accepted_frame_revision_id
        and fact.frame_content_sha256
        == snapshot.accepted_frame_content_sha256
    )
    expected_prior_plan_id = active_plan_id
    if active_plan_id is None and same_measurement_authority:
        expected_prior_plan_id = snapshot.accepted_plan_revision_id
    if fact.prior_plan_revision_id != expected_prior_plan_id:
        raise WorkflowTransitionRejected(
            "accepted plan does not extend its measurement authority"
        )
    if any(item.plan_revision_id == fact.plan_revision_id for item in snapshot.tasks):
        raise WorkflowFactConflict("plan revision already has workflow projections")

    superseded_tasks = tuple(
        replace(item, execution_state=ExecutionState.SUPERSEDED)
        if item.plan_revision_id == active_plan_id
        else item
        for item in snapshot.tasks
    )
    superseded_obligations = tuple(
        replace(item, obligation_state=ObligationState.SUPERSEDED)
        if item.plan_revision_id == active_plan_id
        else item
        for item in snapshot.obligations
    )
    new_tasks = tuple(
        WorkflowTaskProjection(
            case_id=fact.case_id,
            plan_revision_id=fact.plan_revision_id,
            task_id=item.task_id,
            business_label=item.business_label,
            execution_state=ExecutionState.PENDING,
        )
        for item in fact.tasks
    )
    new_obligations = tuple(
        WorkflowObligationProjection(
            case_id=fact.case_id,
            plan_revision_id=fact.plan_revision_id,
            obligation_id=item.obligation_id,
            task_id=item.task_id,
            business_label=item.business_label,
            obligation_state=ObligationState.OPEN,
        )
        for item in fact.obligations
    )
    return WorkflowSnapshot(
        case=WorkflowCaseProjection(
            case_id=fact.case_id,
            active_plan_revision_id=fact.plan_revision_id,
            publication_state=PublicationState.NOT_READY,
            accepted_answer_version_id=None,
            delivery_state=DeliveryState.NOT_DELIVERED,
        ),
        tasks=tuple(
            sorted(
                superseded_tasks + new_tasks,
                key=lambda item: (item.plan_revision_id, item.task_id),
            )
        ),
        obligations=tuple(
            sorted(
                superseded_obligations + new_obligations,
                key=lambda item: (
                    item.plan_revision_id,
                    item.obligation_id,
                ),
            )
        ),
        applied_cursor=snapshot.applied_cursor,
        realm=snapshot.realm,
        evidence_profile=snapshot.evidence_profile,
        accepted_question_revision_id=fact.question_revision_id,
        accepted_question_content_sha256=fact.question_content_sha256,
        accepted_frame_revision_id=fact.frame_revision_id,
        accepted_frame_content_sha256=fact.frame_content_sha256,
        accepted_plan_revision_id=fact.plan_revision_id,
        accepted_plan_content_sha256=fact.plan_content_sha256,
        accepted_plan_adoption_id=fact.plan_adoption_id,
        accepted_plan_adoption_sha256=fact.plan_adoption_sha256,
    )


def _supersede_authority(
    snapshot: WorkflowSnapshot,
    fact: AuthoritySupersededFact,
) -> WorkflowSnapshot:
    if snapshot.case.active_plan_revision_id is None:
        raise WorkflowTransitionRejected(
            "authority supersession requires an active plan"
        )
    if (
        fact.superseded_plan_revision_id != snapshot.case.active_plan_revision_id
        or fact.superseded_plan_revision_id != snapshot.accepted_plan_revision_id
        or fact.superseded_plan_content_sha256 != snapshot.accepted_plan_content_sha256
        or fact.superseded_plan_adoption_id != snapshot.accepted_plan_adoption_id
        or fact.superseded_plan_adoption_sha256
        != snapshot.accepted_plan_adoption_sha256
    ):
        raise WorkflowTransitionRejected(
            "authority supersession does not identify the active accepted plan"
        )
    superseded_tasks = tuple(
        replace(item, execution_state=ExecutionState.SUPERSEDED)
        if item.plan_revision_id == fact.superseded_plan_revision_id
        else item
        for item in snapshot.tasks
    )
    superseded_obligations = tuple(
        replace(item, obligation_state=ObligationState.SUPERSEDED)
        if item.plan_revision_id == fact.superseded_plan_revision_id
        else item
        for item in snapshot.obligations
    )
    return replace(
        snapshot,
        case=replace(
            snapshot.case,
            active_plan_revision_id=None,
            publication_state=PublicationState.NOT_READY,
            accepted_answer_version_id=None,
            delivery_state=DeliveryState.SUPERSEDED,
        ),
        tasks=superseded_tasks,
        obligations=superseded_obligations,
    )


def _change_task_execution(
    snapshot: WorkflowSnapshot,
    fact: TaskExecutionFact,
) -> WorkflowSnapshot:
    index = _find_task(snapshot, fact.plan_revision_id, fact.task_id)
    current = snapshot.tasks[index]
    if current.execution_state is ExecutionState.SUPERSEDED:
        return snapshot
    _require_active_plan(snapshot, fact.plan_revision_id)
    if not _task_transition_allowed(
        current.execution_state,
        fact.state,
    ):
        raise WorkflowTransitionRejected(
            "invalid execution transition {} -> {}".format(
                current.execution_state.value,
                fact.state.value,
            )
        )
    tasks = list(snapshot.tasks)
    tasks[index] = replace(current, execution_state=fact.state)
    return replace(snapshot, tasks=tuple(tasks))


def _change_task_execution_batch(
    snapshot: WorkflowSnapshot,
    fact: TaskExecutionBatchFact,
) -> WorkflowSnapshot:
    result = snapshot
    for update in fact.updates:
        result = _change_task_execution(
            result,
            TaskExecutionFact(
                case_id=fact.case_id,
                cursor=fact.cursor,
                source_event_id=fact.source_event_id,
                source_event_sha256=fact.source_event_sha256,
                plan_revision_id=fact.plan_revision_id,
                task_id=update.task_id,
                state=update.state,
            ),
        )
    return result


def _change_obligation(
    snapshot: WorkflowSnapshot,
    fact: ObligationDispositionFact,
) -> WorkflowSnapshot:
    index = _find_obligation(
        snapshot,
        fact.plan_revision_id,
        fact.obligation_id,
    )
    current = snapshot.obligations[index]
    if current.obligation_state is ObligationState.SUPERSEDED:
        return snapshot
    _require_active_plan(snapshot, fact.plan_revision_id)
    obligations = list(snapshot.obligations)
    obligations[index] = replace(current, obligation_state=fact.state)
    return replace(snapshot, obligations=tuple(obligations))


def _change_publication(
    snapshot: WorkflowSnapshot,
    fact: PublicationDispositionFact,
) -> WorkflowSnapshot:
    if fact.state is PublicationState.SETTLED:
        raise WorkflowTransitionRejected("Gate 3 cannot project settled publication")
    if snapshot.case.active_plan_revision_id is None:
        raise WorkflowTransitionRejected("publication requires an active plan")
    return replace(
        snapshot,
        case=replace(
            snapshot.case,
            publication_state=fact.state,
            accepted_answer_version_id=fact.answer_version_id,
        ),
    )


def _change_delivery(
    snapshot: WorkflowSnapshot,
    fact: DeliveryDispositionFact,
) -> WorkflowSnapshot:
    if fact.state is DeliveryState.DELIVERED:
        raise WorkflowTransitionRejected("Gate 3 cannot project delivered cases")
    return replace(
        snapshot,
        case=replace(snapshot.case, delivery_state=fact.state),
    )


def _task_transition_allowed(
    current: ExecutionState,
    proposed: ExecutionState,
) -> bool:
    if current is ExecutionState.SUPERSEDED:
        return proposed is ExecutionState.SUPERSEDED
    if proposed is ExecutionState.SUPERSEDED or proposed is current:
        return True
    allowed = {
        ExecutionState.PENDING: {
            ExecutionState.RUNNING,
            ExecutionState.SUCCEEDED,
            ExecutionState.FAILED,
        },
        ExecutionState.RUNNING: {
            ExecutionState.SUCCEEDED,
            ExecutionState.FAILED,
        },
        ExecutionState.FAILED: {ExecutionState.RUNNING},
        ExecutionState.SUCCEEDED: set(),
    }
    return proposed in allowed[current]


def _require_active_plan(
    snapshot: WorkflowSnapshot,
    plan_revision_id: str,
) -> None:
    if snapshot.case.active_plan_revision_id != plan_revision_id:
        raise WorkflowTransitionRejected(
            "workflow fact does not target the active plan"
        )


def _find_task(
    snapshot: WorkflowSnapshot,
    plan_revision_id: str,
    task_id: str,
) -> int:
    for index, item in enumerate(snapshot.tasks):
        if item.plan_revision_id == plan_revision_id and item.task_id == task_id:
            return index
    raise WorkflowTransitionRejected("workflow task does not exist")


def _find_obligation(
    snapshot: WorkflowSnapshot,
    plan_revision_id: str,
    obligation_id: str,
) -> int:
    for index, item in enumerate(snapshot.obligations):
        if (
            item.plan_revision_id == plan_revision_id
            and item.obligation_id == obligation_id
        ):
            return index
    raise WorkflowTransitionRejected("workflow obligation does not exist")


def _build_receipt(
    *,
    prior_snapshot: WorkflowSnapshot,
    prior_receipt_id: str | None,
    resulting_snapshot: WorkflowSnapshot,
    fact: WorkflowFact,
) -> WorkflowApplicationReceipt:
    fact_id = workflow_fact_id(fact)
    fact_hash = content_sha256(fact)
    values = {
        "case_id": fact.case_id,
        "cursor": fact.cursor,
        "source_event_id": fact.source_event_id,
        "source_event_sha256": fact.source_event_sha256,
        "fact_id": fact_id,
        "fact_sha256": fact_hash,
        "prior_receipt_id": prior_receipt_id,
        "prior_snapshot_sha256": prior_snapshot.content_sha256,
        "resulting_snapshot_id": resulting_snapshot.snapshot_id,
        "resulting_snapshot_sha256": (resulting_snapshot.content_sha256),
    }
    return WorkflowApplicationReceipt(
        receipt_id=_receipt_id(**values),
        **values,
    )


def _receipt_id(
    *,
    case_id: str,
    cursor: int,
    source_event_id: str,
    source_event_sha256: str,
    fact_id: str,
    fact_sha256: str,
    prior_receipt_id: str | None,
    prior_snapshot_sha256: str,
    resulting_snapshot_id: str,
    resulting_snapshot_sha256: str,
) -> str:
    return content_sha256(
        {
            "kind": "workflow-application-receipt.v1",
            "case_id": case_id,
            "cursor": cursor,
            "source_event_id": source_event_id,
            "source_event_sha256": source_event_sha256,
            "fact_id": fact_id,
            "fact_sha256": fact_sha256,
            "prior_receipt_id": prior_receipt_id,
            "prior_snapshot_sha256": prior_snapshot_sha256,
            "resulting_snapshot_id": resulting_snapshot_id,
            "resulting_snapshot_sha256": resulting_snapshot_sha256,
        }
    )


def _initial_snapshot(
    case_id: str,
    *,
    realm: ExecutionRealm,
    evidence_profile: EvidenceAdmissionProfile,
) -> WorkflowSnapshot:
    return WorkflowSnapshot(
        case=WorkflowCaseProjection(
            case_id=case_id,
            active_plan_revision_id=None,
            publication_state=PublicationState.NOT_READY,
            accepted_answer_version_id=None,
            delivery_state=DeliveryState.NOT_DELIVERED,
        ),
        tasks=(),
        obligations=(),
        applied_cursor=0,
        realm=realm,
        evidence_profile=evidence_profile,
        accepted_question_revision_id=None,
        accepted_question_content_sha256=None,
        accepted_frame_revision_id=None,
        accepted_frame_content_sha256=None,
        accepted_plan_revision_id=None,
        accepted_plan_content_sha256=None,
        accepted_plan_adoption_id=None,
        accepted_plan_adoption_sha256=None,
    )


def _validate_fact_header(fact: object) -> None:
    case_id = fact.case_id
    cursor = fact.cursor
    source_event_id = fact.source_event_id
    source_event_sha256 = fact.source_event_sha256
    require_nonempty(case_id, "case_id")
    require_nonempty(source_event_id, "source_event_id")
    require_sha256(source_event_sha256, "source_event_sha256")
    if cursor < 1:
        raise ValueError("workflow fact cursor must be positive")


def _require_enum(value: object, enum_type: type[StrEnum], name: str) -> None:
    if not isinstance(value, enum_type):
        raise TypeError(f"{name} must be {enum_type.__name__}")


def _require_tuple_type(
    values: object,
    item_type: type | tuple[type, ...],
    name: str,
) -> None:
    if not isinstance(values, tuple):
        raise TypeError(f"{name} must be a tuple")
    if not all(isinstance(item, item_type) for item in values):
        raise TypeError(f"{name} contains an invalid item")
