from __future__ import annotations

from copy import deepcopy
from types import ModuleType, SimpleNamespace
import sys
from threading import Lock
from typing import Any, Sequence
from unittest.mock import Mock, patch

import pytest

from bi_agent.conversation.agent_core import (
    ConversationAgentCore,
    _finalize_capability_execution,
)
from bi_agent.runtime import langgraph_workflow
from bi_agent.runtime.authoritative_execution_result import (
    AuthoritativeExecutionResult,
)
from bi_agent.runtime.analysis_contracts import (
    AnalysisContract,
    analysis_contract_signature,
)
from bi_agent.runtime.capability_authority import (
    CapabilityAdapterOutput,
    CapabilityAttempt,
    CapabilityEvidence,
    CapabilityOutcome,
    EvidenceLedgerEntry,
    ExecutionSnapshot,
    ExplorationStopRecord,
    FailureRecord,
)
from bi_agent.runtime.claim_coverage import ClaimCoverageCheckpoint
from bi_agent.runtime.evidence_authority import (
    EvidenceIntegrityError,
    canonical_digest,
    canonical_value,
)
from bi_agent.runtime.evidence_taxonomy import publication_evidence_kinds
from bi_agent.runtime.durable_call_journal import InMemoryDurableCallJournal
from bi_agent.runtime.single_authority import DecisionLedger, DurableTransition
from bi_agent.runtime.runtime_persistence import CapabilitySettlementAuthority
from tests.phase7.test_single_authority_phase02 import (
    _Phase02AuthorityStore,
    _PlannerLLM,
    _authority_context,
    _decision_ledger,
    _intent_revision,
    _phase02_compile_state,
    _planner_provider_output,
    _registry,
)
from tests.phase7.test_single_authority_phase02_acceptance import _ResumeStore


class _Phase03AuthorityStore(_Phase02AuthorityStore):
    def __init__(self, ledger: DecisionLedger) -> None:
        super().__init__(ledger)
        self.attempt_journal = InMemoryDurableCallJournal()
        self.outcomes: dict[tuple[str, str], tuple] = {}
        self.settlement_authorities: dict[
            tuple[str, str], CapabilitySettlementAuthority
        ] = {}
        self.snapshots: dict[str, ExecutionSnapshot] = {}
        self.stop_records: dict[str, ExplorationStopRecord] = {}
        self.execution_transition: DurableTransition | None = None
        self.execution_transition_input: dict[str, Any] | None = None
        self.execution_transition_output: dict[str, Any] | None = None
        self.claim_coverage_checkpoint: ClaimCoverageCheckpoint | None = None
        self.claim_coverage_transition_input: dict[str, Any] | None = None
        self.claim_coverage_transition_output: dict[str, Any] | None = None
        self.loaded_outcome_task_ids: list[str] = []
        self.latest_transition_override: str | None = None
        self._execution_lock = Lock()

    def load_capability_outcome(
        self,
        plan_revision_id: str,
        task_id: str,
    ):
        with self._execution_lock:
            self.loaded_outcome_task_ids.append(task_id)
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
        bundle = (
            attempt,
            outcome,
            tuple(evidence_entries),
            tuple(failures),
        )
        key = (attempt.plan_revision_id, attempt.task_id)
        with self._execution_lock:
            accepted_authority = self.settlement_authorities.setdefault(
                key,
                settlement_authority,
            )
            if accepted_authority != settlement_authority:
                raise EvidenceIntegrityError(
                    "capability_outcome_settlement_authority_conflict"
                )
            return self.outcomes.setdefault(key, bundle)

    def load_execution_snapshot(
        self,
        plan_revision_id: str,
    ) -> ExecutionSnapshot | None:
        with self._execution_lock:
            return self.snapshots.get(plan_revision_id)

    def accept_execution_settlement(
        self,
        snapshot: ExecutionSnapshot,
        stop_record: ExplorationStopRecord,
        transition: DurableTransition,
        input_payload: dict[str, Any],
        output_payload: dict[str, Any],
        accepted_attempt_refs: Sequence[str],
    ) -> ExecutionSnapshot:
        with self._execution_lock:
            self.stop_records.setdefault(stop_record.stop_ref, stop_record)
            self.execution_transition = transition
            self.execution_transition_input = canonical_value(input_payload)
            self.execution_transition_output = canonical_value(output_payload)
            self.accepted_attempt_refs = tuple(accepted_attempt_refs)
            return self.snapshots.setdefault(
                snapshot.plan_revision_id,
                snapshot,
            )

    def load_accepted_transition(
        self,
        *,
        run_attempt_id: str,
        node_name: str,
        input_digest: str,
    ) -> dict[str, Any] | None:
        if node_name == "execute_capability_dag":
            if (
                self.execution_transition is None
                or self.execution_transition.run_attempt_id != run_attempt_id
                or self.execution_transition.input_digest != input_digest
            ):
                return None
            return {
                "transition": self.execution_transition,
                "input_payload": deepcopy(self.execution_transition_input),
                "output_payload": deepcopy(self.execution_transition_output),
            }
        if node_name == "evaluate_claim_coverage":
            checkpoint = self.claim_coverage_checkpoint
            if (
                checkpoint is None
                or checkpoint.run_attempt_id != run_attempt_id
                or checkpoint.transition.input_digest != input_digest
            ):
                return None
            return {
                "transition": checkpoint.transition,
                "input_payload": deepcopy(self.claim_coverage_transition_input),
                "output_payload": deepcopy(self.claim_coverage_transition_output),
            }
        return super().load_accepted_transition(
            run_attempt_id=run_attempt_id,
            node_name=node_name,
            input_digest=input_digest,
        )

    def save_claim_coverage_transition(self, **kwargs: Any) -> dict[str, Any]:
        checkpoint = kwargs["checkpoint"]
        assert type(checkpoint) is ClaimCoverageCheckpoint
        self.claim_coverage_checkpoint = checkpoint
        self.claim_coverage_transition_input = canonical_value(kwargs["input_payload"])
        self.claim_coverage_transition_output = canonical_value(
            kwargs["output_payload"]
        )
        assert checkpoint.transition.input_digest == canonical_digest(
            self.claim_coverage_transition_input
        )
        assert checkpoint.transition.output_digest == canonical_digest(
            self.claim_coverage_transition_output
        )
        self.attempt_journal.bind_stage(
            run_attempt_id=checkpoint.run_attempt_id,
            transition_attempt_id=checkpoint.transition.attempt_id,
            stage_name="evaluate_claim_coverage",
            attempt_refs=kwargs["accepted_attempt_refs"],
        )
        self.runs[checkpoint.run_attempt_id] = {
            "run_id": checkpoint.run_attempt_id,
            "request": {
                "claim_coverage_refs": {
                    "schema_version": checkpoint.schema_version,
                    "source_plan_revision_id": (checkpoint.source_plan_revision_id),
                    "source_execution_result_ref": (
                        checkpoint.source_execution_result_ref
                    ),
                    "claim_coverage_checkpoint_ref": checkpoint.checkpoint_ref,
                    "claim_coverage_checkpoint_digest": (checkpoint.content_digest),
                    "claim_coverage_evaluation_ref": (checkpoint.evaluation_ref),
                    "plan_expansion_decision_ref": checkpoint.decision_ref,
                    "decision": checkpoint.decision.decision,
                    "plan_patch_ref": checkpoint.plan_patch_ref,
                    "accepted_transition_id": checkpoint.transition_id,
                }
            },
        }
        return {"replayed": False}

    def get_run_state(self, run_id: str) -> dict[str, Any] | None:
        return deepcopy(self.runs.get(run_id))

    def latest_accepted_transition_id(self, run_id: str) -> str | None:
        if self.latest_transition_override is not None:
            return self.latest_transition_override
        if (
            self.claim_coverage_checkpoint is not None
            and self.claim_coverage_checkpoint.run_attempt_id == run_id
        ):
            return self.claim_coverage_checkpoint.transition_id
        if (
            self.execution_transition is not None
            and self.execution_transition.run_attempt_id == run_id
        ):
            return self.execution_transition.transition_id
        if self.transition is not None and self.transition.run_attempt_id == run_id:
            return self.transition.transition_id
        return None


class _AdapterRegistry:
    def __init__(self, runtime_registry: Any) -> None:
        self.runtime_registry = runtime_registry
        self.bind_calls: list[tuple[Any, Any]] = []
        self.executed_task_ids: list[str] = []
        self._lock = Lock()

    def validate_plan(self, plan_revision):
        assert plan_revision.capability_tasks

    def bind(self, plan_revision, runtime_inputs):
        self.bind_calls.append((plan_revision, runtime_inputs))

        def execute(task, attempt):
            assert attempt == CapabilityAttempt.create(plan_revision, task)
            with self._lock:
                self.executed_task_ids.append(task.task_id)
            obligation_by_id = {
                item.obligation_id: item for item in plan_revision.claim_obligations
            }
            supported_claim_kinds = tuple(
                sorted(
                    {
                        obligation_by_id[obligation_id].claim_kind
                        for obligation_id in task.supports_obligation_ids
                    }
                )
            )
            capability = self.runtime_registry.capability_inputs(task.capability_id)
            evidence_kind = publication_evidence_kinds(
                capability["supported_evidence_types"]
            )[0]
            evidence = CapabilityEvidence.create(
                evidence_ref=f"evidence:{task.task_id}",
                binding_record_ref=None,
                execution_state="available",
                evidence_kind=evidence_kind,
                data_contract_state="complete",
                supported_claim_kinds=supported_claim_kinds,
                evidence_strength="high",
                maximum_claim_strength=str(capability["maximum_claim_strength"]),
                observation_facts=({"fact_ref": f"fact:{task.task_id}", "value": 1},),
                scope="full_sample",
                window_refs=plan_revision.resolved_window_refs,
                dimension_path=(),
                limitation_refs=(),
                result_refs=(f"result:{task.task_id}",),
                completeness_report_refs=(f"completeness:{task.task_id}",),
                hierarchy_qualified=False,
            )
            return CapabilityAdapterOutput.create(
                status="succeeded",
                output_payload={"task_id": task.task_id, "value": 1},
                evidence=(evidence,),
                affected_obligation_ids=task.supports_obligation_ids,
                limitation_refs=(),
                retryability="never",
            )

        return execute


def _install_materializer_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Mock, object]:
    runtime_inputs = SimpleNamespace(
        settlement_authority=None,
        accepted_query_attempt_refs=(),
    )

    def materialize(**kwargs):
        plan = kwargs["plan_revision"]
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
        runtime_inputs.settlement_authority = CapabilitySettlementAuthority.create(
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
        return runtime_inputs

    materializer = Mock(side_effect=materialize)
    module = ModuleType("bi_agent.runtime.authoritative_task_inputs")
    module.materialize_authoritative_task_inputs = materializer
    monkeypatch.setitem(
        sys.modules,
        "bi_agent.runtime.authoritative_task_inputs",
        module,
    )
    return materializer, runtime_inputs


def _compiled_state(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stop_after_phase: str | None = None,
):
    registry = _registry()
    intent = _intent_revision(registry)
    ledger = _decision_ledger(intent)
    context = _authority_context(registry)
    decision_refs = tuple(record.decision_id for record in ledger.active_records())
    store = _Phase03AuthorityStore(ledger)
    state = _phase02_compile_state(
        intent=intent,
        ledger=ledger,
        registry=registry,
        store=store,
        llm_client=_PlannerLLM(
            _planner_provider_output(intent, context, decision_refs)
        ),
    )
    state["request"]["analysis_runtime"] = object()
    if stop_after_phase is not None:
        state["request"]["stop_after_phase"] = stop_after_phase
    monkeypatch.setattr(
        langgraph_workflow,
        "resolve_latest_authority_context",
        lambda **_: context,
    )
    compiled = langgraph_workflow._compile_authoritative_plan(state)
    return registry, intent, ledger, store, compiled


def _compile_and_execute(monkeypatch: pytest.MonkeyPatch):
    materializer, runtime_inputs = _install_materializer_fixture(monkeypatch)
    registry, intent, ledger, store, compiled = _compiled_state(monkeypatch)
    adapter_registry = _AdapterRegistry(registry)
    monkeypatch.setattr(
        langgraph_workflow,
        "builtin_capability_adapter_registry",
        lambda: adapter_registry,
    )
    assert langgraph_workflow._route_after_authoritative_plan(compiled) == ("execute")
    executed = langgraph_workflow._execute_capability_dag(compiled)
    return SimpleNamespace(
        registry=registry,
        intent=intent,
        ledger=ledger,
        store=store,
        state=executed,
        materializer=materializer,
        runtime_inputs=runtime_inputs,
        adapter_registry=adapter_registry,
    )


def _compile_execute_and_evaluate(monkeypatch: pytest.MonkeyPatch):
    wired = _compile_and_execute(monkeypatch)
    wired.state = langgraph_workflow._evaluate_claim_coverage(wired.state)
    return wired


def test_default_compile_route_executes_and_rebuilds_result_from_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wired = _compile_and_execute(monkeypatch)
    result = AuthoritativeExecutionResult.from_dict(wired.state["execution_result"])
    plan = result.plan_revision

    assert wired.state["workflow_status"] == "evidence_ready"
    assert result.status == "evidence_ready"
    wired.materializer.assert_called_once_with(
        plan_revision=plan,
        intent_revision=wired.intent,
        decision_ledger=wired.ledger,
        authority_context=wired.store.authority_context,
        analysis_runtime=wired.state["request"]["analysis_runtime"],
        attempt_journal=wired.store.attempt_journal,
    )
    assert wired.adapter_registry.bind_calls == [(plan, wired.runtime_inputs)]
    assert set(wired.adapter_registry.executed_task_ids) == {
        task.task_id for task in plan.capability_tasks
    }
    assert {bundle[1].task_id for bundle in result.capability_outcome_bundles} == {
        task.task_id for task in plan.capability_tasks
    }
    for bundle in result.capability_outcome_bundles:
        assert (
            bundle == wired.store.outcomes[(plan.plan_revision_id, bundle[1].task_id)]
        )
    assert set(wired.store.loaded_outcome_task_ids) >= {
        task.task_id for task in plan.capability_tasks
    }


def test_claim_coverage_checkpoint_is_the_mandatory_phase03_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wired = _compile_execute_and_evaluate(monkeypatch)
    checkpoint = wired.store.claim_coverage_checkpoint

    assert checkpoint is not None
    assert checkpoint.decision.decision == "seal"
    assert checkpoint.decision.decision_authority == (
        "deterministic_no_admissible_route"
    )
    assert checkpoint.transition.parent_transition_id == (
        wired.store.execution_transition.transition_id
    )
    assert checkpoint.transition.next_transition == "seal_authority_bundle"
    assert wired.state["claim_coverage_checkpoint"] == checkpoint
    assert wired.state["workflow_status"] == "evidence_ready"
    assert (
        wired.store.latest_accepted_transition_id(wired.intent.run_attempt_id)
        == checkpoint.transition_id
    )


def test_accepted_claim_coverage_checkpoint_replays_without_new_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wired = _compile_execute_and_evaluate(monkeypatch)
    accepted = wired.store.claim_coverage_checkpoint
    initial_llm_calls = deepcopy(wired.state["llm_calls"])

    replayed = langgraph_workflow._evaluate_claim_coverage(wired.state)

    assert replayed["claim_coverage_checkpoint"] == accepted
    assert replayed["llm_calls"] == initial_llm_calls


def test_accepted_phase03_snapshot_resumes_without_query_materialization_or_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wired = _compile_and_execute(monkeypatch)
    transition = wired.store.execution_transition
    assert transition is not None
    resumed_state = dict(wired.state)
    resumed_state["request"] = dict(wired.state["request"])
    resumed_state["durable_transition_id"] = transition.parent_transition_id
    wired.materializer.reset_mock()
    adapter_registry_factory = Mock()
    monkeypatch.setattr(
        langgraph_workflow,
        "builtin_capability_adapter_registry",
        adapter_registry_factory,
    )

    replayed = langgraph_workflow._execute_capability_dag(resumed_state)

    wired.materializer.assert_not_called()
    adapter_registry_factory.assert_not_called()
    assert replayed["execution_result"] == wired.state["execution_result"]


def test_explicit_phase02_stop_is_planned_without_materialization_or_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    materializer, _ = _install_materializer_fixture(monkeypatch)
    adapter_registry_factory = Mock()
    monkeypatch.setattr(
        langgraph_workflow,
        "builtin_capability_adapter_registry",
        adapter_registry_factory,
    )
    _, _, _, _, compiled = _compiled_state(
        monkeypatch,
        stop_after_phase="phase02",
    )

    assert compiled["workflow_status"] == "planned"
    assert langgraph_workflow._route_after_authoritative_plan(compiled) == ("stop")
    assert compiled["plan_result"]["status"] == "planned"
    materializer.assert_not_called()
    adapter_registry_factory.assert_not_called()


def _finalize(wired) -> dict[str, Any]:
    return _finalize_capability_execution(
        store=wired.store,
        plan_result=wired.state["plan_result"],
        execution_result=wired.state["execution_result"],
        run_id=wired.intent.run_attempt_id,
        thread_id="thread-phase03-core",
        turn_id="turn-phase03-core",
        topic_id="topic-phase03-core",
        request={
            "question": wired.intent.original_user_text,
            "stop_after_phase": "phase03",
        },
        context_manifest={"manifest_id": "manifest-phase03-core"},
        turn_intent="new_topic",
        topic_relation="new_topic",
        llm_calls=tuple(wired.state["llm_calls"]),
    )


def test_core_finalizer_accepts_complete_authority_and_sets_evidence_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wired = _compile_execute_and_evaluate(monkeypatch)

    finalized = _finalize(wired)

    assert finalized["status"] == "evidence_ready"
    persisted_run = wired.store.runs[wired.intent.run_attempt_id]
    assert persisted_run["status"] == "evidence_ready"
    assert persisted_run["request"]["plan_result_refs"]
    assert persisted_run["request"]["execution_result_refs"]
    assert any(
        event["event_type"] == "capability_execution_settled"
        for event in wired.store.audit_events
    )


@pytest.mark.parametrize(
    ("corruption", "expected_error"),
    (
        ("active_plan", "single_authority_plan_persistence_mismatch"),
        ("snapshot", "authoritative_execution_persistence_mismatch"),
        ("outcome", "authoritative_execution_persistence_mismatch"),
        ("settlement_transition", "authoritative_execution_transition_mismatch"),
        ("latest_head", "authoritative_execution_transition_mismatch"),
    ),
)
def test_core_finalizer_rejects_authority_closure_tampering(
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
    expected_error: str,
) -> None:
    wired = _compile_execute_and_evaluate(monkeypatch)
    parsed = AuthoritativeExecutionResult.from_dict(wired.state["execution_result"])
    persisted_run_before = deepcopy(wired.store.runs[wired.intent.run_attempt_id])

    if corruption == "active_plan":
        wired.store.plan_revision = None
    elif corruption == "snapshot":
        outcomes = tuple(bundle[1] for bundle in parsed.capability_outcome_bundles)
        evidence = tuple(
            entry for bundle in parsed.capability_outcome_bundles for entry in bundle[2]
        )
        failures = tuple(
            failure
            for bundle in parsed.capability_outcome_bundles
            for failure in bundle[3]
        )
        altered_stop = ExplorationStopRecord.create(
            parsed.plan_revision,
            outcomes,
            reason="no_ready_tasks",
            hard_budget_limit=None,
        )
        wired.store.snapshots[parsed.plan_revision_id] = ExecutionSnapshot.create(
            parsed.plan_revision,
            altered_stop,
            outcomes,
            evidence,
            failures,
        )
    elif corruption == "outcome":
        first = parsed.capability_outcome_bundles[0][1]
        wired.store.outcomes.pop((parsed.plan_revision_id, first.task_id))
    elif corruption == "settlement_transition":
        transition = parsed.durable_transition
        wired.store.execution_transition = DurableTransition.create(
            node_name=transition.node_name,
            parent_transition_id=transition.parent_transition_id,
            run_attempt_id=transition.run_attempt_id,
            intent_revision_id=transition.intent_revision_id,
            decision_ledger_position=transition.decision_ledger_position,
            input_digest=transition.input_digest,
            output_digest=transition.output_digest,
            execution_attempt=transition.execution_attempt,
            provider_ref="tampered-provider",
            model_ref=transition.model_ref,
            status=transition.status,
            acceptance_state=transition.acceptance_state,
            next_transition=transition.next_transition,
            started_at=transition.started_at,
            finished_at=transition.finished_at,
        )
    elif corruption == "latest_head":
        wired.store.latest_transition_override = wired.store.transition.transition_id
    else:
        raise AssertionError(corruption)

    with pytest.raises(EvidenceIntegrityError, match=expected_error):
        _finalize(wired)
    assert wired.store.runs[wired.intent.run_attempt_id] == persisted_run_before


def test_clarification_resume_accepts_evidence_ready_and_rebinds_runtime(
    tmp_path,
) -> None:
    registry = _registry()
    intent = _intent_revision(registry)
    ledger = _decision_ledger(intent)
    decision = ledger.active_records()[0]
    transition = DurableTransition.create(
        node_name="accept_material_decision",
        parent_transition_id="transition-waiting",
        run_attempt_id=intent.run_attempt_id,
        intent_revision_id=intent.intent_revision_id,
        decision_ledger_position=ledger.position,
        input_digest=canonical_digest({"decision": decision.decision_id}),
        output_digest=canonical_digest({"decision": decision.to_dict()}),
        execution_attempt=1,
        provider_ref="user_protocol",
        model_ref="stable_option_contract",
        status="succeeded",
        acceptance_state="accepted",
        next_transition="compile_authoritative_plan",
    )
    artifact_root = str(tmp_path / "phase03-resume")
    context_manifest = {"manifest_id": "manifest-phase03-resume"}
    waiting_request = {
        "schema_version": "single-authority-phase02-waiting.v1",
        "run_attempt_id": intent.run_attempt_id,
        "thread_id": "thread-phase03-resume",
        "turn_id": "turn-phase03-resume",
        "topic_id": "topic-phase03-resume",
        "turn_intent": "new_topic",
        "topic_relation": "new_topic",
        "intent_revision_id": intent.intent_revision_id,
        "decision_ledger_position": 0,
        "accepted_transition_id": "transition-waiting",
        "clarification": {"slot_id": "comparison_baseline"},
        "context_manifest_ref": context_manifest["manifest_id"],
        "runtime_descriptors": {
            "run_id": intent.run_attempt_id,
            "run_attempt_id": intent.run_attempt_id,
            "question": intent.original_user_text,
            "artifact_root": artifact_root,
            "analysis_context": {},
            "context_manifest": context_manifest,
        },
    }
    store = _ResumeStore(
        intent=intent,
        ledger=ledger,
        transition=transition,
        waiting_request=waiting_request,
    )
    analysis_runtime = object()
    captured: dict[str, Any] = {}

    def workflow(request: dict[str, Any]) -> SimpleNamespace:
        captured.update(request)
        return SimpleNamespace(
            status="evidence_ready",
            plan_result={"status": "planned"},
            execution_result={"status": "evidence_ready"},
            failure_reason="",
            checkpoint_events=(),
            llm_calls=(),
        )

    core = ConversationAgentCore(
        store,
        workflow_runner=workflow,
        conversation_llm_client=object(),
        runtime_registry=registry,
        release_resolver=store,
        analysis_runtime=analysis_runtime,
    )
    decision_result = {
        "status": "decision_recorded",
        "run_id": intent.run_attempt_id,
        "intent_revision_id": intent.intent_revision_id,
        "decision": decision.to_dict(),
        "decision_ledger": {
            "position": ledger.position,
            "records": [decision.to_dict()],
        },
        "durable_checkpoint": transition.to_dict(),
        "llm_calls": [],
    }
    finalized = {
        "status": "evidence_ready",
        "run_id": intent.run_attempt_id,
    }
    with patch(
        "bi_agent.conversation.agent_core._finalize_capability_execution",
        return_value=finalized,
    ) as finalize:
        result = core._resume_authoritative_plan_after_decision(
            thread_id="thread-phase03-resume",
            run_id=intent.run_attempt_id,
            artifact_root=artifact_root,
            decision_result=decision_result,
            stop_after_phase="phase03",
        )

    assert result == finalized
    assert captured["analysis_runtime"] is analysis_runtime
    assert captured["stop_after_phase"] == "phase03"
    assert finalize.call_count == 1
    assert (
        finalize.call_args.kwargs["expected_plan_parent_transition_id"]
        == transition.transition_id
    )
