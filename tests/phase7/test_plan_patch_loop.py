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


SELECTED_AXIS_ID = "anomaly_validation"


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
            *(
                item
                for item in base.dataset_coverage
                if item["dataset_id"]
                not in {"market_dashboard", "market_dashboard_channel"}
            ),
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
            "proposal_item_id": "proposal-axis-anomaly-validation-patch",
            "axis_id": SELECTED_AXIS_ID,
            "rationale": "补充异常成立性验证以关闭主结论的稳健性证据缺口。",
            "supports_claim_kinds": ["comparative_change"],
        }
    )
    output["priority_proposals"].append(
        {
            "proposal_item_id": "proposal-priority-anomaly-validation-patch",
            "target_ref": SELECTED_AXIS_ID,
            "rationale": "按覆盖决策只扩展已选中的异常验证轴。",
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


def test_full_factor_plan_has_no_unexplored_patch_route_and_skips_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wired = _execute_source_plan(monkeypatch)
    provider = _PlannerLLM({}, allow_calls=False)
    wired.state["llm_client"] = provider

    settled = langgraph_workflow._evaluate_claim_coverage(wired.state)
    checkpoint = settled["claim_coverage_checkpoint"]

    assert checkpoint.evaluation.admissible_routes == ()
    assert checkpoint.decision.decision == "seal"
    assert checkpoint.decision.decision_authority == (
        "deterministic_no_admissible_route"
    )
    assert checkpoint.plan_patch is None
    assert provider.calls == []
    assert {
        ref.removeprefix("factor-domain:")
        for task in wired.source_plan.capability_tasks
        for ref in task.normalized_input_refs
        if ref.startswith("factor-domain:")
    } == set(wired.registry.factor_domain_ids)
