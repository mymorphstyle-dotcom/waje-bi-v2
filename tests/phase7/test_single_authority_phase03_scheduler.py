from __future__ import annotations

from dataclasses import FrozenInstanceError
from threading import Lock
import time
from typing import Sequence
from unittest.mock import patch

import pytest

from bi_agent.runtime.analysis_contracts import (
    AnalysisContract,
    analysis_contract_signature,
)
from bi_agent.runtime.capability_authority import (
    CapabilityAdapterOutput,
    CapabilityAttempt,
    CapabilityAuthorityContractError,
    CapabilityEvidence,
    CapabilityExecutionStore,
    CapabilityFailure,
    CapabilityOutcome,
    EvidenceLedgerEntry,
    ExecutionSnapshot,
    ExplorationStopRecord,
    FailureRecord,
)
from bi_agent.runtime.capability_scheduler import (
    execute_capability_plan,
    topological_ready_waves,
)
from bi_agent.runtime.durable_call_journal import InMemoryDurableCallJournal
from bi_agent.runtime.exploration_budget_policy import ExplorationBudgetPolicy
from bi_agent.runtime.plan_authority import (
    AnalysisAxis,
    ClaimObligation,
    EvidenceRequirement,
    PlanRevision,
)
from bi_agent.runtime.runtime_persistence import CapabilitySettlementAuthority
from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry
from tests.support.temporal_authority import resolved_test_temporal_authority


class _Store(CapabilityExecutionStore):
    def __init__(self) -> None:
        self.outcomes: dict[tuple[str, str], tuple] = {}
        self.settlement_authorities: dict[
            tuple[str, str], CapabilitySettlementAuthority
        ] = {}
        self.snapshots: dict[str, ExecutionSnapshot] = {}
        self.stop_records: dict[str, ExplorationStopRecord] = {}
        self.accepted_task_ids: list[str] = []
        self.attempt_journal = InMemoryDurableCallJournal()
        self._lock = Lock()

    def load_capability_outcome(self, plan_revision_id: str, task_id: str):
        with self._lock:
            return self.outcomes.get((plan_revision_id, task_id))

    def accept_capability_outcome(
        self,
        attempt: CapabilityAttempt,
        outcome: CapabilityOutcome,
        evidence_entries: Sequence[EvidenceLedgerEntry],
        failures: Sequence[FailureRecord],
        settlement_authority: CapabilitySettlementAuthority,
    ):
        settlement_authority = settlement_authority.revalidated()
        bundle = (attempt, outcome, tuple(evidence_entries), tuple(failures))
        key = (attempt.plan_revision_id, attempt.task_id)
        with self._lock:
            accepted_authority = self.settlement_authorities.setdefault(
                key,
                settlement_authority,
            )
            if accepted_authority != settlement_authority:
                raise CapabilityAuthorityContractError(
                    "capability_outcome_settlement_authority_conflict"
                )
            accepted = self.outcomes.setdefault(key, bundle)
            if accepted == bundle and attempt.task_id not in self.accepted_task_ids:
                self.accepted_task_ids.append(attempt.task_id)
            return accepted

    def load_execution_snapshot(
        self, plan_revision_id: str
    ) -> ExecutionSnapshot | None:
        with self._lock:
            return self.snapshots.get(plan_revision_id)

    def accept_execution_settlement(
        self,
        snapshot: ExecutionSnapshot,
        stop_record: ExplorationStopRecord,
        transition,
        input_payload,
        output_payload,
        accepted_attempt_refs,
    ) -> ExecutionSnapshot:
        with self._lock:
            self.stop_records.setdefault(stop_record.stop_ref, stop_record)
            self.transition = transition
            self.transition_input = input_payload
            self.transition_output = output_payload
            self.accepted_attempt_refs = tuple(accepted_attempt_refs)
            return self.snapshots.setdefault(snapshot.plan_revision_id, snapshot)


def _execute_plan(plan, *, adapter, store, **kwargs):
    settlement_authority = kwargs.pop(
        "settlement_authority",
        _settlement_authority(plan),
    )
    return execute_capability_plan(
        plan,
        adapter=adapter,
        store=store,
        attempt_journal=kwargs.pop("attempt_journal", store.attempt_journal),
        upstream_accepted_attempt_refs=kwargs.pop("upstream_accepted_attempt_refs", ()),
        settlement_authority=settlement_authority,
        budget_policy=kwargs.pop("budget_policy", _policy()),
        parent_transition_id="transition-phase02-plan-bound",
        decision_ledger_position=0,
        **kwargs,
    )


def _policy(auxiliary_budget_limit: int | None = None) -> ExplorationBudgetPolicy:
    return ExplorationBudgetPolicy.create(
        schema_version="exploration-budget-policy.v1",
        scope="run_attempt",
        protected_axis_roles=("required", "disclosure"),
        auxiliary_budget_limit=auxiliary_budget_limit,
        accounting_unit="declared_task_unit",
    )


def _settlement_authority(plan: PlanRevision) -> CapabilitySettlementAuthority:
    analysis = AnalysisContract(
        analysis_contract_id=f"analysis:{plan.plan_revision_id}:settlement",
        contract_version="test.v1",
        question_families=(),
        target_metric_refs=(),
        claim_intents=(),
        scope={"type": "test"},
        business_timezone="Asia/Shanghai",
        as_of="2026-07-18T00:00:00+08:00",
        resolved_windows=(),
        metric_bindings=(),
        dimension_bindings=(),
        dataset_requirements=(),
        capability_requirements=tuple(
            dict.fromkeys(task.capability_id for task in plan.capability_tasks)
        ),
        contract_gaps=(),
    )
    return CapabilitySettlementAuthority.create(
        run_id=plan.run_attempt_id,
        analysis_contract={
            **analysis.to_dict(),
            "contract_signature": analysis_contract_signature(analysis),
        },
        query_contracts=(),
        query_execution_records=(),
        rows_records=(),
        snapshot_records=(),
        completeness_records=(),
        capability_binding_records=(),
    )


def _plan(
    task_specs: Sequence[dict],
    *,
    user_obligation_keys: Sequence[str] = ("main",),
    axis_role: str = "required",
    budget_policy: ExplorationBudgetPolicy | None = None,
) -> PlanRevision:
    budget_policy = budget_policy or _policy()
    obligations = {
        key: ClaimObligation.create(
            claim_kind=f"{key}_claim",
            role="user_required"
            if key in user_obligation_keys
            else "analyst_auxiliary",
            subject=(
                {
                    "target_metric_ref": "metric:paid_amount",
                    "scope": {"scope_type": "full_sample", "filters": []},
                    "outcome_refs": (f"outcome:{key}",),
                    "goal_refs": ("explain_change",),
                }
                if key in user_obligation_keys
                else {
                    "planner_proposal_ref": "planner-proposal:phase03-scheduler",
                    "proposal_item_ref": f"proposal-item:{key}",
                    "target_metric_refs": ("metric:paid_amount",),
                    "scope": {"scope_type": "full_sample", "filters": []},
                    "goal_refs": ("explain_change",),
                }
            ),
            evidence_requirement=EvidenceRequirement.create(
                operator="any_of",
                evidence_kinds=("observed",),
            ),
            success_policy={
                "policy": "verified_or_explicit_boundary",
                "minimum_claim_strength": "directional",
            },
        )
        for key in {
            edge["obligation_key"] for spec in task_specs for edge in spec["edges"]
        }
    }
    capabilities = tuple(
        dict.fromkeys(str(spec["capability_id"]) for spec in task_specs)
    )
    axis = AnalysisAxis.create(
        axis_id="phase3_test_axis",
        role=axis_role,
        axis_kind="test",
        target_metric_refs=("metric:paid_amount",),
        metric_refs=("metric:paid_amount",),
        dimension_refs=("dimension:region", "dimension:device"),
        context_source_refs=(),
        capability_refs=capabilities,
        reconciliation_group="paid_amount",
        selection_policy="all_contract_backed_members",
        source_refs=("contract:test",),
        goal_refs=("explain_change",),
        supports_obligation_ids=tuple(
            item.obligation_id for item in obligations.values()
        ),
    )
    normalized_specs = []
    for execution_rank, spec in enumerate(task_specs, start=1):
        required = any(bool(edge["required"]) for edge in spec["edges"])
        has_edges = bool(spec["edges"])
        normalized_specs.append(
            {
                "task_key": spec["task_key"],
                "capability_id": spec["capability_id"],
                "normalized_input_refs": tuple(
                    spec.get(
                        "normalized_input_refs",
                        (
                            "authority:test",
                            axis.analysis_axis_ref,
                            f"input:{spec['task_key']}",
                        ),
                    )
                ),
                "dependency_task_keys": tuple(spec.get("dependencies", ())),
                "obligation_edges": tuple(
                    {
                        "obligation_id": obligations[
                            edge["obligation_key"]
                        ].obligation_id,
                        "required": bool(edge["required"]),
                    }
                    for edge in spec["edges"]
                ),
                "execution_rank": int(spec.get("execution_rank", execution_rank)),
                "declared_budget_units": int(spec.get("declared_budget_units", 1)),
                "governor_inputs": {
                    "expected_information_gain": (
                        "obligation_closing"
                        if required
                        else "hypothesis_testing"
                        if has_edges
                        else "context_enrichment"
                    ),
                    "materiality": (
                        "user_required"
                        if required
                        else "analyst_auxiliary"
                        if has_edges
                        else "contextual"
                    ),
                    "actionability": (
                        "decision_supporting"
                        if required
                        else "explanation_supporting"
                        if has_edges
                        else "diagnostic"
                    ),
                    "statistical_risk": str(
                        spec.get("statistical_risk", "contract_bounded")
                    ),
                },
                "execution_policy": {
                    "degradation_policy": {
                        "missing_required_input": "block_claim",
                        "missing_optional_input": "record_limitation",
                    },
                    "integrity_failure": "fail_closed",
                    "input_states": (),
                },
            }
        )
    temporal_authority = resolved_test_temporal_authority(
        time_spec={"kind": "date", "target": "2026-06-19"},
        comparison_spec={
            "kind": "fixed_window",
            "baseline_class": "prior_period",
            "baseline_start": "2026-06-18",
            "baseline_end": "2026-06-18",
            "aggregation": "sum_of_complete_days",
        },
        require_physical_baseline=True,
    )
    return PlanRevision.create(
        run_attempt_id="phase03-run",
        supersedes_plan_revision_id=None,
        intent_revision_id="intent-revision-phase03",
        decision_refs=("decision:baseline",),
        authority_context_ref="authority-context:phase03",
        planner_proposal_ref="planner-proposal-phase03",
        proposal_admission_ref="proposal-admission-phase03",
        temporal_authority=temporal_authority,
        resolved_window_refs=temporal_authority.resolved_window_refs,
        context_window_specs=(),
        claim_obligations=tuple(obligations.values()),
        analysis_axes=(axis,),
        capability_task_specs=tuple(normalized_specs),
        assumption_refs=(),
        budget_policy_ref=budget_policy.budget_policy_ref,
        contract_versions={"runtime": "phase03.v1", "factor": "factor.v1"},
    )


def _success(task, *, delay: float = 0.0, hierarchy: str | None = None):
    if delay:
        time.sleep(delay)
    dimension_path = (f"dimension:{hierarchy}",) if hierarchy else ()
    return CapabilityAdapterOutput.create(
        status="succeeded",
        output_payload={"task_id": task.task_id, "value": 1},
        evidence=(
            CapabilityEvidence.create(
                evidence_ref=f"evidence:{task.task_id}",
                binding_record_ref=None,
                execution_state="available",
                evidence_kind="observed",
                data_contract_state="complete",
                supported_claim_kinds=("comparative_change",),
                evidence_strength="high",
                maximum_claim_strength="descriptive",
                observation_facts=({"name": "value", "value": 1},),
                scope="all_players",
                window_refs=("window:target", "window:baseline"),
                dimension_path=dimension_path,
                limitation_refs=(),
                result_refs=(f"result:{task.task_id}",),
                completeness_report_refs=(f"completeness:{task.task_id}",),
                hierarchy_qualified=bool(hierarchy),
            ),
        ),
        affected_obligation_ids=task.supports_obligation_ids,
        limitation_refs=(),
        retryability="never",
    )


def _technical_failure(task):
    return CapabilityAdapterOutput.create(
        status="technical_failed",
        output_payload={"task_id": task.task_id, "stage": "query"},
        evidence=(),
        affected_obligation_ids=task.supports_obligation_ids,
        limitation_refs=(f"limitation:{task.task_id}",),
        retryability="same_input",
        failure=CapabilityFailure.create(
            layer="query",
            kind="query_transport_failed",
            scope="task",
            affected_refs=(task.task_id, *task.supports_obligation_ids),
            integrity_level="task",
            retryability="same_input",
            user_actionable=False,
            business_boundary="query_result_unavailable",
            technical_detail_ref=f"technical-detail:{task.task_id}",
        ),
    )


def _task_by_key(plan: PlanRevision, key: str):
    return next(task for task in plan.capability_tasks if task.task_key == key)


def test_settlement_revalidation_uses_sealed_contract_content_only() -> None:
    plan = _plan(
        (
            {
                "task_key": "content_addressed",
                "capability_id": "metric_timeseries",
                "edges": ({"obligation_key": "main", "required": True},),
            },
        )
    )
    authority = _settlement_authority(plan)

    with patch.object(
        RuntimeContractRegistry,
        "from_path",
        side_effect=AssertionError("current registry must not be consulted"),
    ):
        assert authority.revalidated() == authority


def test_ready_waves_follow_admitted_execution_rank_with_task_id_only_as_tiebreaker() -> (
    None
):
    plan = _plan(
        (
            {
                "task_key": "root_b",
                "capability_id": "root_b",
                "edges": ({"obligation_key": "main", "required": True},),
            },
            {
                "task_key": "child",
                "capability_id": "child",
                "dependencies": ("root_a", "root_b"),
                "edges": ({"obligation_key": "main", "required": True},),
            },
            {
                "task_key": "root_a",
                "capability_id": "root_a",
                "edges": ({"obligation_key": "main", "required": True},),
            },
            {
                "task_key": "grandchild",
                "capability_id": "grandchild",
                "dependencies": ("child",),
                "edges": ({"obligation_key": "main", "required": True},),
            },
        )
    )

    waves = topological_ready_waves(plan)

    assert "answer_verify" not in {task.capability_id for task in plan.capability_tasks}
    assert tuple(tuple(task.task_id for task in wave) for wave in waves) == (
        (
            _task_by_key(plan, "root_b").task_id,
            _task_by_key(plan, "root_a").task_id,
        ),
        (_task_by_key(plan, "child").task_id,),
        (_task_by_key(plan, "grandchild").task_id,),
    )


def test_failed_auxiliary_branch_skips_only_its_successors() -> None:
    plan = _plan(
        (
            {
                "task_key": "aux_root",
                "capability_id": "aux_root",
                "edges": ({"obligation_key": "aux", "required": False},),
            },
            {
                "task_key": "aux_child",
                "capability_id": "aux_child",
                "dependencies": ("aux_root",),
                "edges": ({"obligation_key": "aux", "required": False},),
            },
            {
                "task_key": "main_root",
                "capability_id": "main_root",
                "edges": ({"obligation_key": "main", "required": True},),
            },
            {
                "task_key": "main_child",
                "capability_id": "main_child",
                "dependencies": ("main_root",),
                "edges": ({"obligation_key": "main", "required": True},),
            },
        ),
        user_obligation_keys=("main",),
    )
    store = _Store()
    called: list[str] = []

    def adapter(task, _attempt):
        called.append(task.task_key)
        return (
            _technical_failure(task) if task.task_key == "aux_root" else _success(task)
        )

    snapshot = _execute_plan(plan, adapter=adapter, store=store)
    outcomes = {bundle[1].task_id: bundle[1] for bundle in store.outcomes.values()}

    assert set(called) == {"aux_root", "main_root", "main_child"}
    assert outcomes[_task_by_key(plan, "aux_root").task_id].status == "technical_failed"
    assert outcomes[_task_by_key(plan, "aux_child").task_id].status == "skipped"
    assert outcomes[_task_by_key(plan, "main_root").task_id].status == "succeeded"
    assert outcomes[_task_by_key(plan, "main_child").task_id].status == "succeeded"
    aux_ids = set(_task_by_key(plan, "aux_root").supports_obligation_ids)
    main_ids = set(_task_by_key(plan, "main_root").supports_obligation_ids)
    assert (
        set(outcomes[_task_by_key(plan, "aux_root").task_id].affected_obligation_ids)
        == aux_ids
    )
    assert not (
        set(outcomes[_task_by_key(plan, "aux_root").task_id].affected_obligation_ids)
        & main_ids
    )
    assert snapshot.stop_ref in store.stop_records
    assert store.stop_records[snapshot.stop_ref].reason == "plan_exhausted"


def test_exact_replay_and_resume_do_not_call_the_adapter_twice() -> None:
    plan = _plan(
        (
            {
                "task_key": "first",
                "capability_id": "first",
                "edges": ({"obligation_key": "main", "required": True},),
            },
            {
                "task_key": "second",
                "capability_id": "second",
                "dependencies": ("first",),
                "edges": ({"obligation_key": "main", "required": True},),
            },
        )
    )
    store = _Store()
    first_attempt_calls: list[str] = []

    def crashing_adapter(task, _attempt):
        first_attempt_calls.append(task.task_key)
        if task.task_key == "second":
            raise RuntimeError("worker_died_after_first_task")
        return _success(task)

    with pytest.raises(RuntimeError, match="worker_died_after_first_task"):
        _execute_plan(plan, adapter=crashing_adapter, store=store)

    assert first_attempt_calls == ["first", "second"]
    resumed_calls: list[str] = []
    snapshot = _execute_plan(
        plan,
        adapter=lambda task, attempt: (
            resumed_calls.append(task.task_key) or _success(task)
        ),
        store=store,
    )
    replayed = _execute_plan(
        plan,
        adapter=lambda *_: pytest.fail("settled plan called adapter"),
        store=store,
    )

    assert resumed_calls == ["second"]
    assert replayed == snapshot
    assert len(store.outcomes) == 2
    second_attempt = store.outcomes[
        (plan.plan_revision_id, _task_by_key(plan, "second").task_id)
    ][0]
    assert second_attempt.execution_attempt == 2
    assert set(store.accepted_attempt_refs) == {
        bundle[0].attempt_id for bundle in store.outcomes.values()
    }


def test_completion_order_cannot_change_snapshot_or_ledger_digest() -> None:
    plan = _plan(
        (
            {
                "task_key": "region",
                "capability_id": "dimension_screen",
                "edges": ({"obligation_key": "main", "required": True},),
            },
            {
                "task_key": "device",
                "capability_id": "dimension_screen",
                "edges": ({"obligation_key": "main", "required": True},),
            },
            {
                "task_key": "channel",
                "capability_id": "dimension_screen",
                "edges": ({"obligation_key": "main", "required": True},),
            },
        )
    )

    def run(delays):
        store = _Store()
        completion_order: list[str] = []
        order_lock = Lock()

        def adapter(task, _attempt):
            output = _success(
                task, delay=delays[task.task_key], hierarchy=task.task_key
            )
            with order_lock:
                completion_order.append(task.task_key)
            return output

        snapshot = _execute_plan(
            plan,
            adapter=adapter,
            store=store,
            max_workers=3,
        )
        ledger = tuple(
            entry for bundle in store.outcomes.values() for entry in bundle[2]
        )
        return snapshot, completion_order, ledger

    first, first_order, first_ledger = run(
        {"region": 0.03, "device": 0.02, "channel": 0.01}
    )
    second, second_order, second_ledger = run(
        {"region": 0.01, "device": 0.02, "channel": 0.03}
    )

    assert first_order != second_order
    assert first.content_digest == second.content_digest
    assert first.outcome_set_digest == second.outcome_set_digest
    assert first.evidence_ledger_digest == second.evidence_ledger_digest
    assert {
        entry.dimension_path for entry in first_ledger if entry.hierarchy_qualified
    } == {
        ("dimension:region",),
        ("dimension:device",),
        ("dimension:channel",),
    }
    assert {entry.entry_ref for entry in first_ledger} == set(first.evidence_entry_refs)
    assert {entry.entry_ref for entry in second_ledger} == set(
        second.evidence_entry_refs
    )


@pytest.mark.parametrize("axis_role", ("required", "disclosure"))
def test_budget_never_truncates_protected_axis_tasks(axis_role: str) -> None:
    policy = _policy(auxiliary_budget_limit=0)
    plan = _plan(
        tuple(
            {
                "task_key": key,
                "capability_id": key,
                "edges": ({"obligation_key": "axis", "required": False},),
            }
            for key in ("one", "two", "three")
        ),
        user_obligation_keys=(),
        axis_role=axis_role,
        budget_policy=policy,
    )
    store = _Store()
    called: list[str] = []

    snapshot = _execute_plan(
        plan,
        adapter=lambda task, attempt: called.append(task.task_id) or _success(task),
        store=store,
        budget_policy=policy,
        max_workers=3,
    )
    stop = store.stop_records[snapshot.stop_ref]

    assert set(called) == {task.task_id for task in plan.capability_tasks}
    assert stop.reason == "plan_exhausted"
    assert stop.used_budget_units == 3
    assert stop.hard_budget_limit == 3
    assert len(snapshot.outcome_refs) == 3
    assert len(store.outcomes) == 3
    assert stop.policy_decision["budget"] == "exhausted"


def test_auxiliary_budget_stops_only_auxiliary_tasks() -> None:
    policy = _policy(auxiliary_budget_limit=1)
    plan = _plan(
        tuple(
            {
                "task_key": key,
                "capability_id": key,
                "edges": ({"obligation_key": "aux", "required": False},),
            }
            for key in ("one", "two", "three")
        ),
        user_obligation_keys=(),
        axis_role="auxiliary",
        budget_policy=policy,
    )
    store = _Store()
    called: list[str] = []

    snapshot = _execute_plan(
        plan,
        adapter=lambda task, attempt: called.append(task.task_id) or _success(task),
        store=store,
        budget_policy=policy,
        max_workers=3,
    )
    stop = store.stop_records[snapshot.stop_ref]

    assert called == [_task_by_key(plan, "one").task_id]
    assert stop.reason == "hard_budget_reached"
    assert stop.used_budget_units == 1
    assert stop.hard_budget_limit == 1
    assert stop.policy_decision["next_information_gain"] == (
        "eligible_but_budget_blocked"
    )


def test_required_obligation_runs_when_auxiliary_budget_is_zero() -> None:
    policy = _policy(auxiliary_budget_limit=0)
    plan = _plan(
        (
            {
                "task_key": "auxiliary_first",
                "capability_id": "auxiliary_first",
                "edges": ({"obligation_key": "aux", "required": False},),
            },
            {
                "task_key": "required_second",
                "capability_id": "required_second",
                "edges": ({"obligation_key": "main", "required": True},),
            },
        ),
        user_obligation_keys=("main",),
        axis_role="auxiliary",
        budget_policy=policy,
    )
    store = _Store()
    called: list[str] = []

    snapshot = _execute_plan(
        plan,
        adapter=lambda task, attempt: called.append(task.task_key) or _success(task),
        store=store,
        budget_policy=policy,
    )
    stop = store.stop_records[snapshot.stop_ref]

    assert called == ["required_second"]
    assert stop.reason == "hard_budget_reached"
    assert stop.used_budget_units == 1
    assert stop.hard_budget_limit == 1


def test_required_task_dependency_closure_is_budget_protected() -> None:
    policy = _policy(auxiliary_budget_limit=0)
    plan = _plan(
        (
            {
                "task_key": "foundation",
                "capability_id": "foundation",
                "edges": (),
            },
            {
                "task_key": "required",
                "capability_id": "required",
                "dependencies": ("foundation",),
                "edges": ({"obligation_key": "main", "required": True},),
            },
        ),
        user_obligation_keys=("main",),
        axis_role="auxiliary",
        budget_policy=policy,
    )
    store = _Store()
    called: list[str] = []

    snapshot = _execute_plan(
        plan,
        adapter=lambda task, attempt: called.append(task.task_key) or _success(task),
        store=store,
        budget_policy=policy,
    )
    stop = store.stop_records[snapshot.stop_ref]

    assert called == ["foundation", "required"]
    assert stop.reason == "plan_exhausted"
    assert stop.used_budget_units == 2
    assert stop.hard_budget_limit == 2


def test_declared_task_cost_controls_budget_and_outcome_accounting() -> None:
    policy = _policy(auxiliary_budget_limit=2)
    plan = _plan(
        (
            {
                "task_key": "expensive_first",
                "capability_id": "expensive_first",
                "declared_budget_units": 2,
                "edges": ({"obligation_key": "aux", "required": False},),
            },
            {
                "task_key": "cheap_second",
                "capability_id": "cheap_second",
                "declared_budget_units": 1,
                "edges": ({"obligation_key": "aux", "required": False},),
            },
        ),
        user_obligation_keys=(),
        axis_role="auxiliary",
        budget_policy=policy,
    )
    store = _Store()
    called = []

    snapshot = _execute_plan(
        plan,
        adapter=lambda task, _: called.append(task.task_key) or _success(task),
        store=store,
        budget_policy=policy,
        max_workers=2,
    )
    stop = store.stop_records[snapshot.stop_ref]
    outcome = next(iter(store.outcomes.values()))[1]

    assert called == ["expensive_first"]
    assert outcome.budget_units == 2
    assert stop.used_budget_units == 2
    assert stop.reason == "hard_budget_reached"


def test_scheduler_rejects_a_policy_that_is_not_bound_to_the_plan() -> None:
    plan_policy = _policy()
    plan = _plan(
        (
            {
                "task_key": "required",
                "capability_id": "required",
                "edges": ({"obligation_key": "main", "required": True},),
            },
        ),
        budget_policy=plan_policy,
    )

    with pytest.raises(
        CapabilityAuthorityContractError,
        match="capability_scheduler_budget_policy_mismatch",
    ):
        _execute_plan(
            plan,
            adapter=lambda task, attempt: _success(task),
            store=_Store(),
            budget_policy=_policy(auxiliary_budget_limit=1),
        )


def test_persisted_records_have_strict_content_addressed_roundtrips() -> None:
    plan = _plan(
        (
            {
                "task_key": "region",
                "capability_id": "dimension_screen",
                "edges": ({"obligation_key": "main", "required": True},),
            },
        )
    )
    store = _Store()
    snapshot = _execute_plan(
        plan,
        adapter=lambda task, attempt: _success(task, hierarchy="region"),
        store=store,
    )
    attempt, outcome, evidence, failures = next(iter(store.outcomes.values()))
    stop = store.stop_records[snapshot.stop_ref]
    records = (
        (attempt, CapabilityAttempt),
        (outcome, CapabilityOutcome),
        (evidence[0], EvidenceLedgerEntry),
        (stop, ExplorationStopRecord),
        (snapshot, ExecutionSnapshot),
    )

    for record, record_type in records:
        assert record_type.from_dict(record.to_dict()) == record
        tampered = record.to_dict()
        tampered["unexpected"] = True
        with pytest.raises(CapabilityAuthorityContractError):
            record_type.from_dict(tampered)
        with pytest.raises(FrozenInstanceError):
            record.content_digest = "0" * 64
    assert failures == ()


def test_failure_record_roundtrip_is_strict() -> None:
    plan = _plan(
        (
            {
                "task_key": "failed",
                "capability_id": "failed",
                "edges": ({"obligation_key": "main", "required": True},),
            },
        )
    )
    store = _Store()
    _execute_plan(
        plan,
        adapter=lambda task, attempt: _technical_failure(task),
        store=store,
    )
    failure = next(iter(store.outcomes.values()))[3][0]

    assert FailureRecord.from_dict(failure.to_dict()) == failure
    tampered = failure.to_dict()
    tampered["kind"] = "hidden_real_failure"
    with pytest.raises(
        CapabilityAuthorityContractError,
        match="failure_record_(ref|digest)_invalid",
    ):
        FailureRecord.from_dict(tampered)
