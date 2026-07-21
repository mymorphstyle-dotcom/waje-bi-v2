from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

import pytest

from bi_agent.runtime import langgraph_workflow
from bi_agent.runtime.authoritative_execution_result import (
    AuthoritativeExecutionResult,
)
from bi_agent.runtime.capability_authority import (
    CapabilityAdapterOutput,
    CapabilityEvidence,
)
from bi_agent.runtime.durable_call_journal import DurableCallSpec
from bi_agent.runtime.evidence_authority import canonical_value
from bi_agent.runtime.plan_authority import AuthorityContext, PlanRevision
from bi_agent.runtime.single_authority import DurableTransition
from tests.phase7.test_single_authority_phase02 import (
    _PlannerLLM,
    _authority_context,
    _decision_ledger,
    _intent_revision,
    _phase02_compile_state,
    _planner_provider_output,
    _registry,
)
from tests.phase7.test_single_authority_phase03_workflow_core import (
    _AdapterRegistry,
    _Phase03AuthorityStore,
    _install_materializer_fixture,
)


SELECTED_AXIS_ID = "market_context"


class _PatchLoopStore(_Phase03AuthorityStore):
    def __init__(self, ledger) -> None:
        super().__init__(ledger)
        self.plan_transitions: dict[str, dict[str, Any]] = {}
        self.head_transition_id: str | None = None

    def save_plan_revision_transition(self, **kwargs: Any) -> dict[str, Any]:
        result = super().save_plan_revision_transition(**kwargs)
        transition = kwargs["transition"]
        plan_revision = kwargs["plan_revision"]
        self.plan_transitions[plan_revision.plan_revision_id] = {
            "transition": transition,
            "input_payload": canonical_value(kwargs["input_payload"]),
            "output_payload": canonical_value(kwargs["output_payload"]),
        }
        self.head_transition_id = transition.transition_id
        return result

    def load_plan_revision_transition(
        self, plan_revision_id: str
    ) -> dict[str, Any] | None:
        record = self.plan_transitions.get(plan_revision_id)
        return None if record is None else deepcopy(record)

    def accept_execution_settlement(self, *args: Any, **kwargs: Any):
        result = super().accept_execution_settlement(*args, **kwargs)
        transition = kwargs.get("transition")
        if transition is None:
            transition = args[2]
        self.head_transition_id = transition.transition_id
        return result

    def save_claim_coverage_transition(self, **kwargs: Any) -> dict[str, Any]:
        result = super().save_claim_coverage_transition(**kwargs)
        self.head_transition_id = kwargs["checkpoint"].transition_id
        return result

    def latest_accepted_transition_id(self, run_id: str) -> str | None:
        if self.resolve_active_plan_revision(run_id) is None:
            return None
        return self.head_transition_id


class _PartialSourceEvidenceAdapterRegistry(_AdapterRegistry):
    def __init__(
        self,
        runtime_registry: Any,
        *,
        partial_task_keys: Sequence[str],
    ) -> None:
        super().__init__(runtime_registry)
        self.partial_task_keys = frozenset(partial_task_keys)

    def bind(self, plan_revision, runtime_inputs):
        execute = super().bind(plan_revision, runtime_inputs)

        def execute_with_source_limit(task, attempt):
            output = execute(task, attempt)
            if task.task_key not in self.partial_task_keys:
                return output
            evidence = []
            for item in output.evidence:
                payload = item.to_dict()
                payload.pop("content_digest")
                payload["data_contract_state"] = "partial"
                payload["limitation_refs"] = (
                    "limitation:test-source-evidence-partial",
                )
                evidence.append(CapabilityEvidence.create(**payload))
            return CapabilityAdapterOutput.create(
                status=output.status,
                output_payload=output.output_payload,
                evidence=evidence,
                affected_obligation_ids=output.affected_obligation_ids,
                limitation_refs=("limitation:test-source-evidence-partial",),
                retryability=output.retryability,
            )

        return execute_with_source_limit


def _authority_context_with_market(
    registry,
    *,
    intent,
) -> AuthorityContext:
    base = _authority_context(registry)
    market_release_ref = "release:market-dashboard:r1"
    market_snapshot_ref = "snapshot:market-dashboard:r1"
    channel_snapshot_ref = "snapshot:market-dashboard-channel:r1"
    return AuthorityContext.create(
        run_attempt_id=intent.run_attempt_id,
        actual_as_of=base.actual_as_of,
        release_refs=(*base.release_refs, market_release_ref),
        snapshot_refs=(
            *base.snapshot_refs,
            market_snapshot_ref,
            channel_snapshot_ref,
        ),
        dataset_coverage=(
            *base.dataset_coverage,
            {
                "dataset_id": "market_dashboard",
                "availability": "claim_ready",
                "release_ref": market_release_ref,
                "snapshot_refs": (market_snapshot_ref,),
                "limitation_ref": None,
            },
            {
                "dataset_id": "market_dashboard_channel",
                "availability": "claim_ready",
                "release_ref": market_release_ref,
                "snapshot_refs": (channel_snapshot_ref,),
                "limitation_ref": None,
            },
        ),
        contract_versions=base.contract_versions,
    )


def _execute_source_plan(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    _install_materializer_fixture(monkeypatch)
    registry = _registry()
    intent = _intent_revision(registry)
    ledger = _decision_ledger(intent)
    authority_context = _authority_context_with_market(
        registry,
        intent=intent,
    )
    decision_refs = tuple(record.decision_id for record in ledger.active_records())
    initial_output = _planner_provider_output(
        intent,
        authority_context,
        decision_refs,
    )
    store = _PatchLoopStore(ledger)
    state = _phase02_compile_state(
        intent=intent,
        ledger=ledger,
        registry=registry,
        store=store,
        llm_client=_PlannerLLM(initial_output),
    )
    state["request"]["analysis_runtime"] = object()
    monkeypatch.setattr(
        langgraph_workflow,
        "resolve_latest_authority_context",
        lambda **_: authority_context,
    )
    state = langgraph_workflow._compile_authoritative_plan(state)
    source_plan = PlanRevision.from_dict(state["plan_revision"])
    comparative_obligation_ids = {
        obligation.obligation_id
        for obligation in source_plan.claim_obligations
        if obligation.claim_kind == "comparative_change"
    }
    partial_task_keys = {
        task.task_key
        for task in source_plan.capability_tasks
        if comparative_obligation_ids.intersection(task.supports_obligation_ids)
    }
    adapter_registry = _PartialSourceEvidenceAdapterRegistry(
        registry,
        partial_task_keys=partial_task_keys,
    )
    monkeypatch.setattr(
        langgraph_workflow,
        "builtin_capability_adapter_registry",
        lambda: adapter_registry,
    )
    state = langgraph_workflow._execute_capability_dag(state)
    return SimpleNamespace(
        registry=registry,
        intent=intent,
        ledger=ledger,
        authority_context=authority_context,
        decision_refs=decision_refs,
        initial_output=initial_output,
        store=store,
        source_plan=source_plan,
        adapter_registry=adapter_registry,
        state=state,
    )


def _patch_planner_output(source_output: Mapping[str, Any]) -> dict[str, Any]:
    output = deepcopy(source_output)
    output["auxiliary_axes"].append(
        {
            "proposal_item_id": "proposal-axis-market-context-patch",
            "axis_id": SELECTED_AXIS_ID,
            "rationale": "补充市场指标基准以关闭主结论的对照证据缺口。",
            "supports_claim_kinds": ["comparative_change"],
        }
    )
    output["priority_proposals"].append(
        {
            "proposal_item_id": "proposal-priority-market-context-patch",
            "target_ref": SELECTED_AXIS_ID,
            "rationale": "按覆盖决策只扩展已选中的市场对照轴。",
        }
    )
    return output


def _task_contract_projection(
    task,
    *,
    plan: PlanRevision,
) -> dict[str, Any]:
    task_key_by_id = {item.task_id: item.task_key for item in plan.capability_tasks}
    return {
        "task_key": task.task_key,
        "authority_context_ref": task.authority_context_ref,
        "capability_id": task.capability_id,
        "normalized_input_refs": task.normalized_input_refs,
        "dependency_task_keys": tuple(
            task_key_by_id[item] for item in task.dependency_task_ids
        ),
        "obligation_edges": canonical_value(task.obligation_edges),
        "supports_obligation_ids": task.supports_obligation_ids,
        "execution_rank": task.execution_rank,
        "declared_budget_units": task.declared_budget_units,
        "governor_inputs": canonical_value(task.governor_inputs),
        "execution_policy": canonical_value(task.execution_policy),
    }


def test_plan_patch_provider_scope_is_bound_to_the_source_plan() -> None:
    spec = DurableCallSpec.create(
        run_attempt_id="run-plan-patch-scope",
        intent_revision_id="intent-plan-patch-scope",
        plan_revision_id="plan-plan-patch-scope",
        task_id=None,
        stage_name="compile_plan_patch",
        call_kind="plan_patch_provider",
        operation_name="single_authority_plan_patch_proposal",
        input_ref="input:plan-patch-scope",
        input_payload={"selected_axis_ids": [SELECTED_AXIS_ID]},
    )

    assert spec.call_kind == "plan_patch_provider"
    assert spec.intent_revision_id == "intent-plan-patch-scope"
    assert spec.plan_revision_id == "plan-plan-patch-scope"
    assert spec.task_id is None


def test_provider_selected_plan_patch_reexecutes_and_replays_without_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wired = _execute_source_plan(monkeypatch)

    scheduled_axis = wired.source_plan.analysis_axes[0].axis_id
    invalid_provider = _PlannerLLM(
        {"decision": "patch", "selected_axis_ids": [scheduled_axis]}
    )
    wired.state["llm_client"] = invalid_provider
    with pytest.raises(
        langgraph_workflow.WorkflowFailure,
        match="plan_expansion_provider_output_invalid",
    ):
        langgraph_workflow._evaluate_claim_coverage(wired.state)
    assert invalid_provider.calls
    assert wired.store.claim_coverage_checkpoint is None

    decision_output = {
        "decision": "patch",
        "selected_axis_ids": [SELECTED_AXIS_ID],
    }
    decision_provider = _PlannerLLM(decision_output)
    wired.state["llm_client"] = decision_provider
    wired.state = langgraph_workflow._evaluate_claim_coverage(wired.state)
    checkpoint = wired.state["claim_coverage_checkpoint"]
    decision = checkpoint.decision

    assert tuple(
        route.axis_id for route in checkpoint.evaluation.admissible_routes
    ) == (SELECTED_AXIS_ID,)
    assert set(checkpoint.evaluation.scheduled_axis_ids).isdisjoint(
        route.axis_id for route in checkpoint.evaluation.admissible_routes
    )
    assert decision.decision == "patch"
    assert decision.decision_authority == "provider"
    assert decision.selected_axis_ids == (SELECTED_AXIS_ID,)
    assert canonical_value(decision.structured_output) == decision_output
    assert json.loads(decision.raw_response_content) == decision_output
    assert checkpoint.plan_patch is not None
    assert checkpoint.plan_patch.selected_axis_ids == (SELECTED_AXIS_ID,)
    assert len(decision_provider.calls) == 1
    coverage_attempt_refs = wired.store.attempt_journal.load_stage_attempt_refs(
        run_attempt_id=wired.intent.run_attempt_id,
        transition_attempt_id=checkpoint.transition.attempt_id,
        stage_name="evaluate_claim_coverage",
    )
    assert len(coverage_attempt_refs) == 1

    coverage_replay_provider = _PlannerLLM(
        decision_output,
        allow_calls=False,
    )
    wired.state["llm_client"] = coverage_replay_provider
    llm_calls_before_replay = deepcopy(wired.state["llm_calls"])
    replayed_coverage = langgraph_workflow._evaluate_claim_coverage(wired.state)
    assert replayed_coverage["claim_coverage_checkpoint"] == checkpoint
    assert replayed_coverage["llm_calls"] == llm_calls_before_replay
    assert coverage_replay_provider.calls == []

    patch_output = _patch_planner_output(wired.initial_output)
    patch_provider = _PlannerLLM(patch_output)
    wired.state["llm_client"] = patch_provider
    wired.state = langgraph_workflow._compile_plan_patch(wired.state)
    successor_plan = PlanRevision.from_dict(wired.state["plan_revision"])
    successor_transition = DurableTransition.from_dict(
        wired.state["durable_checkpoint"]
    )

    source_axis_ids = {axis.axis_id for axis in wired.source_plan.analysis_axes}
    successor_axis_ids = {axis.axis_id for axis in successor_plan.analysis_axes}
    assert successor_axis_ids - source_axis_ids == {SELECTED_AXIS_ID}
    source_axes = {axis.axis_id: axis for axis in wired.source_plan.analysis_axes}
    successor_axes = {axis.axis_id: axis for axis in successor_plan.analysis_axes}
    assert all(
        successor_axes[axis_id] == source_axis
        for axis_id, source_axis in source_axes.items()
    )
    source_obligations = {
        obligation.obligation_id: obligation
        for obligation in wired.source_plan.claim_obligations
    }
    successor_obligations = {
        obligation.obligation_id: obligation
        for obligation in successor_plan.claim_obligations
    }
    assert set(source_obligations).issubset(successor_obligations)
    assert all(
        successor_obligations[obligation_id] == source_obligation
        for obligation_id, source_obligation in source_obligations.items()
    )
    source_tasks = {task.task_key: task for task in wired.source_plan.capability_tasks}
    successor_tasks = {task.task_key: task for task in successor_plan.capability_tasks}
    assert set(source_tasks).issubset(successor_tasks)
    assert all(
        _task_contract_projection(source_task, plan=wired.source_plan)
        == _task_contract_projection(
            successor_tasks[task_key],
            plan=successor_plan,
        )
        for task_key, source_task in source_tasks.items()
    )
    assert successor_plan.supersedes_plan_revision_id == (
        wired.source_plan.plan_revision_id
    )
    assert successor_plan.authority_context_ref == (
        wired.source_plan.authority_context_ref
    )
    assert successor_plan.decision_refs == wired.source_plan.decision_refs
    assert successor_plan.resolved_window_refs == (
        wired.source_plan.resolved_window_refs
    )
    assert successor_plan.budget_policy_ref == (wired.source_plan.budget_policy_ref)
    assert successor_plan.contract_versions == (wired.source_plan.contract_versions)
    assert successor_transition.node_name == "compile_plan_patch"
    assert successor_transition.parent_transition_id == (checkpoint.transition_id)
    assert successor_transition.next_transition == "phase03_plan_patch_bound"
    assert len(patch_provider.calls) == 1
    assert set(patch_provider.calls[0]["required_keys"]) == {
        "issue_tree",
        "auxiliary_axes",
        "hypotheses",
        "priority_proposals",
        "assumption_proposals",
    }
    patch_audit = next(
        item
        for item in reversed(wired.state["llm_calls"])
        if item["task"] == "single_authority_plan_patch_proposal"
    )
    assert canonical_value(patch_audit["structured_output"]) == (
        canonical_value(patch_output)
    )
    patch_attempt_refs = wired.store.attempt_journal.load_stage_attempt_refs(
        run_attempt_id=wired.intent.run_attempt_id,
        transition_attempt_id=successor_transition.attempt_id,
        stage_name="compile_plan_patch",
    )
    assert len(patch_attempt_refs) == 1

    plan_replay_provider = _PlannerLLM(patch_output, allow_calls=False)
    resumed = langgraph_workflow._compile_authoritative_plan(
        _phase02_compile_state(
            intent=wired.intent,
            ledger=wired.ledger,
            registry=wired.registry,
            store=wired.store,
            llm_client=plan_replay_provider,
        )
    )
    assert resumed["plan_revision"] == successor_plan.to_dict()
    assert plan_replay_provider.calls == []

    executed_before_successor = tuple(wired.adapter_registry.executed_task_ids)
    wired.state["llm_client"] = _PlannerLLM({}, allow_calls=False)
    wired.state = langgraph_workflow._execute_capability_dag(wired.state)
    successor_execution = AuthoritativeExecutionResult.from_dict(
        wired.state["execution_result"]
    )
    assert successor_execution.plan_revision == successor_plan
    assert successor_execution.durable_transition.parent_transition_id == (
        successor_transition.transition_id
    )
    assert set(
        wired.adapter_registry.executed_task_ids[len(executed_before_successor) :]
    ) == {task.task_id for task in successor_plan.capability_tasks}

    final_provider = _PlannerLLM({}, allow_calls=False)
    wired.state["llm_client"] = final_provider
    wired.state = langgraph_workflow._evaluate_claim_coverage(wired.state)
    final_checkpoint = wired.state["claim_coverage_checkpoint"]
    assert final_checkpoint.source_plan_revision_id == (successor_plan.plan_revision_id)
    assert final_checkpoint.decision.decision == "seal"
    assert final_checkpoint.decision.decision_authority == (
        "deterministic_no_admissible_route"
    )
    assert set(source_obligations).issubset(
        final_checkpoint.evaluation.unresolved_obligation_ids
    )
    assert all(
        coverage.status == "evidence_present"
        for coverage in final_checkpoint.evaluation.obligation_coverages
        if coverage.obligation_id in source_obligations
    )
    assert final_checkpoint.evaluation.admissible_routes == ()
    assert final_provider.calls == []
