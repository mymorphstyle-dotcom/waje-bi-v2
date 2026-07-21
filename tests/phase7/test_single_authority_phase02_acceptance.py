from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from bi_agent.conversation.agent_core import ConversationAgentCore
from bi_agent.runtime import langgraph_workflow
from bi_agent.runtime.authoritative_plan_result import (
    parse_authoritative_plan_result,
)
from bi_agent.runtime.evidence_authority import canonical_digest
from bi_agent.runtime.plan_authority import AuthorityContext
from bi_agent.runtime.single_authority import DecisionLedger, DurableTransition
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
from tools.phase7.run_single_authority_phase02_acceptance import (
    _review_persistence,
    _review_planned_result,
    _review_start_persistence,
    _review_start_result,
    _resume_artifact_root,
    _selected_option,
)


def test_phase02_acceptance_reviews_real_compiled_case_b_plan() -> None:
    registry = _registry()
    intent = _intent_revision(registry)
    ledger = _decision_ledger(intent)
    context = _authority_context(registry)
    decision_refs = tuple(record.decision_id for record in ledger.active_records())
    store = _Phase02AuthorityStore(ledger)
    state = _phase02_compile_state(
        intent=intent,
        ledger=ledger,
        registry=registry,
        store=store,
        llm_client=_PlannerLLM(
            _planner_provider_output(intent, context, decision_refs)
        ),
    )
    with patch.object(
        langgraph_workflow,
        "resolve_latest_authority_context",
        return_value=context,
    ):
        planned = langgraph_workflow._compile_authoritative_plan(state)

    parsed = parse_authoritative_plan_result(
        planned["plan_result"],
        expected_run_id=intent.run_attempt_id,
        expected_llm_calls=planned["llm_calls"],
    )
    review = _review_planned_result(parsed)
    persistence = _review_persistence(store, parsed)
    assert review["passed"] is True
    assert persistence["passed"] is True
    assert {
        "change_validation",
        "formula_tree",
        "dimension_localization",
        "time_context",
        "data_quality",
    } <= set(review["axis_ids"])
    assert any(
        boundary["dataset_id"] == "payment_attempt"
        and boundary["availability"] == "missing_contract"
        for boundary in review["task_boundaries"]
    )

    tampered = {
        **planned["plan_result"],
        "authority_refs": {
            **planned["plan_result"]["authority_refs"],
            "plan_revision_id": "plan-revision-tampered",
        },
    }
    with pytest.raises(
        ValueError,
        match="single_authority_plan_authority_mismatch",
    ):
        parse_authoritative_plan_result(
            tampered,
            expected_run_id=intent.run_attempt_id,
            expected_llm_calls=planned["llm_calls"],
        )


def test_phase02_acceptance_start_requires_explicit_human_selection() -> None:
    result = {
        "status": "waiting_for_clarification",
        "run_id": "run-phase02-acceptance",
        "intent_revision": {"intent_revision_id": "intent-phase02"},
        "decision_ledger": {"position": 0, "records": []},
        "durable_checkpoint": {"transition_id": "transition-waiting"},
        "clarification": {
            "options": [
                {
                    "option_id": "comparison_baseline.previous_day",
                    "label": "跟前一天比较（推荐）",
                    "description": "用于日变化解释。",
                    "recommended": True,
                },
                {
                    "option_id": "comparison_baseline.rolling_7_day_baseline",
                    "label": "跟过去七天比较",
                    "description": "用于平滑短期波动。",
                    "recommended": False,
                },
                {
                    "option_id": "tell_agent_differently",
                    "label": "告诉分析助手采用其他方式",
                    "description": "自由说明业务选择。",
                    "recommended": False,
                },
            ]
        },
    }

    review = _review_start_result(
        result,
        run_id="run-phase02-acceptance",
    )

    assert review["passed"] is True
    assert review["human_selection_required"] is True
    start = {"result": result}
    assert (
        _selected_option(start, "comparison_baseline.previous_day")["recommended"]
        is True
    )
    with pytest.raises(ValueError, match="option_id_invalid"):
        _selected_option(start, "tell_agent_differently")
    with pytest.raises(ValueError, match="option_id_unknown"):
        _selected_option(start, "comparison_baseline.unknown")


def test_phase02_acceptance_start_reviews_persisted_authority_head() -> None:
    registry = _registry()
    intent = _intent_revision(registry)
    ledger = DecisionLedger()
    transition_input = {"intent_revision_id": intent.intent_revision_id}
    transition_output = {"status": "waiting_for_clarification"}
    transition = DurableTransition.create(
        node_name="persist_waiting_for_decision",
        parent_transition_id="transition-intent-bound",
        run_attempt_id=intent.run_attempt_id,
        intent_revision_id=intent.intent_revision_id,
        decision_ledger_position=0,
        input_digest=canonical_digest(transition_input),
        output_digest=canonical_digest(transition_output),
        execution_attempt=1,
        provider_ref="local_deterministic",
        model_ref="contract_policy",
        status="succeeded",
        acceptance_state="accepted",
        next_transition="await_user_decision",
    )
    store = _StartStore(
        intent=intent,
        ledger=ledger,
        transition=transition,
        transition_input=transition_input,
        transition_output=transition_output,
    )
    result = {
        "run_id": intent.run_attempt_id,
        "intent_revision": intent.to_dict(),
        "decision_ledger": {"position": 0, "records": []},
        "durable_checkpoint": transition.to_dict(),
    }

    review = _review_start_persistence(store, result)

    assert review == {
        "passed": True,
        "intent_revision_id": intent.intent_revision_id,
        "decision_ledger_position": 0,
        "accepted_transition_id": transition.transition_id,
    }


def test_phase02_acceptance_rejects_runtime_binding_version_drift() -> None:
    registry = _registry()
    intent = _intent_revision(registry)
    ledger = _decision_ledger(intent)
    context = _authority_context(registry)
    store = _Phase02AuthorityStore(ledger)
    state = _phase02_compile_state(
        intent=intent,
        ledger=ledger,
        registry=registry,
        store=store,
        llm_client=_PlannerLLM(
            _planner_provider_output(
                intent,
                context,
                tuple(record.decision_id for record in ledger.active_records()),
            )
        ),
    )
    with patch.object(
        langgraph_workflow,
        "resolve_latest_authority_context",
        return_value=context,
    ):
        planned = langgraph_workflow._compile_authoritative_plan(state)
    parsed = parse_authoritative_plan_result(
        planned["plan_result"],
        expected_run_id=intent.run_attempt_id,
        expected_llm_calls=planned["llm_calls"],
    )
    drifted_context = AuthorityContext.create(
        run_attempt_id=context.run_attempt_id,
        actual_as_of=context.actual_as_of,
        release_refs=context.release_refs,
        snapshot_refs=context.snapshot_refs,
        dataset_coverage=context.dataset_coverage,
        contract_versions={
            **dict(context.contract_versions),
            "runtime_bindings": "runtime-bindings-drifted",
        },
    )

    with pytest.raises(
        ValueError,
        match="phase02_acceptance_runtime_contract_drift",
    ):
        _review_planned_result(replace(parsed, authority_context=drifted_context))


def test_core_resumes_plan_from_persisted_intent_and_decision_head() -> None:
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
    artifact_root = "artifacts/phase7/phase02-resume-test"
    manifest = {"manifest_id": "manifest-phase02-resume"}
    waiting_request = {
        "schema_version": "single-authority-phase02-waiting.v1",
        "run_attempt_id": intent.run_attempt_id,
        "thread_id": "thread-phase02-resume",
        "turn_id": "turn-phase02-resume",
        "topic_id": "topic-phase02-resume",
        "turn_intent": "new_topic",
        "topic_relation": "new_topic",
        "intent_revision_id": intent.intent_revision_id,
        "decision_ledger_position": 0,
        "accepted_transition_id": "transition-waiting",
        "clarification": {"slot_id": "comparison_baseline"},
        "context_manifest_ref": manifest["manifest_id"],
        "runtime_descriptors": {
            "run_id": intent.run_attempt_id,
            "run_attempt_id": intent.run_attempt_id,
            "question": intent.original_user_text,
            "artifact_root": artifact_root,
            "analysis_context": {},
            "context_manifest": manifest,
        },
    }
    store = _ResumeStore(
        intent=intent,
        ledger=ledger,
        transition=transition,
        waiting_request=waiting_request,
    )
    captured: dict[str, Any] = {}

    def workflow(request: dict[str, Any]) -> SimpleNamespace:
        captured.update(request)
        return SimpleNamespace(
            status="planned",
            plan_result={"status": "planned"},
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
        "status": "planned",
        "run_id": intent.run_attempt_id,
    }
    with patch(
        "bi_agent.conversation.agent_core._finalize_authoritative_plan",
        return_value=finalized,
    ) as finalize:
        result = core._resume_authoritative_plan_after_decision(
            thread_id="thread-phase02-resume",
            run_id=intent.run_attempt_id,
            artifact_root=artifact_root,
            decision_result=decision_result,
            stop_after_phase="phase02",
        )

    assert result == finalized
    assert captured["question"] == intent.original_user_text
    assert captured["authority_store"] is store
    assert captured["runtime_registry"] is registry
    assert captured["release_resolver"] is store
    assert captured["run_attempt_id"] == intent.run_attempt_id
    assert store.run_updates[-1]["status"] == "running_workflow"
    assert finalize.call_count == 1
    assert finalize.call_args.kwargs["expected_parent_transition_id"] == (
        transition.transition_id
    )


def test_acceptance_resume_uses_persisted_artifact_root_authority() -> None:
    registry = _registry()
    intent = _intent_revision(registry)
    ledger = _decision_ledger(intent)
    artifact_root = "artifacts/phase7/phase02-resume-test"
    store = _ResumeStore(
        intent=intent,
        ledger=ledger,
        transition=DurableTransition.create(
            node_name="accept_material_decision",
            parent_transition_id="transition-waiting",
            run_attempt_id=intent.run_attempt_id,
            intent_revision_id=intent.intent_revision_id,
            decision_ledger_position=ledger.position,
            input_digest=canonical_digest({"decision": "accepted"}),
            output_digest=canonical_digest({"status": "accepted"}),
            execution_attempt=1,
            provider_ref="user_protocol",
            model_ref="stable_option_contract",
            status="succeeded",
            acceptance_state="accepted",
            next_transition="compile_authoritative_plan",
        ),
        waiting_request={
            "thread_id": "thread-phase02-resume",
            "turn_id": "turn-phase02-resume",
            "topic_id": "topic-phase02-resume",
            "runtime_descriptors": {"artifact_root": artifact_root},
        },
    )

    resolved = _resume_artifact_root(
        store,
        intent.run_attempt_id,
        Path(artifact_root).resolve(),
    )

    assert resolved == artifact_root
    with pytest.raises(
        ValueError,
        match="phase02_acceptance_artifact_root_mismatch",
    ):
        _resume_artifact_root(
            store,
            intent.run_attempt_id,
            Path("artifacts/phase7/other-run").resolve(),
        )


class _ResumeStore:
    def __init__(
        self,
        *,
        intent: Any,
        ledger: Any,
        transition: DurableTransition,
        waiting_request: dict[str, Any],
    ) -> None:
        self.intent = intent
        self.ledger = ledger
        self.transition = transition
        self.waiting_request = deepcopy(waiting_request)
        self.run_updates: list[dict[str, Any]] = []
        self.audit_events: list[dict[str, Any]] = []
        self.node_records: list[tuple[Any, ...]] = []

    def get_run_state(self, run_id: str) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "thread_id": self.waiting_request["thread_id"],
            "turn_id": self.waiting_request["turn_id"],
            "topic_id": self.waiting_request["topic_id"],
            "status": "waiting_for_clarification",
            "request": deepcopy(self.waiting_request),
        }

    def resolve_active_intent_revision(self, run_id: str) -> Any:
        assert run_id == self.intent.run_attempt_id
        return self.intent

    def load_decision_ledger(self, intent_revision_id: str) -> Any:
        assert intent_revision_id == self.intent.intent_revision_id
        return self.ledger

    def latest_accepted_transition_id(self, run_id: str) -> str:
        assert run_id == self.intent.run_attempt_id
        return self.transition.transition_id

    def upsert_run(self, run_id: str, **record: Any) -> None:
        self.run_updates.append({"run_id": run_id, **deepcopy(record)})

    def add_audit_event(self, event_type: str, **record: Any) -> None:
        self.audit_events.append({"event_type": event_type, **deepcopy(record)})

    def record_run_nodes(self, run_id: str, events: tuple[Any, ...]) -> None:
        self.node_records.append((run_id, *events))


class _StartStore:
    def __init__(
        self,
        *,
        intent: Any,
        ledger: DecisionLedger,
        transition: DurableTransition,
        transition_input: dict[str, Any],
        transition_output: dict[str, Any],
    ) -> None:
        self.intent = intent
        self.ledger = ledger
        self.transition = transition
        self.transition_input = deepcopy(transition_input)
        self.transition_output = deepcopy(transition_output)

    def resolve_active_intent_revision(self, run_id: str) -> Any:
        assert run_id == self.intent.run_attempt_id
        return self.intent

    def load_decision_ledger(self, intent_revision_id: str) -> DecisionLedger:
        assert intent_revision_id == self.intent.intent_revision_id
        return self.ledger

    def latest_accepted_transition_id(self, run_id: str) -> str:
        assert run_id == self.intent.run_attempt_id
        return self.transition.transition_id

    def load_accepted_transition(
        self,
        *,
        run_attempt_id: str,
        node_name: str,
        input_digest: str,
    ) -> dict[str, Any] | None:
        if (
            run_attempt_id != self.intent.run_attempt_id
            or node_name != self.transition.node_name
            or input_digest != self.transition.input_digest
        ):
            return None
        return {
            "transition": self.transition,
            "input_payload": deepcopy(self.transition_input),
            "output_payload": deepcopy(self.transition_output),
        }
