"""The five vNext authority object families and their subordinate records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Mapping

from .canonical import (
    FrozenJson,
    content_sha256,
    freeze_json,
    require_aware_datetime,
    require_nonempty,
    require_sha256,
)


class CaseLifecycle(StrEnum):
    OPEN = "open"
    WAITING_FOR_USER = "waiting_for_user"
    STOPPED = "stopped"
    CLOSED = "closed"


class EvidenceType(StrEnum):
    ACCOUNTING = "accounting"
    DESCRIPTIVE = "descriptive"
    ASSOCIATION = "association"
    CANDIDATE_MECHANISM = "candidate_mechanism"
    CAUSAL = "causal"
    DATA_QUALITY = "data_quality"
    BOUNDARY = "boundary"


class EvidenceStrength(StrEnum):
    NONE = "none"
    CONTEXTUAL = "contextual"
    DIRECTIONAL = "directional"
    QUANTIFIED = "quantified"
    CAUSAL = "causal"


class AnswerStatus(StrEnum):
    PROVISIONAL = "provisional"
    SETTLED = "settled"


class ClaimVerifierStatus(StrEnum):
    ACCEPTED = "accepted"
    BOUNDARY_ONLY = "boundary_only"
    REJECTED = "rejected"


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
    accepted_frame_revision_id: str | None
    accepted_plan_revision_id: str | None
    accepted_answer_version_id: str | None
    opened_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        require_nonempty(self.case_id, "case_id")
        require_nonempty(self.thread_id, "thread_id")
        _require_enum(self.lifecycle, CaseLifecycle, "lifecycle")
        if self.head_version < 0:
            raise ValueError("head_version must be non-negative")
        require_aware_datetime(self.opened_at, "opened_at")
        require_aware_datetime(self.updated_at, "updated_at")
        if self.updated_at < self.opened_at:
            raise ValueError("updated_at cannot precede opened_at")
        if self.accepted_plan_revision_id and not self.accepted_frame_revision_id:
            raise ValueError("accepted plan requires an accepted frame")
        if self.accepted_answer_version_id and not self.accepted_plan_revision_id:
            raise ValueError("accepted answer requires an accepted plan")


@dataclass(frozen=True, slots=True)
class AnalysisFrameRevision:
    frame_revision_id: str
    case_id: str
    revision_number: int
    prior_frame_revision_id: str | None
    created_by_action_id: str
    created_at: datetime
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
        for name in (
            "frame_revision_id",
            "case_id",
            "created_by_action_id",
            "revision_reason",
            "estimand",
            "observation_unit",
            "numerator",
            "denominator",
            "exposure",
            "comparison",
        ):
            require_nonempty(getattr(self, name), name)
        if self.revision_number < 1:
            raise ValueError("revision_number must be positive")
        if self.revision_number == 1 and self.prior_frame_revision_id is not None:
            raise ValueError("first frame revision cannot have a prior revision")
        if self.revision_number > 1 and not self.prior_frame_revision_id:
            raise ValueError("later frame revisions require prior_frame_revision_id")
        require_aware_datetime(self.created_at, "created_at")
        _require_nonempty_members(self.assumptions, "assumptions")
        _require_nonempty_members(self.alternatives, "alternatives")
        _require_nonempty_members(
            self.falsification_conditions, "falsification_conditions"
        )
        _require_nonempty_members(self.reversal_conditions, "reversal_conditions")
        _require_nonempty_members(self.success_conditions, "success_conditions")
        _require_nonempty_members(self.stop_conditions, "stop_conditions")
        _require_nonempty_members(self.decision_record_ids, "decision_record_ids")
        _require_nonempty_members(
            self.semantic_contract_refs, "semantic_contract_refs"
        )

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)


@dataclass(frozen=True, slots=True)
class WorkTask:
    task_id: str
    business_purpose: str
    capability_intent: str
    target_claim_ids: tuple[str, ...]
    depends_on_task_ids: tuple[str, ...]
    success_conditions: tuple[str, ...]
    stop_conditions: tuple[str, ...]

    def __post_init__(self) -> None:
        require_nonempty(self.task_id, "task_id")
        require_nonempty(self.business_purpose, "business_purpose")
        require_nonempty(self.capability_intent, "capability_intent")
        if self.task_id in self.depends_on_task_ids:
            raise ValueError("task cannot depend on itself")
        _require_nonempty_members(self.target_claim_ids, "target_claim_ids")
        _require_nonempty_members(self.depends_on_task_ids, "depends_on_task_ids")
        _require_nonempty_members(self.success_conditions, "success_conditions")
        _require_nonempty_members(self.stop_conditions, "stop_conditions")


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
        _require_tuple_of(self.tasks, WorkTask, "tasks")
        if not self.tasks:
            raise ValueError("work plan must contain at least one task")
        task_ids = tuple(task.task_id for task in self.tasks)
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("work plan task IDs must be unique")
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
class ResultHandle:
    handle_id: str
    content_sha256: str
    schema_ref: str
    row_count: int
    storage_ref: str

    def __post_init__(self) -> None:
        require_nonempty(self.handle_id, "handle_id")
        require_sha256(self.content_sha256, "content_sha256")
        require_nonempty(self.schema_ref, "schema_ref")
        require_nonempty(self.storage_ref, "storage_ref")
        if self.row_count < 0:
            raise ValueError("row_count must be non-negative")


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    evidence_record_id: str
    case_id: str
    frame_revision_id: str
    plan_revision_id: str
    task_id: str
    capability_name: str
    query_spec_ref: str | None
    semantic_contract_refs: tuple[str, ...]
    snapshot_release_ref: str
    grain: str
    evidence_type: EvidenceType
    strength: EvidenceStrength
    business_summary: str
    limitations: tuple[str, ...]
    provenance: Mapping[str, FrozenJson]
    payload_sha256: str
    inline_payload: Mapping[str, FrozenJson] | None
    result_handle: ResultHandle | None
    created_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "evidence_record_id",
            "case_id",
            "frame_revision_id",
            "plan_revision_id",
            "task_id",
            "capability_name",
            "snapshot_release_ref",
            "grain",
            "business_summary",
        ):
            require_nonempty(getattr(self, name), name)
        require_sha256(self.payload_sha256, "payload_sha256")
        require_aware_datetime(self.created_at, "created_at")
        _require_enum(self.evidence_type, EvidenceType, "evidence_type")
        _require_enum(self.strength, EvidenceStrength, "strength")
        if self.query_spec_ref is not None:
            require_nonempty(self.query_spec_ref, "query_spec_ref")
        _require_nonempty_members(self.semantic_contract_refs, "semantic_contract_refs")
        _require_nonempty_members(self.limitations, "limitations")
        frozen_provenance = freeze_json(self.provenance)
        if not isinstance(frozen_provenance, Mapping):
            raise TypeError("provenance must be a JSON object")
        object.__setattr__(self, "provenance", frozen_provenance)
        if (self.inline_payload is None) == (self.result_handle is None):
            raise ValueError(
                "evidence requires exactly one of inline_payload or result_handle"
            )
        if self.inline_payload is not None:
            frozen_payload = freeze_json(self.inline_payload)
            if not isinstance(frozen_payload, Mapping):
                raise TypeError("inline_payload must be a JSON object")
            if content_sha256(frozen_payload) != self.payload_sha256:
                raise ValueError("inline_payload does not match payload_sha256")
            object.__setattr__(self, "inline_payload", frozen_payload)
        if (
            self.result_handle is not None
            and self.result_handle.content_sha256 != self.payload_sha256
        ):
            raise ValueError("result_handle does not match payload_sha256")


@dataclass(frozen=True, slots=True)
class AnswerClaim:
    claim_id: str
    statement: str
    applicability: str
    evidence_record_ids: tuple[str, ...]
    boundary_ref: str | None
    limitations: tuple[str, ...]
    verifier_status: ClaimVerifierStatus
    reviewer_objection_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        require_nonempty(self.claim_id, "claim_id")
        require_nonempty(self.statement, "statement")
        require_nonempty(self.applicability, "applicability")
        _require_enum(
            self.verifier_status,
            ClaimVerifierStatus,
            "verifier_status",
        )
        if self.boundary_ref is not None:
            require_nonempty(self.boundary_ref, "boundary_ref")
        if not self.evidence_record_ids and not self.boundary_ref:
            raise ValueError("claim requires evidence or an explicit boundary")
        _require_nonempty_members(self.evidence_record_ids, "evidence_record_ids")
        _require_nonempty_members(self.limitations, "limitations")
        _require_nonempty_members(
            self.reviewer_objection_ids, "reviewer_objection_ids"
        )


@dataclass(frozen=True, slots=True)
class AnswerVersion:
    answer_version_id: str
    case_id: str
    frame_revision_id: str
    plan_revision_id: str
    version_number: int
    prior_answer_version_id: str | None
    status: AnswerStatus
    claims: tuple[AnswerClaim, ...]
    narrative_markdown: str
    verifier_policy_version: str
    unresolved_blocking_objection_ids: tuple[str, ...]
    settlement_fingerprint: str | None
    created_by_action_id: str
    created_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "answer_version_id",
            "case_id",
            "frame_revision_id",
            "plan_revision_id",
            "narrative_markdown",
            "verifier_policy_version",
            "created_by_action_id",
        ):
            require_nonempty(getattr(self, name), name)
        if self.version_number < 1:
            raise ValueError("version_number must be positive")
        if self.version_number == 1 and self.prior_answer_version_id is not None:
            raise ValueError("first answer version cannot have a prior version")
        if self.version_number > 1 and not self.prior_answer_version_id:
            raise ValueError("later answer versions require prior_answer_version_id")
        if not self.claims:
            raise ValueError("answer must contain at least one claim")
        _require_enum(self.status, AnswerStatus, "status")
        _require_tuple_of(self.claims, AnswerClaim, "claims")
        _require_nonempty_members(
            self.unresolved_blocking_objection_ids,
            "unresolved_blocking_objection_ids",
        )
        claim_ids = tuple(claim.claim_id for claim in self.claims)
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("answer claim IDs must be unique")
        require_aware_datetime(self.created_at, "created_at")
        if self.status is AnswerStatus.SETTLED:
            if self.unresolved_blocking_objection_ids:
                raise ValueError("settled answer cannot have blocking objections")
            if not self.settlement_fingerprint:
                raise ValueError("settled answer requires settlement_fingerprint")
            require_sha256(self.settlement_fingerprint, "settlement_fingerprint")
            rejected = tuple(
                claim.claim_id
                for claim in self.claims
                if claim.verifier_status is ClaimVerifierStatus.REJECTED
            )
            if rejected:
                raise ValueError(
                    "settled answer cannot contain rejected claims: {}".format(rejected)
                )
            expected_fingerprint = compute_answer_settlement_fingerprint(
                frame_revision_id=self.frame_revision_id,
                plan_revision_id=self.plan_revision_id,
                claims=self.claims,
                verifier_policy_version=self.verifier_policy_version,
            )
            if self.settlement_fingerprint != expected_fingerprint:
                raise ValueError(
                    "settlement_fingerprint does not match exact answer bindings"
                )
        elif self.settlement_fingerprint is not None:
            raise ValueError("provisional answer cannot carry settlement_fingerprint")

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)


@dataclass(frozen=True, slots=True)
class InterpretationRecord:
    interpretation_id: str
    case_id: str
    frame_revision_id: str
    evidence_record_ids: tuple[str, ...]
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
    selected_option_id: str
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
        if self.selected_option_id not in option_ids:
            raise ValueError("selected option must be present")
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


def compute_answer_settlement_fingerprint(
    *,
    frame_revision_id: str,
    plan_revision_id: str,
    claims: tuple[AnswerClaim, ...],
    verifier_policy_version: str,
) -> str:
    return content_sha256(
        {
            "frame_revision_id": frame_revision_id,
            "plan_revision_id": plan_revision_id,
            "claims": claims,
            "verifier_policy_version": verifier_policy_version,
        }
    )


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
