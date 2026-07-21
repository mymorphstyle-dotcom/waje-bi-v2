from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import Callable, Sequence

from bi_agent.runtime.capability_authority import (
    CapabilityAdapterOutput,
    CapabilityAttempt,
    CapabilityAuthorityContractError,
    CapabilityExecutionStore,
    CapabilityFailure,
    CapabilityOutcome,
    CapabilityOutcomeBundle,
    EvidenceLedgerEntry,
    ExecutionSnapshot,
    ExplorationStopRecord,
    FailureRecord,
)
from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value
from bi_agent.runtime.durable_call_journal import (
    DurableCallJournal,
    DurableCallSpec,
)
from bi_agent.runtime.exploration_budget_policy import (
    ExplorationBudgetPolicy,
    ExplorationBudgetPolicyError,
)
from bi_agent.runtime.plan_authority import CapabilityTask, PlanRevision
from bi_agent.runtime.runtime_persistence import CapabilitySettlementAuthority
from bi_agent.runtime.single_authority import DurableTransition


CapabilityAdapter = Callable[
    [CapabilityTask, CapabilityAttempt], CapabilityAdapterOutput
]


def topological_ready_waves(
    plan_revision: PlanRevision,
) -> tuple[tuple[CapabilityTask, ...], ...]:
    _validate_plan(plan_revision)
    remaining = {task.task_id: task for task in plan_revision.capability_tasks}
    completed: set[str] = set()
    waves: list[tuple[CapabilityTask, ...]] = []
    while remaining:
        ready = tuple(
            sorted(
                (
                    task
                    for task in remaining.values()
                    if set(task.dependency_task_ids) <= completed
                ),
                key=_task_execution_order,
            )
        )
        if not ready:
            raise CapabilityAuthorityContractError(
                "capability_scheduler_dependency_cycle"
            )
        waves.append(ready)
        for task in ready:
            completed.add(task.task_id)
            del remaining[task.task_id]
    return tuple(waves)


def execute_capability_plan(
    plan_revision: PlanRevision,
    *,
    adapter: CapabilityAdapter,
    store: CapabilityExecutionStore,
    settlement_authority: CapabilitySettlementAuthority,
    attempt_journal: DurableCallJournal,
    upstream_accepted_attempt_refs: Sequence[str],
    budget_policy: ExplorationBudgetPolicy,
    max_workers: int = 1,
    parent_transition_id: str,
    decision_ledger_position: int,
) -> ExecutionSnapshot:
    _validate_plan(plan_revision)
    if not callable(adapter):
        raise CapabilityAuthorityContractError("capability_scheduler_adapter_invalid")
    if not isinstance(store, CapabilityExecutionStore):
        raise CapabilityAuthorityContractError("capability_scheduler_store_invalid")
    if not isinstance(attempt_journal, DurableCallJournal):
        raise CapabilityAuthorityContractError(
            "capability_scheduler_attempt_journal_invalid"
        )
    if isinstance(upstream_accepted_attempt_refs, (str, bytes)):
        raise CapabilityAuthorityContractError(
            "capability_scheduler_upstream_attempt_refs_invalid"
        )
    upstream_attempt_refs = tuple(sorted(set(upstream_accepted_attempt_refs)))
    if len(upstream_attempt_refs) != len(tuple(upstream_accepted_attempt_refs)):
        raise CapabilityAuthorityContractError(
            "capability_scheduler_upstream_attempt_refs_invalid"
        )
    if type(settlement_authority) is not CapabilitySettlementAuthority:
        raise CapabilityAuthorityContractError(
            "capability_scheduler_settlement_authority_invalid"
        )
    settlement_authority = settlement_authority.revalidated()
    if settlement_authority.run_id != plan_revision.run_attempt_id:
        raise CapabilityAuthorityContractError(
            "capability_scheduler_settlement_run_mismatch"
        )
    if not isinstance(budget_policy, ExplorationBudgetPolicy):
        raise CapabilityAuthorityContractError(
            "capability_scheduler_budget_policy_invalid"
        )
    if plan_revision.budget_policy_ref != budget_policy.budget_policy_ref:
        raise CapabilityAuthorityContractError(
            "capability_scheduler_budget_policy_mismatch"
        )
    try:
        budget_policy = ExplorationBudgetPolicy.from_dict(budget_policy.to_dict())
        protected_task_ids = budget_policy.protected_task_ids(plan_revision)
        hard_budget_limit = budget_policy.effective_hard_budget_limit(plan_revision)
    except ExplorationBudgetPolicyError as exc:
        raise CapabilityAuthorityContractError(
            "capability_scheduler_budget_policy_invalid"
        ) from exc
    if type(max_workers) is not int or max_workers < 1:
        raise CapabilityAuthorityContractError(
            "capability_scheduler_max_workers_invalid"
        )
    if not isinstance(parent_transition_id, str) or not parent_transition_id:
        raise CapabilityAuthorityContractError(
            "capability_scheduler_parent_transition_invalid"
        )
    if type(decision_ledger_position) is not int or decision_ledger_position < 0:
        raise CapabilityAuthorityContractError(
            "capability_scheduler_ledger_position_invalid"
        )

    existing_snapshot = store.load_execution_snapshot(plan_revision.plan_revision_id)
    if existing_snapshot is not None:
        return _validate_snapshot(plan_revision, existing_snapshot)

    bundles: dict[str, CapabilityOutcomeBundle] = {}
    for task in sorted(plan_revision.capability_tasks, key=_task_execution_order):
        loaded = store.load_capability_outcome(
            plan_revision.plan_revision_id,
            task.task_id,
        )
        if loaded is not None:
            bundles[task.task_id] = _validate_bundle(
                plan_revision,
                task,
                loaded,
            )

    stop_reason: str
    while True:
        if _has_shared_authority_failure(bundles.values()):
            stop_reason = "shared_authority_failure"
            break

        unresolved = tuple(
            task
            for task in plan_revision.capability_tasks
            if task.task_id not in bundles
        )
        if not unresolved:
            stop_reason = "plan_exhausted"
            break

        dependency_skips = tuple(
            sorted(
                (
                    task
                    for task in unresolved
                    if task.dependency_task_ids
                    and all(
                        dependency_id in bundles
                        for dependency_id in task.dependency_task_ids
                    )
                    and any(
                        bundles[dependency_id][1].status != "succeeded"
                        for dependency_id in task.dependency_task_ids
                    )
                ),
                key=_task_execution_order,
            )
        )
        if dependency_skips:
            for task in dependency_skips:
                candidate = _dependency_skip_bundle(
                    plan_revision,
                    task,
                    tuple(bundles[item][1] for item in task.dependency_task_ids),
                    attempt_journal=attempt_journal,
                )
                authority = _outcome_settlement_authority(
                    task,
                    candidate,
                    settlement_authority,
                )
                accepted = store.accept_capability_outcome(
                    *candidate,
                    authority,
                )
                bundles[task.task_id] = _validate_bundle(
                    plan_revision,
                    task,
                    accepted,
                )
            continue

        ready = tuple(
            sorted(
                (
                    task
                    for task in unresolved
                    if all(
                        dependency_id in bundles
                        and bundles[dependency_id][1].status == "succeeded"
                        for dependency_id in task.dependency_task_ids
                    )
                ),
                key=_task_execution_order,
            )
        )
        if not ready:
            stop_reason = "no_ready_tasks"
            break

        if budget_policy.auxiliary_budget_limit is not None:
            used_auxiliary_budget = sum(
                bundle[1].budget_units
                for task_id, bundle in bundles.items()
                if task_id not in protected_task_ids
            )
            remaining_auxiliary_budget = (
                budget_policy.auxiliary_budget_limit - used_auxiliary_budget
            )
            protected_ready = tuple(
                task for task in ready if task.task_id in protected_task_ids
            )
            admitted_auxiliary: list[CapabilityTask] = []
            for task in ready:
                if task.task_id in protected_task_ids:
                    continue
                if task.declared_budget_units > remaining_auxiliary_budget:
                    break
                admitted_auxiliary.append(task)
                remaining_auxiliary_budget -= task.declared_budget_units
            ready = tuple(
                sorted(
                    (*protected_ready, *admitted_auxiliary),
                    key=_task_execution_order,
                )
            )
            if not ready:
                stop_reason = "hard_budget_reached"
                break

        accepted_wave = _execute_ready_wave(
            plan_revision,
            ready,
            adapter=adapter,
            store=store,
            settlement_authority=settlement_authority,
            attempt_journal=attempt_journal,
            max_workers=max_workers,
        )
        bundles.update(accepted_wave)

    outcomes = tuple(bundle[1] for bundle in bundles.values())
    evidence_entries = tuple(
        evidence for bundle in bundles.values() for evidence in bundle[2]
    )
    failures = tuple(failure for bundle in bundles.values() for failure in bundle[3])
    stop_record = ExplorationStopRecord.create(
        plan_revision,
        outcomes,
        reason=stop_reason,
        hard_budget_limit=hard_budget_limit,
    )
    snapshot = ExecutionSnapshot.create(
        plan_revision,
        stop_record,
        outcomes,
        evidence_entries,
        failures,
    )
    input_payload, output_payload = capability_execution_transition_payloads(
        plan_revision,
        snapshot,
        stop_record,
    )
    transition = DurableTransition.create(
        node_name="execute_capability_dag",
        parent_transition_id=parent_transition_id,
        run_attempt_id=plan_revision.run_attempt_id,
        intent_revision_id=plan_revision.intent_revision_id,
        decision_ledger_position=decision_ledger_position,
        input_digest=canonical_digest(input_payload),
        output_digest=canonical_digest(output_payload),
        execution_attempt=1,
        provider_ref="waje-capability-runtime",
        model_ref="deterministic-capability-dag.v1",
        status="succeeded",
        acceptance_state="accepted",
        next_transition="phase03_evidence_bound",
    )
    accepted_snapshot = store.accept_execution_settlement(
        snapshot,
        stop_record,
        transition,
        input_payload,
        output_payload,
        tuple(
            sorted(
                {
                    *upstream_attempt_refs,
                    *(bundle[0].attempt_id for bundle in bundles.values()),
                }
            )
        ),
    )
    accepted_snapshot = _validate_snapshot(plan_revision, accepted_snapshot)
    if accepted_snapshot != snapshot:
        raise CapabilityAuthorityContractError(
            "capability_scheduler_snapshot_replay_mismatch"
        )
    return accepted_snapshot


def capability_execution_transition_payloads(
    plan_revision: PlanRevision,
    snapshot: ExecutionSnapshot,
    stop_record: ExplorationStopRecord,
) -> tuple[dict[str, object], dict[str, object]]:
    _validate_plan(plan_revision)
    if (
        not isinstance(snapshot, ExecutionSnapshot)
        or not isinstance(stop_record, ExplorationStopRecord)
        or snapshot.run_attempt_id != plan_revision.run_attempt_id
        or snapshot.plan_revision_id != plan_revision.plan_revision_id
        or snapshot.authority_context_ref != plan_revision.authority_context_ref
        or stop_record.run_attempt_id != plan_revision.run_attempt_id
        or stop_record.plan_revision_id != plan_revision.plan_revision_id
        or snapshot.stop_ref != stop_record.stop_ref
        or snapshot.outcome_refs != stop_record.evaluated_outcome_refs
    ):
        raise CapabilityAuthorityContractError(
            "capability_scheduler_settlement_closure_invalid"
        )
    return (
        capability_execution_transition_input(
            plan_revision,
            hard_budget_limit=stop_record.hard_budget_limit,
        ),
        canonical_value(
            {
                "execution_snapshot": snapshot.to_dict(),
                "exploration_stop_record": stop_record.to_dict(),
            }
        ),
    )


def capability_execution_transition_input(
    plan_revision: PlanRevision,
    *,
    hard_budget_limit: int | None,
) -> dict[str, object]:
    _validate_plan(plan_revision)
    if hard_budget_limit is not None and (
        type(hard_budget_limit) is not int or hard_budget_limit < 0
    ):
        raise CapabilityAuthorityContractError(
            "capability_scheduler_budget_limit_invalid"
        )
    return canonical_value(
        {
            "plan_revision_id": plan_revision.plan_revision_id,
            "plan_digest": plan_revision.content_digest,
            "authority_context_ref": plan_revision.authority_context_ref,
            "budget_policy_ref": plan_revision.budget_policy_ref,
            "hard_budget_limit": hard_budget_limit,
            "capability_tasks": [
                {
                    "task_id": task.task_id,
                    "idempotency_key": task.idempotency_key,
                }
                for task in sorted(
                    plan_revision.capability_tasks,
                    key=lambda item: item.task_id,
                )
            ],
        }
    )


def _execute_ready_wave(
    plan_revision: PlanRevision,
    ready: Sequence[CapabilityTask],
    *,
    adapter: CapabilityAdapter,
    store: CapabilityExecutionStore,
    settlement_authority: CapabilitySettlementAuthority,
    attempt_journal: DurableCallJournal,
    max_workers: int,
) -> dict[str, CapabilityOutcomeBundle]:
    accepted: dict[str, CapabilityOutcomeBundle] = {}
    if max_workers == 1 or len(ready) == 1:
        for task in ready:
            candidate = _adapter_bundle(
                plan_revision,
                task,
                adapter,
                attempt_journal=attempt_journal,
            )
            authority = _outcome_settlement_authority(
                task,
                candidate,
                settlement_authority,
            )
            stored = store.accept_capability_outcome(*candidate, authority)
            accepted[task.task_id] = _validate_bundle(
                plan_revision,
                task,
                stored,
            )
        return accepted

    first_error: Exception | None = None
    with ThreadPoolExecutor(max_workers=min(max_workers, len(ready))) as executor:
        futures: dict[Future[CapabilityOutcomeBundle], CapabilityTask] = {
            executor.submit(
                _adapter_bundle,
                plan_revision,
                task,
                adapter,
                attempt_journal=attempt_journal,
            ): task
            for task in ready
        }
        for future in as_completed(futures):
            task = futures[future]
            try:
                candidate = future.result()
            except Exception as exc:
                if first_error is None:
                    first_error = exc
                continue
            authority = _outcome_settlement_authority(
                task,
                candidate,
                settlement_authority,
            )
            stored = store.accept_capability_outcome(*candidate, authority)
            accepted[task.task_id] = _validate_bundle(
                plan_revision,
                task,
                stored,
            )
    if first_error is not None:
        raise first_error
    return accepted


def _outcome_settlement_authority(
    task: CapabilityTask,
    bundle: CapabilityOutcomeBundle,
    settlement_authority: CapabilitySettlementAuthority,
) -> CapabilitySettlementAuthority:
    binding_refs = tuple(
        sorted(
            {
                entry.binding_record_ref
                for entry in bundle[2]
                if entry.binding_record_ref is not None
            }
        )
    )
    selected = settlement_authority.for_binding_refs(binding_refs)
    bindings_by_ref = {
        record.record_ref: record for record in selected.capability_binding_records
    }
    if set(bindings_by_ref) != set(binding_refs):
        raise CapabilityAuthorityContractError(
            "capability_settlement_binding_membership_mismatch"
        )
    for entry in bundle[2]:
        binding_ref = entry.binding_record_ref
        if binding_ref is None:
            continue
        binding = bindings_by_ref[binding_ref]
        expected_results = {
            *binding.result_refs,
            *binding.validation_result_refs,
        }
        expected_reports = {
            *binding.completeness_report_refs,
            *binding.validation_completeness_report_refs,
        }
        if binding.capability_id != task.capability_id:
            raise CapabilityAuthorityContractError(
                "capability_settlement_binding_capability_mismatch"
            )
        if set(entry.result_refs) != expected_results:
            raise CapabilityAuthorityContractError(
                "capability_settlement_result_membership_mismatch"
            )
        if set(entry.completeness_report_refs) != expected_reports:
            raise CapabilityAuthorityContractError(
                "capability_settlement_completeness_membership_mismatch"
            )
    return selected


def _adapter_bundle(
    plan_revision: PlanRevision,
    task: CapabilityTask,
    adapter: CapabilityAdapter,
    *,
    attempt_journal: DurableCallJournal,
) -> CapabilityOutcomeBundle:
    attempt, adapter_output = _journaled_adapter_output(
        plan_revision,
        task,
        attempt_journal=attempt_journal,
        output_factory=lambda current: adapter(task, current),
    )
    failure_records = (
        (FailureRecord.create(attempt, adapter_output.failure),)
        if adapter_output.failure is not None
        else ()
    )
    failure_ref = failure_records[0].failure_ref if failure_records else None
    outcome = CapabilityOutcome.create(
        attempt,
        task,
        adapter_output,
        failure_ref=failure_ref,
        budget_units=task.declared_budget_units,
    )
    evidence_entries = tuple(
        EvidenceLedgerEntry.create(plan_revision, task, outcome, evidence)
        for evidence in adapter_output.evidence
    )
    return (attempt, outcome, evidence_entries, failure_records)


def _dependency_skip_bundle(
    plan_revision: PlanRevision,
    task: CapabilityTask,
    dependency_outcomes: Sequence[CapabilityOutcome],
    *,
    attempt_journal: DurableCallJournal,
) -> CapabilityOutcomeBundle:
    ordered_dependencies = tuple(
        sorted(dependency_outcomes, key=lambda item: item.task_id)
    )
    failed_dependencies = tuple(
        item for item in ordered_dependencies if item.status != "succeeded"
    )
    if not failed_dependencies:
        raise CapabilityAuthorityContractError(
            "capability_scheduler_dependency_skip_without_failure"
        )
    integrity_level = (
        "task"
        if any(
            item.status in {"integrity_failed", "technical_failed"}
            for item in failed_dependencies
        )
        else "expected_boundary"
    )
    failure = CapabilityFailure.create(
        layer="capability",
        kind="dependency_not_succeeded",
        scope="task",
        affected_refs=(
            task.task_id,
            *task.supports_obligation_ids,
            *(item.outcome_ref for item in failed_dependencies),
        ),
        integrity_level=integrity_level,
        retryability="replan_required",
        user_actionable=False,
        business_boundary="dependent_capability_not_executed",
        technical_detail_ref="dependency-outcomes:"
        + ",".join(item.outcome_ref for item in failed_dependencies),
    )
    skip_output = CapabilityAdapterOutput.create(
        status="skipped",
        output_payload={
            "dependency_outcomes": tuple(
                {
                    "task_id": item.task_id,
                    "outcome_ref": item.outcome_ref,
                    "status": item.status,
                }
                for item in ordered_dependencies
            )
        },
        evidence=(),
        affected_obligation_ids=task.supports_obligation_ids,
        limitation_refs=tuple(
            "dependency-outcome:" + item.outcome_ref for item in failed_dependencies
        ),
        retryability="replan_required",
        failure=failure,
    )
    attempt, output = _journaled_adapter_output(
        plan_revision,
        task,
        attempt_journal=attempt_journal,
        output_factory=lambda _attempt: skip_output,
    )
    failure_record = FailureRecord.create(attempt, failure)
    outcome = CapabilityOutcome.create(
        attempt,
        task,
        output,
        failure_ref=failure_record.failure_ref,
        budget_units=0,
    )
    return (attempt, outcome, (), (failure_record,))


def _journaled_adapter_output(
    plan_revision: PlanRevision,
    task: CapabilityTask,
    *,
    attempt_journal: DurableCallJournal,
    output_factory: Callable[[CapabilityAttempt], CapabilityAdapterOutput],
) -> tuple[CapabilityAttempt, CapabilityAdapterOutput]:
    spec = _capability_call_spec(plan_revision, task)
    claim = attempt_journal.claim(spec)
    call_attempt = claim.attempt
    attempt = CapabilityAttempt.create(
        plan_revision,
        task,
        execution_attempt=call_attempt.attempt_number,
    )
    if call_attempt.attempt_ref != attempt.attempt_id or call_attempt.spec != spec:
        raise CapabilityAuthorityContractError(
            "capability_scheduler_attempt_journal_mismatch"
        )
    if claim.replayed:
        try:
            return attempt, CapabilityAdapterOutput.from_dict(
                claim.output_payload or {}
            )
        except (TypeError, ValueError) as exc:
            raise CapabilityAuthorityContractError(
                "capability_scheduler_journaled_output_invalid"
            ) from exc
    try:
        adapter_output = output_factory(attempt)
        if not isinstance(adapter_output, CapabilityAdapterOutput):
            raise CapabilityAuthorityContractError(
                "capability_scheduler_adapter_output_invalid"
            )
        adapter_output = CapabilityAdapterOutput.from_dict(adapter_output.to_dict())
    except Exception as exc:
        attempt_journal.fail(
            call_attempt,
            failure_code=type(exc).__name__,
        )
        raise
    completion = attempt_journal.succeed(
        call_attempt,
        adapter_output.to_dict(),
    )
    if (
        completion.disposition != "accepted"
        or completion.acceptance is None
        or completion.accepted_attempt is None
    ):
        raise CapabilityAuthorityContractError("capability_scheduler_call_orphaned")
    accepted_call_attempt = completion.accepted_attempt
    accepted_attempt = CapabilityAttempt.create(
        plan_revision,
        task,
        execution_attempt=accepted_call_attempt.attempt_number,
    )
    if (
        accepted_call_attempt.spec != spec
        or accepted_call_attempt.attempt_ref != accepted_attempt.attempt_id
    ):
        raise CapabilityAuthorityContractError(
            "capability_scheduler_attempt_journal_mismatch"
        )
    try:
        accepted_output = CapabilityAdapterOutput.from_dict(completion.output_payload)
    except (TypeError, ValueError) as exc:
        raise CapabilityAuthorityContractError(
            "capability_scheduler_journaled_output_invalid"
        ) from exc
    return accepted_attempt, accepted_output


def _capability_call_spec(
    plan_revision: PlanRevision,
    task: CapabilityTask,
) -> DurableCallSpec:
    prototype = CapabilityAttempt.create(plan_revision, task)
    input_payload = {
        "plan_revision_id": prototype.plan_revision_id,
        "task_id": prototype.task_id,
        "task_idempotency_key": prototype.task_idempotency_key,
        "normalized_input_digest": prototype.normalized_input_digest,
        "release_set_digest": prototype.release_set_digest,
        "contract_versions_digest": prototype.contract_versions_digest,
    }
    if canonical_digest(input_payload) != prototype.input_digest:
        raise CapabilityAuthorityContractError(
            "capability_scheduler_attempt_input_digest_invalid"
        )
    return DurableCallSpec.create(
        run_attempt_id=prototype.run_attempt_id,
        intent_revision_id=prototype.intent_revision_id,
        plan_revision_id=prototype.plan_revision_id,
        task_id=prototype.task_id,
        stage_name="execute_capability_dag",
        call_kind="capability",
        operation_name=task.capability_id,
        input_ref="capability-task-input:sha256:" + prototype.input_digest,
        input_payload=input_payload,
    )


def _validate_plan(plan_revision: PlanRevision) -> None:
    if not isinstance(plan_revision, PlanRevision) or not plan_revision.executable:
        raise CapabilityAuthorityContractError("capability_scheduler_plan_invalid")
    if not plan_revision.capability_tasks:
        raise CapabilityAuthorityContractError(
            "capability_scheduler_plan_tasks_missing"
        )
    task_ids = {task.task_id for task in plan_revision.capability_tasks}
    if any(
        task.plan_revision_id != plan_revision.plan_revision_id
        or set(task.dependency_task_ids) - task_ids
        for task in plan_revision.capability_tasks
    ):
        raise CapabilityAuthorityContractError(
            "capability_scheduler_plan_task_closure_invalid"
        )


def _task_execution_order(task: CapabilityTask) -> tuple[int, str]:
    return task.execution_rank, task.task_id


def _validate_bundle(
    plan_revision: PlanRevision,
    task: CapabilityTask,
    bundle: CapabilityOutcomeBundle,
) -> CapabilityOutcomeBundle:
    if not isinstance(bundle, tuple) or len(bundle) != 4:
        raise CapabilityAuthorityContractError(
            "capability_scheduler_outcome_bundle_invalid"
        )
    attempt, outcome, evidence_entries, failures = bundle
    if not isinstance(attempt, CapabilityAttempt) or not isinstance(
        outcome, CapabilityOutcome
    ):
        raise CapabilityAuthorityContractError(
            "capability_scheduler_outcome_bundle_invalid"
        )
    if isinstance(evidence_entries, (str, bytes)) or not isinstance(
        evidence_entries, Sequence
    ):
        raise CapabilityAuthorityContractError(
            "capability_scheduler_evidence_bundle_invalid"
        )
    if isinstance(failures, (str, bytes)) or not isinstance(failures, Sequence):
        raise CapabilityAuthorityContractError(
            "capability_scheduler_failure_bundle_invalid"
        )
    attempt = CapabilityAttempt.from_dict(attempt.to_dict())
    outcome = CapabilityOutcome.from_dict(outcome.to_dict())
    evidence = tuple(
        EvidenceLedgerEntry.from_dict(item.to_dict())
        if isinstance(item, EvidenceLedgerEntry)
        else _raise_bundle_type("capability_scheduler_evidence_bundle_invalid")
        for item in evidence_entries
    )
    failure_records = tuple(
        FailureRecord.from_dict(item.to_dict())
        if isinstance(item, FailureRecord)
        else _raise_bundle_type("capability_scheduler_failure_bundle_invalid")
        for item in failures
    )
    expected_attempt = CapabilityAttempt.create(
        plan_revision,
        task,
        execution_attempt=attempt.execution_attempt,
    )
    if attempt != expected_attempt:
        raise CapabilityAuthorityContractError(
            "capability_scheduler_attempt_replay_mismatch"
        )
    if (
        outcome.plan_revision_id != plan_revision.plan_revision_id
        or outcome.run_attempt_id != plan_revision.run_attempt_id
        or outcome.task_id != task.task_id
        or outcome.attempt_id != attempt.attempt_id
        or outcome.input_digest != attempt.input_digest
        or set(outcome.affected_obligation_ids) - set(task.supports_obligation_ids)
    ):
        raise CapabilityAuthorityContractError(
            "capability_scheduler_outcome_closure_invalid"
        )
    normalized_evidence = tuple(sorted(evidence, key=lambda item: item.entry_ref))
    if len({item.entry_ref for item in normalized_evidence}) != len(
        normalized_evidence
    ) or any(
        item.plan_revision_id != plan_revision.plan_revision_id
        or item.task_id != task.task_id
        or item.outcome_ref != outcome.outcome_ref
        for item in normalized_evidence
    ):
        raise CapabilityAuthorityContractError(
            "capability_scheduler_evidence_closure_invalid"
        )
    if {item.evidence_ref for item in normalized_evidence} != set(
        outcome.evidence_refs
    ):
        raise CapabilityAuthorityContractError(
            "capability_scheduler_evidence_membership_invalid"
        )
    normalized_failures = tuple(
        sorted(failure_records, key=lambda item: item.failure_ref)
    )
    if len({item.failure_ref for item in normalized_failures}) != len(
        normalized_failures
    ) or any(
        item.plan_revision_id != plan_revision.plan_revision_id
        or item.task_id != task.task_id
        or item.attempt_id != attempt.attempt_id
        for item in normalized_failures
    ):
        raise CapabilityAuthorityContractError(
            "capability_scheduler_failure_closure_invalid"
        )
    if outcome.failure_ref is None and normalized_failures:
        raise CapabilityAuthorityContractError(
            "capability_scheduler_unreferenced_failure_invalid"
        )
    if outcome.failure_ref is not None and outcome.failure_ref not in {
        item.failure_ref for item in normalized_failures
    }:
        raise CapabilityAuthorityContractError(
            "capability_scheduler_failure_membership_invalid"
        )
    return (attempt, outcome, normalized_evidence, normalized_failures)


def _validate_snapshot(
    plan_revision: PlanRevision,
    snapshot: ExecutionSnapshot,
) -> ExecutionSnapshot:
    if not isinstance(snapshot, ExecutionSnapshot):
        raise CapabilityAuthorityContractError(
            "capability_scheduler_snapshot_type_invalid"
        )
    snapshot = ExecutionSnapshot.from_dict(snapshot.to_dict())
    if (
        snapshot.plan_revision_id != plan_revision.plan_revision_id
        or snapshot.run_attempt_id != plan_revision.run_attempt_id
        or snapshot.authority_context_ref != plan_revision.authority_context_ref
    ):
        raise CapabilityAuthorityContractError(
            "capability_scheduler_snapshot_plan_mismatch"
        )
    return snapshot


def _has_shared_authority_failure(
    bundles: Sequence[CapabilityOutcomeBundle],
) -> bool:
    return any(
        failure.integrity_level == "shared_authority"
        for bundle in bundles
        for failure in bundle[3]
    )


def _raise_bundle_type(error: str):
    raise CapabilityAuthorityContractError(error)
