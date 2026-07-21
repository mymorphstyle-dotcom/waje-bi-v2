from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from bi_agent.runtime.capability_authority import (
    CapabilityAttempt,
    CapabilityOutcome,
    CapabilityOutcomeBundle,
    EvidenceLedgerEntry,
    ExecutionSnapshot,
    ExplorationStopRecord,
    FailureRecord,
)
from bi_agent.runtime.capability_scheduler import (
    capability_execution_transition_payloads,
)
from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value
from bi_agent.runtime.plan_authority import PlanRevision
from bi_agent.runtime.single_authority import DurableTransition


SCHEMA_VERSION = "single-authority-phase03.v1"
RESULT_STATUS = "evidence_ready"


class AuthoritativeExecutionResultContractError(ValueError):
    pass


_RESULT_FIELDS = frozenset(
    {
        "authoritative_execution_result_ref",
        "schema_version",
        "status",
        "run_attempt_id",
        "intent_revision_id",
        "authority_context_ref",
        "plan_revision_id",
        "execution_snapshot_ref",
        "stop_ref",
        "transition_id",
        "plan_revision",
        "execution_snapshot",
        "exploration_stop_record",
        "capability_outcome_bundles",
        "durable_transition",
        "bundle_set_digest",
        "content_digest",
    }
)
_BUNDLE_FIELDS = frozenset(
    {"attempt", "outcome", "evidence_entries", "failure_records"}
)


def _record_replay(record: Any, record_type: type) -> Any:
    if not isinstance(record, record_type):
        raise AuthoritativeExecutionResultContractError(
            "authoritative_execution_result_record_invalid"
        )
    try:
        rebuilt = record_type.from_dict(record.to_dict())
    except (AttributeError, TypeError, ValueError) as exc:
        raise AuthoritativeExecutionResultContractError(
            "authoritative_execution_result_record_invalid"
        ) from exc
    if rebuilt != record:
        raise AuthoritativeExecutionResultContractError(
            "authoritative_execution_result_record_invalid"
        )
    return rebuilt


def _record_from_payload(payload: Any, record_type: type) -> Any:
    if not isinstance(payload, Mapping):
        raise AuthoritativeExecutionResultContractError(
            "authoritative_execution_result_record_invalid"
        )
    try:
        return record_type.from_dict(payload)
    except (TypeError, ValueError) as exc:
        raise AuthoritativeExecutionResultContractError(
            "authoritative_execution_result_record_invalid"
        ) from exc


def _bundle_payload(bundle: CapabilityOutcomeBundle) -> dict[str, Any]:
    attempt, outcome, evidence_entries, failure_records = bundle
    return {
        "attempt": attempt.to_dict(),
        "outcome": outcome.to_dict(),
        "evidence_entries": tuple(item.to_dict() for item in evidence_entries),
        "failure_records": tuple(item.to_dict() for item in failure_records),
    }


def _bundle_from_payload(payload: Any) -> CapabilityOutcomeBundle:
    if not isinstance(payload, Mapping) or set(payload) != _BUNDLE_FIELDS:
        raise AuthoritativeExecutionResultContractError(
            "authoritative_execution_result_bundle_shape_invalid"
        )
    evidence_payloads = payload["evidence_entries"]
    failure_payloads = payload["failure_records"]
    if (
        isinstance(evidence_payloads, (str, bytes))
        or not isinstance(evidence_payloads, Sequence)
        or isinstance(failure_payloads, (str, bytes))
        or not isinstance(failure_payloads, Sequence)
    ):
        raise AuthoritativeExecutionResultContractError(
            "authoritative_execution_result_bundle_shape_invalid"
        )
    return (
        _record_from_payload(payload["attempt"], CapabilityAttempt),
        _record_from_payload(payload["outcome"], CapabilityOutcome),
        tuple(
            _record_from_payload(item, EvidenceLedgerEntry)
            for item in evidence_payloads
        ),
        tuple(_record_from_payload(item, FailureRecord) for item in failure_payloads),
    )


def _normalize_bundles(
    plan_revision: PlanRevision,
    execution_snapshot: ExecutionSnapshot,
    exploration_stop_record: ExplorationStopRecord,
    bundles: Sequence[CapabilityOutcomeBundle],
) -> tuple[CapabilityOutcomeBundle, ...]:
    if isinstance(bundles, (str, bytes)) or not isinstance(bundles, Sequence):
        raise AuthoritativeExecutionResultContractError(
            "authoritative_execution_result_bundles_invalid"
        )
    task_by_id = {task.task_id: task for task in plan_revision.capability_tasks}
    normalized: list[CapabilityOutcomeBundle] = []
    seen_attempt_ids: set[str] = set()
    seen_outcome_refs: set[str] = set()
    seen_task_ids: set[str] = set()
    evidence_entry_refs: set[str] = set()
    failure_refs: set[str] = set()
    for raw_bundle in bundles:
        if not isinstance(raw_bundle, tuple) or len(raw_bundle) != 4:
            raise AuthoritativeExecutionResultContractError(
                "authoritative_execution_result_bundle_shape_invalid"
            )
        attempt, outcome, raw_evidence, raw_failures = raw_bundle
        if not isinstance(raw_evidence, tuple) or not isinstance(raw_failures, tuple):
            raise AuthoritativeExecutionResultContractError(
                "authoritative_execution_result_bundle_shape_invalid"
            )
        attempt = _record_replay(attempt, CapabilityAttempt)
        outcome = _record_replay(outcome, CapabilityOutcome)
        evidence_entries = tuple(
            _record_replay(item, EvidenceLedgerEntry) for item in raw_evidence
        )
        failures = tuple(_record_replay(item, FailureRecord) for item in raw_failures)
        if (
            attempt.attempt_id in seen_attempt_ids
            or outcome.outcome_ref in seen_outcome_refs
            or outcome.task_id in seen_task_ids
        ):
            raise AuthoritativeExecutionResultContractError(
                "authoritative_execution_result_bundle_duplicate"
            )
        seen_attempt_ids.add(attempt.attempt_id)
        seen_outcome_refs.add(outcome.outcome_ref)
        seen_task_ids.add(outcome.task_id)
        task = task_by_id.get(outcome.task_id)
        if task is None:
            raise AuthoritativeExecutionResultContractError(
                "authoritative_execution_result_task_closure_invalid"
            )
        expected_attempt = CapabilityAttempt.create(
            plan_revision,
            task,
            execution_attempt=attempt.execution_attempt,
        )
        if (
            attempt != expected_attempt
            or outcome.run_attempt_id != plan_revision.run_attempt_id
            or outcome.plan_revision_id != plan_revision.plan_revision_id
            or outcome.task_id != attempt.task_id
            or outcome.attempt_id != attempt.attempt_id
            or outcome.input_digest != attempt.input_digest
            or set(outcome.affected_obligation_ids) - set(task.supports_obligation_ids)
        ):
            raise AuthoritativeExecutionResultContractError(
                "authoritative_execution_result_outcome_closure_invalid"
            )
        bundle_evidence_refs: set[str] = set()
        for entry in evidence_entries:
            if (
                entry.entry_ref in evidence_entry_refs
                or entry.run_attempt_id != plan_revision.run_attempt_id
                or entry.authority_context_ref != plan_revision.authority_context_ref
                or entry.plan_revision_id != plan_revision.plan_revision_id
                or entry.task_id != task.task_id
                or entry.outcome_ref != outcome.outcome_ref
            ):
                raise AuthoritativeExecutionResultContractError(
                    "authoritative_execution_result_evidence_closure_invalid"
                )
            evidence_entry_refs.add(entry.entry_ref)
            bundle_evidence_refs.add(entry.evidence_ref)
        if bundle_evidence_refs != set(outcome.evidence_refs):
            raise AuthoritativeExecutionResultContractError(
                "authoritative_execution_result_evidence_closure_invalid"
            )
        bundle_failure_refs: set[str] = set()
        for failure in failures:
            if (
                failure.failure_ref in failure_refs
                or failure.run_attempt_id != plan_revision.run_attempt_id
                or failure.plan_revision_id != plan_revision.plan_revision_id
                or failure.task_id != task.task_id
                or failure.attempt_id != attempt.attempt_id
            ):
                raise AuthoritativeExecutionResultContractError(
                    "authoritative_execution_result_failure_closure_invalid"
                )
            failure_refs.add(failure.failure_ref)
            bundle_failure_refs.add(failure.failure_ref)
        expected_failure_refs = (
            {outcome.failure_ref} if outcome.failure_ref is not None else set()
        )
        if bundle_failure_refs != expected_failure_refs:
            raise AuthoritativeExecutionResultContractError(
                "authoritative_execution_result_failure_closure_invalid"
            )
        normalized.append((attempt, outcome, evidence_entries, failures))

    normalized.sort(key=lambda item: item[1].outcome_ref)
    outcome_refs = tuple(item[1].outcome_ref for item in normalized)
    if outcome_refs != execution_snapshot.outcome_refs:
        raise AuthoritativeExecutionResultContractError(
            "authoritative_execution_result_outcome_closure_invalid"
        )
    if outcome_refs != exploration_stop_record.evaluated_outcome_refs:
        raise AuthoritativeExecutionResultContractError(
            "authoritative_execution_result_stop_closure_invalid"
        )
    if tuple(sorted(evidence_entry_refs)) != execution_snapshot.evidence_entry_refs:
        raise AuthoritativeExecutionResultContractError(
            "authoritative_execution_result_evidence_closure_invalid"
        )
    if tuple(sorted(failure_refs)) != execution_snapshot.failure_refs:
        raise AuthoritativeExecutionResultContractError(
            "authoritative_execution_result_failure_closure_invalid"
        )
    return tuple(normalized)


def _validate_record_closure(
    plan_revision: PlanRevision,
    execution_snapshot: ExecutionSnapshot,
    exploration_stop_record: ExplorationStopRecord,
    durable_transition: DurableTransition,
) -> None:
    if (
        execution_snapshot.run_attempt_id != plan_revision.run_attempt_id
        or execution_snapshot.authority_context_ref
        != plan_revision.authority_context_ref
        or execution_snapshot.plan_revision_id != plan_revision.plan_revision_id
        or exploration_stop_record.run_attempt_id != plan_revision.run_attempt_id
        or exploration_stop_record.plan_revision_id != plan_revision.plan_revision_id
        or exploration_stop_record.budget_policy_ref != plan_revision.budget_policy_ref
        or execution_snapshot.stop_ref != exploration_stop_record.stop_ref
        or durable_transition.run_attempt_id != plan_revision.run_attempt_id
        or durable_transition.intent_revision_id != plan_revision.intent_revision_id
    ):
        raise AuthoritativeExecutionResultContractError(
            "authoritative_execution_result_record_closure_invalid"
        )
    if (
        durable_transition.node_name != "execute_capability_dag"
        or durable_transition.status != "succeeded"
        or durable_transition.acceptance_state != "accepted"
        or durable_transition.next_transition != "phase03_evidence_bound"
    ):
        raise AuthoritativeExecutionResultContractError(
            "authoritative_execution_result_transition_invalid"
        )
    input_payload, output_payload = capability_execution_transition_payloads(
        plan_revision,
        execution_snapshot,
        exploration_stop_record,
    )
    if durable_transition.input_digest != canonical_digest(
        input_payload
    ) or durable_transition.output_digest != canonical_digest(output_payload):
        raise AuthoritativeExecutionResultContractError(
            "authoritative_execution_result_transition_digest_invalid"
        )


@dataclass(frozen=True)
class AuthoritativeExecutionResult:
    authoritative_execution_result_ref: str
    schema_version: str
    status: str
    run_attempt_id: str
    intent_revision_id: str
    authority_context_ref: str
    plan_revision_id: str
    execution_snapshot_ref: str
    stop_ref: str
    transition_id: str
    plan_revision: PlanRevision
    execution_snapshot: ExecutionSnapshot
    exploration_stop_record: ExplorationStopRecord
    capability_outcome_bundles: tuple[CapabilityOutcomeBundle, ...]
    durable_transition: DurableTransition
    bundle_set_digest: str
    content_digest: str

    @classmethod
    def from_records(
        cls,
        *,
        plan_revision: PlanRevision,
        execution_snapshot: ExecutionSnapshot,
        exploration_stop_record: ExplorationStopRecord,
        capability_outcome_bundles: Sequence[CapabilityOutcomeBundle],
        durable_transition: DurableTransition,
    ) -> "AuthoritativeExecutionResult":
        plan_revision = _record_replay(plan_revision, PlanRevision)
        execution_snapshot = _record_replay(execution_snapshot, ExecutionSnapshot)
        exploration_stop_record = _record_replay(
            exploration_stop_record, ExplorationStopRecord
        )
        durable_transition = _record_replay(durable_transition, DurableTransition)
        _validate_record_closure(
            plan_revision,
            execution_snapshot,
            exploration_stop_record,
            durable_transition,
        )
        bundles = _normalize_bundles(
            plan_revision,
            execution_snapshot,
            exploration_stop_record,
            capability_outcome_bundles,
        )
        bundle_payloads = tuple(_bundle_payload(bundle) for bundle in bundles)
        bundle_set_digest = canonical_digest(bundle_payloads)
        body = {
            "schema_version": SCHEMA_VERSION,
            "status": RESULT_STATUS,
            "run_attempt_id": plan_revision.run_attempt_id,
            "intent_revision_id": plan_revision.intent_revision_id,
            "authority_context_ref": plan_revision.authority_context_ref,
            "plan_revision_id": plan_revision.plan_revision_id,
            "execution_snapshot_ref": execution_snapshot.execution_snapshot_ref,
            "stop_ref": exploration_stop_record.stop_ref,
            "transition_id": durable_transition.transition_id,
            "plan_revision": plan_revision.to_dict(),
            "execution_snapshot": execution_snapshot.to_dict(),
            "exploration_stop_record": exploration_stop_record.to_dict(),
            "capability_outcome_bundles": bundle_payloads,
            "durable_transition": durable_transition.to_dict(),
            "bundle_set_digest": bundle_set_digest,
        }
        content_digest = canonical_digest(body)
        return cls(
            authoritative_execution_result_ref=(
                "authoritative-execution-result:sha256:" + content_digest
            ),
            content_digest=content_digest,
            schema_version=SCHEMA_VERSION,
            status=RESULT_STATUS,
            run_attempt_id=plan_revision.run_attempt_id,
            intent_revision_id=plan_revision.intent_revision_id,
            authority_context_ref=plan_revision.authority_context_ref,
            plan_revision_id=plan_revision.plan_revision_id,
            execution_snapshot_ref=execution_snapshot.execution_snapshot_ref,
            stop_ref=exploration_stop_record.stop_ref,
            transition_id=durable_transition.transition_id,
            plan_revision=plan_revision,
            execution_snapshot=execution_snapshot,
            exploration_stop_record=exploration_stop_record,
            capability_outcome_bundles=bundles,
            durable_transition=durable_transition,
            bundle_set_digest=bundle_set_digest,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AuthoritativeExecutionResult":
        if not isinstance(payload, Mapping) or set(payload) != _RESULT_FIELDS:
            raise AuthoritativeExecutionResultContractError(
                "authoritative_execution_result_shape_invalid"
            )
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise AuthoritativeExecutionResultContractError(
                "authoritative_execution_result_schema_invalid"
            )
        if payload.get("status") != RESULT_STATUS:
            raise AuthoritativeExecutionResultContractError(
                "authoritative_execution_result_status_invalid"
            )
        raw_bundles = payload["capability_outcome_bundles"]
        if isinstance(raw_bundles, (str, bytes)) or not isinstance(
            raw_bundles, Sequence
        ):
            raise AuthoritativeExecutionResultContractError(
                "authoritative_execution_result_bundles_invalid"
            )
        rebuilt = cls.from_records(
            plan_revision=_record_from_payload(payload["plan_revision"], PlanRevision),
            execution_snapshot=_record_from_payload(
                payload["execution_snapshot"], ExecutionSnapshot
            ),
            exploration_stop_record=_record_from_payload(
                payload["exploration_stop_record"], ExplorationStopRecord
            ),
            capability_outcome_bundles=tuple(
                _bundle_from_payload(item) for item in raw_bundles
            ),
            durable_transition=_record_from_payload(
                payload["durable_transition"], DurableTransition
            ),
        )
        for field in (
            "run_attempt_id",
            "intent_revision_id",
            "authority_context_ref",
            "plan_revision_id",
            "execution_snapshot_ref",
            "stop_ref",
            "transition_id",
        ):
            if getattr(rebuilt, field) != payload.get(field):
                raise AuthoritativeExecutionResultContractError(
                    "authoritative_execution_result_record_closure_invalid"
                )
        if rebuilt.bundle_set_digest != payload.get("bundle_set_digest"):
            raise AuthoritativeExecutionResultContractError(
                "authoritative_execution_result_bundle_digest_invalid"
            )
        if rebuilt.authoritative_execution_result_ref != payload.get(
            "authoritative_execution_result_ref"
        ):
            raise AuthoritativeExecutionResultContractError(
                "authoritative_execution_result_ref_invalid"
            )
        if rebuilt.content_digest != payload.get("content_digest"):
            raise AuthoritativeExecutionResultContractError(
                "authoritative_execution_result_digest_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(
            {
                "authoritative_execution_result_ref": (
                    self.authoritative_execution_result_ref
                ),
                "schema_version": self.schema_version,
                "status": self.status,
                "run_attempt_id": self.run_attempt_id,
                "intent_revision_id": self.intent_revision_id,
                "authority_context_ref": self.authority_context_ref,
                "plan_revision_id": self.plan_revision_id,
                "execution_snapshot_ref": self.execution_snapshot_ref,
                "stop_ref": self.stop_ref,
                "transition_id": self.transition_id,
                "plan_revision": self.plan_revision.to_dict(),
                "execution_snapshot": self.execution_snapshot.to_dict(),
                "exploration_stop_record": self.exploration_stop_record.to_dict(),
                "capability_outcome_bundles": tuple(
                    _bundle_payload(bundle)
                    for bundle in self.capability_outcome_bundles
                ),
                "durable_transition": self.durable_transition.to_dict(),
                "bundle_set_digest": self.bundle_set_digest,
                "content_digest": self.content_digest,
            }
        )

    def public_projection(self) -> dict[str, Any]:
        obligations = tuple(
            {
                "obligation_id": obligation.obligation_id,
                "claim_kind": obligation.claim_kind,
                "role": obligation.role,
                "evidence_requirement": obligation.evidence_requirement.to_dict(),
            }
            for obligation in sorted(
                self.plan_revision.claim_obligations,
                key=lambda item: item.obligation_id,
            )
        )
        tasks = tuple(
            {
                "task_id": task.task_id,
                "capability_id": task.capability_id,
                "obligations": tuple(
                    {
                        "obligation_id": str(edge["obligation_id"]),
                        "required": bool(edge["required"]),
                    }
                    for edge in task.obligation_edges
                ),
            }
            for task in sorted(
                self.plan_revision.capability_tasks,
                key=lambda item: item.task_id,
            )
        )
        outcomes = tuple(
            {
                "outcome_ref": outcome.outcome_ref,
                "task_id": outcome.task_id,
                "status": outcome.status,
                "evidence_refs": outcome.evidence_refs,
                "affected_obligation_ids": outcome.affected_obligation_ids,
                "limitation_refs": outcome.limitation_refs,
            }
            for _, outcome, _, _ in self.capability_outcome_bundles
        )
        evidence_entries = tuple(
            sorted(
                (
                    entry
                    for bundle in self.capability_outcome_bundles
                    for entry in bundle[2]
                ),
                key=lambda item: item.entry_ref,
            )
        )
        evidence = tuple(
            {
                "evidence_entry_ref": entry.entry_ref,
                "evidence_ref": entry.evidence_ref,
                "task_id": entry.task_id,
                "outcome_ref": entry.outcome_ref,
                "status": entry.execution_state,
                "evidence_kind": entry.evidence_kind,
                "data_contract_state": entry.data_contract_state,
                "supported_claim_kinds": entry.supported_claim_kinds,
                "evidence_strength": entry.evidence_strength,
                "maximum_claim_strength": entry.maximum_claim_strength,
                "scope": entry.scope,
                "window_refs": entry.window_refs,
                "result_refs": entry.result_refs,
                "completeness_report_refs": (entry.completeness_report_refs),
                "dimension_path": entry.dimension_path,
                "hierarchy_qualified": entry.hierarchy_qualified,
                "limitation_refs": entry.limitation_refs,
            }
            for entry in evidence_entries
        )
        failures = tuple(
            {
                "failure_ref": failure.failure_ref,
                "task_id": failure.task_id,
                "scope": failure.scope,
                "integrity_level": failure.integrity_level,
                "retryability": failure.retryability,
                "user_actionable": failure.user_actionable,
                "business_boundary": failure.business_boundary,
            }
            for failure in sorted(
                (
                    failure
                    for bundle in self.capability_outcome_bundles
                    for failure in bundle[3]
                ),
                key=lambda item: item.failure_ref,
            )
        )
        limitations = tuple(
            sorted(
                {
                    *(
                        limitation
                        for _, outcome, _, _ in self.capability_outcome_bundles
                        for limitation in outcome.limitation_refs
                    ),
                    *(
                        limitation
                        for entry in evidence_entries
                        for limitation in entry.limitation_refs
                    ),
                }
            )
        )
        return canonical_value(
            {
                "schema_version": self.schema_version,
                "status": self.status,
                "result_ref": self.authoritative_execution_result_ref,
                "plan_revision_id": self.plan_revision_id,
                "execution_snapshot_ref": self.execution_snapshot_ref,
                "tasks": tasks,
                "outcomes": outcomes,
                "obligations": obligations,
                "evidence": evidence,
                "failures": failures,
                "limitations": limitations,
                "stop": {
                    "stop_ref": self.exploration_stop_record.stop_ref,
                    "reason": self.exploration_stop_record.reason,
                    "evaluated_outcome_refs": (
                        self.exploration_stop_record.evaluated_outcome_refs
                    ),
                    "used_budget_units": (
                        self.exploration_stop_record.used_budget_units
                    ),
                    "hard_budget_limit": (
                        self.exploration_stop_record.hard_budget_limit
                    ),
                },
            }
        )


def _valid_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def validate_typed_authoritative_execution_result(
    value: AuthoritativeExecutionResult,
) -> AuthoritativeExecutionResult:
    """Validate an already-admitted typed result without reserializing evidence."""
    if type(value) is not AuthoritativeExecutionResult:
        raise AuthoritativeExecutionResultContractError(
            "authoritative_execution_result_invalid"
        )
    plan = value.plan_revision
    snapshot = value.execution_snapshot
    stop = value.exploration_stop_record
    transition = value.durable_transition
    if (
        type(plan) is not PlanRevision
        or type(snapshot) is not ExecutionSnapshot
        or type(stop) is not ExplorationStopRecord
        or type(transition) is not DurableTransition
    ):
        raise AuthoritativeExecutionResultContractError(
            "authoritative_execution_result_record_invalid"
        )
    _validate_record_closure(plan, snapshot, stop, transition)
    if (
        value.schema_version != SCHEMA_VERSION
        or value.status != RESULT_STATUS
        or value.run_attempt_id != plan.run_attempt_id
        or value.intent_revision_id != plan.intent_revision_id
        or value.authority_context_ref != plan.authority_context_ref
        or value.plan_revision_id != plan.plan_revision_id
        or value.execution_snapshot_ref != snapshot.execution_snapshot_ref
        or value.stop_ref != stop.stop_ref
        or value.transition_id != transition.transition_id
        or not _valid_digest(value.bundle_set_digest)
        or not _valid_digest(value.content_digest)
        or value.authoritative_execution_result_ref
        != "authoritative-execution-result:sha256:" + value.content_digest
        or type(value.capability_outcome_bundles) is not tuple
    ):
        raise AuthoritativeExecutionResultContractError(
            "authoritative_execution_result_record_closure_invalid"
        )

    outcome_refs: list[str] = []
    evidence_entry_refs: list[str] = []
    failure_refs: list[str] = []
    seen_attempt_ids: set[str] = set()
    seen_task_ids: set[str] = set()
    for bundle in value.capability_outcome_bundles:
        if type(bundle) is not tuple or len(bundle) != 4:
            raise AuthoritativeExecutionResultContractError(
                "authoritative_execution_result_bundle_shape_invalid"
            )
        attempt, outcome, evidence_entries, failures = bundle
        if (
            type(attempt) is not CapabilityAttempt
            or type(outcome) is not CapabilityOutcome
            or type(evidence_entries) is not tuple
            or type(failures) is not tuple
            or attempt.attempt_id in seen_attempt_ids
            or outcome.task_id in seen_task_ids
            or outcome.attempt_id != attempt.attempt_id
            or outcome.task_id != attempt.task_id
            or outcome.run_attempt_id != value.run_attempt_id
            or outcome.plan_revision_id != value.plan_revision_id
            or outcome.input_digest != attempt.input_digest
        ):
            raise AuthoritativeExecutionResultContractError(
                "authoritative_execution_result_outcome_closure_invalid"
            )
        seen_attempt_ids.add(attempt.attempt_id)
        seen_task_ids.add(outcome.task_id)
        outcome_refs.append(outcome.outcome_ref)
        bundle_evidence_refs: set[str] = set()
        for entry in evidence_entries:
            if (
                type(entry) is not EvidenceLedgerEntry
                or entry.run_attempt_id != value.run_attempt_id
                or entry.authority_context_ref != value.authority_context_ref
                or entry.plan_revision_id != value.plan_revision_id
                or entry.task_id != outcome.task_id
                or entry.outcome_ref != outcome.outcome_ref
            ):
                raise AuthoritativeExecutionResultContractError(
                    "authoritative_execution_result_evidence_closure_invalid"
                )
            evidence_entry_refs.append(entry.entry_ref)
            bundle_evidence_refs.add(entry.evidence_ref)
        if bundle_evidence_refs != set(outcome.evidence_refs):
            raise AuthoritativeExecutionResultContractError(
                "authoritative_execution_result_evidence_closure_invalid"
            )
        bundle_failure_refs: set[str] = set()
        for failure in failures:
            if (
                type(failure) is not FailureRecord
                or failure.run_attempt_id != value.run_attempt_id
                or failure.plan_revision_id != value.plan_revision_id
                or failure.task_id != outcome.task_id
                or failure.attempt_id != attempt.attempt_id
            ):
                raise AuthoritativeExecutionResultContractError(
                    "authoritative_execution_result_failure_closure_invalid"
                )
            failure_refs.append(failure.failure_ref)
            bundle_failure_refs.add(failure.failure_ref)
        if bundle_failure_refs != (
            {outcome.failure_ref} if outcome.failure_ref is not None else set()
        ):
            raise AuthoritativeExecutionResultContractError(
                "authoritative_execution_result_failure_closure_invalid"
            )
    if (
        tuple(sorted(outcome_refs)) != snapshot.outcome_refs
        or tuple(sorted(outcome_refs)) != stop.evaluated_outcome_refs
        or tuple(sorted(evidence_entry_refs)) != snapshot.evidence_entry_refs
        or tuple(sorted(failure_refs)) != snapshot.failure_refs
    ):
        raise AuthoritativeExecutionResultContractError(
            "authoritative_execution_result_bundle_closure_invalid"
        )
    return value


__all__ = (
    "AuthoritativeExecutionResult",
    "AuthoritativeExecutionResultContractError",
    "RESULT_STATUS",
    "SCHEMA_VERSION",
    "validate_typed_authoritative_execution_result",
)
