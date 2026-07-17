from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from bi_agent.capabilities.formula_decompose import formula_decompose
from bi_agent.conversation.agent_core import (
    ConversationAgentCore,
    _build_clarification_source_envelope,
)
from bi_agent.conversation.models import ClarificationOption, ClarificationState
from bi_agent.conversation.store import InMemoryConversationStore
from bi_agent.runtime.langgraph_workflow import WorkflowRunResult
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)


def _paid_amount_change_intent() -> dict:
    return {
        "question_family": "paid_amount_change_explanation",
        "question_families": ["paid_amount_change_explanation"],
        "primary_question_family": "paid_amount_change_explanation",
        "secondary_question_families": [],
        "target_metric": "paid_amount",
        "pattern_family": "custom_baseline",
        "pattern_params": {},
        "scope": "full_sample",
        "time_window": "yesterday",
        "target_claim": "解释付费金额变化及其影响因素。",
        "baseline_candidates": [],
        "sub_intents": [],
        "ambiguous_slots": [],
        "answer_contract": {},
        "context_sources": [],
        "claim_intents": [
            "comparative_change",
            "formula_component_contribution",
        ],
        "requested_dimensions": [],
        "requested_components": [
            "first_paid_users",
            "paid_frequency",
            "avg_order_amount",
            "payment_success_rate",
        ],
        "baseline": {},
        "target": {},
        "question": (
            "昨天付费金额为什么变化？主要是首充人数、付费频次、"
            "单笔付费金额，还是支付成功率等因素导致的？"
        ),
        "requested_nodes": [],
    }


def _material_slots(intent: dict) -> dict:
    return {
        "target_metrics": [intent["target_metric"]],
        "requested_components": list(intent["requested_components"]),
        "requested_dimensions": [],
        "baselines": [],
        "context_sources": [],
        "claim_intents": list(intent["claim_intents"]),
        "diagnostic_tags": [],
        "scope": intent["scope"],
    }


def test_baseline_boundary_builds_typed_actions_and_persists_unresolved_axis(
    monkeypatch,
):
    from bi_agent.runtime import langgraph_workflow as workflow

    provider_output = {
        "questions": [
            {
                "question": "请问您希望将昨天与哪个时期进行比较？",
                "options": [
                    "跟前一天（昨日变化）",
                    "跟上周同日（周同比）",
                    "自定义基准",
                    "tell the agent to do differently",
                ],
            }
        ],
        "recommended_assumption": {"option": "跟前一天（昨日变化）"},
        "status_message": "基线未指定，需要用户选择比较基准。",
    }
    monkeypatch.setattr(
        workflow,
        "_invoke_llm",
        lambda state, node, payload, **kwargs: deepcopy(provider_output),
    )
    state = {
        "request": {},
        "intent": _paid_amount_change_intent(),
        "boundary_decision": {
            "boundary_status": "needs_question",
            "clarification_questions": provider_output["questions"],
            "recommended_assumption": provider_output[
                "recommended_assumption"
            ],
            "decision_summary": "缺少比较基准。",
        },
        "clarification_outcome": {"choice": {}},
        "next_action": {"next_action": "ask_question"},
    }

    workflow._generate_clarification(state)

    actions = state["clarification_outcome"]["choice_actions"]
    business_actions = [
        action
        for action in actions
        if action["action_kind"] == "bind_material_choice"
    ]
    assert [
        action["material_patch"] for action in business_actions
    ] == [
        {"baseline_candidates": ["previous_day"]},
        {"baseline_candidates": ["rolling_7_day_baseline"]},
        {"baseline_candidates": ["same_weekday_last_week"]},
    ]
    assert len({action["choice_id"] for action in actions}) == len(actions)
    assert state["clarification_outcome"]["recommended_choice_id"] == (
        business_actions[0]["choice_id"]
    )
    assert business_actions[0]["business_label"].endswith("（推荐）")
    assert state["clarification_outcome"]["questions"][0]["options"][
        0
    ].endswith("（推荐）")
    assert workflow._ambiguous_slot_names(state["intent"]) == {"baseline"}


def test_missing_comparison_baseline_overrides_provider_clear_boundary(
    monkeypatch,
):
    from bi_agent.runtime import langgraph_workflow as workflow

    provider_output = {
        "boundary_status": "clear",
        "clarification_questions": [],
        "recommended_assumption": {},
        "decision_summary": "问题已经足够清楚。",
    }
    monkeypatch.setattr(
        workflow,
        "_invoke_llm",
        lambda state, node, payload, **kwargs: deepcopy(provider_output),
    )
    intent = _paid_amount_change_intent()
    intent["baseline_candidates"] = ["previous_day"]
    intent["baseline_binding"] = {
        "confirmed": False,
        "source": "provider_suggestion",
        "candidates": ["previous_day"],
    }
    state = {
        "request": {},
        "intent": intent,
    }

    workflow._decide_question_boundary(state)

    decision = state["boundary_decision"]
    assert decision["boundary_status"] == "needs_question"
    assert decision["recommended_assumption"]["option"].endswith(
        "（推荐）"
    )
    assert decision["clarification_questions"][0]["options"][0].endswith(
        "（推荐）"
    )
    assert workflow._intent_material_slots(intent)["baselines"] == []


def test_material_baseline_override_replaces_rejected_provider_summary():
    from bi_agent.runtime import langgraph_workflow as workflow

    state = {
        "request": {},
        "intent": {
            **_paid_amount_change_intent(),
            "baseline_binding": {
                "confirmed": False,
                "source": "unbound",
                "candidates": [],
            },
        },
    }
    rejected = {
        "boundary_status": "low_risk_assumption",
        "clarification_questions": [],
        "recommended_assumption": {"option": "使用全样本作为基线"},
        "decision_summary": "基线未指定，默认使用全样本。",
        "display_summary": "基线默认采用全样本。",
    }

    decision = workflow._enforce_material_clarification_boundary(
        state,
        rejected,
    )

    assert decision["boundary_status"] == "needs_question"
    assert decision["display_summary"] == (
        "比较基准尚未确定，需要用户选择后再验证变化方向。"
    )
    assert "全样本" not in decision["display_summary"]


def test_unconfirmed_provider_baseline_cannot_enter_analysis_route_material():
    from bi_agent.runtime import langgraph_workflow as workflow

    intent = _paid_amount_change_intent()
    intent["baseline_candidates"] = ["previous_day"]
    intent["baseline_binding"] = {
        "confirmed": False,
        "source": "provider_suggestion",
        "candidates": ["previous_day"],
    }
    route = {
        "analysis_requirements": {
            **_material_slots(intent),
            "baselines": ["previous_day"],
        }
    }

    merged, conflicts = workflow._merge_confirmed_material_requirements(
        route,
        {"intent": intent, "request": {}},
    )

    assert merged["analysis_requirements"]["baselines"] == []
    assert "baselines" in conflicts


def test_clarification_retry_recompiles_derived_claim_roles_from_current_intent():
    from bi_agent.runtime import langgraph_workflow as workflow

    intent = {
        "target_metric": "paid_amount",
        "baseline_candidates": ["previous_day"],
        "baseline_binding": {
            "confirmed": True,
            "source": "user_clarification",
            "candidates": ["previous_day"],
        },
        "scope": "full_sample",
        "publishable_claim_types": [
            "comparative_change",
            "formula_component_contribution",
        ],
        "required_outcomes": ["change_direction", "factor_contribution"],
        "analysis_axis_ids": ["change_validation", "formula_tree"],
    }
    stale_source_material = {
        "target_metrics": ["paid_amount"],
        "component_ids": [],
        "association_metric_ids": [],
        "dimension_ids": [],
        "baselines": ["previous_day"],
        "context_sources": [],
        "claim_types": [
            "comparative_change",
            "formula_component_contribution",
            "contract_coverage_and_trust_boundary",
        ],
        "required_outcomes": ["change_direction", "factor_contribution"],
        "analysis_axis_ids": [
            "change_validation",
            "formula_tree",
            "data_quality",
        ],
        "scope": "full_sample",
    }

    merged, conflicts = workflow._merge_confirmed_material_requirements(
        {"analysis_requirements": {}},
        {
            "intent": intent,
            "request": {
                "clarification_attempt_context": {
                    "material_slots": stale_source_material,
                }
            },
        },
    )

    requirements = merged["analysis_requirements"]
    assert requirements["baselines"] == ["previous_day"]
    assert requirements["claim_types"] == [
        "comparative_change",
        "formula_component_contribution",
    ]
    assert requirements["analysis_axis_ids"] == [
        "change_validation",
        "formula_tree",
    ]
    assert conflicts == ()


def _waiting_baseline_result(
    request: dict,
    *,
    confirmed: bool,
    intent_baselines: tuple[str, ...],
    material_baselines: tuple[str, ...],
    analysis_contract: dict | None = None,
    analysis_runtime_records: dict | None = None,
    artifact_path: str = "",
) -> WorkflowRunResult:
    intent = _paid_amount_change_intent()
    intent["time_window"] = "2026-06-01"
    intent["target_semantic"] = "2026-06-01"
    intent["ambiguous_slots"] = [] if confirmed else ["baseline"]
    intent["baseline_candidates"] = list(intent_baselines)
    intent["baseline_binding"] = {
        "confirmed": confirmed,
        "source": "user_clarification" if confirmed else "provider_suggestion",
        "candidates": list(intent_baselines),
    }
    material_slots = {
        **_material_slots(intent),
        "baselines": list(material_baselines),
    }
    previous_day_label = "跟前一天比较（推荐）"
    last_week_label = "跟上周同日比较"
    clarification = {
        "questions": [
            {
                "question": "你希望把目标日期的付费金额与哪个基准比较？",
                "options": [
                    previous_day_label,
                    last_week_label,
                    "tell the agent to do differently",
                ],
            }
        ],
        "recommended_assumption": {"option": previous_day_label},
        "recommended_choice_id": "material-baseline-previous-day",
        "choice_actions": [
            {
                "choice_id": "material-baseline-previous-day",
                "action_kind": "bind_material_choice",
                "business_label": previous_day_label,
                "material_patch": {"baseline_candidates": ["previous_day"]},
                "affected_material_slots": ["baseline"],
            },
            {
                "choice_id": "material-baseline-last-week",
                "action_kind": "bind_material_choice",
                "business_label": last_week_label,
                "material_patch": {
                    "baseline_candidates": ["same_weekday_last_week"]
                },
                "affected_material_slots": ["baseline"],
            },
            {
                "choice_id": "material-user-redirect",
                "action_kind": "user_redirect",
                "business_label": "tell the agent to do differently",
            },
        ],
    }
    return WorkflowRunResult(
        status="waiting_for_clarification",
        run_id=request["run_id"],
        answer_package={
            "status": "waiting_for_clarification",
            "accepted_graph": [],
            "analysis_contract": dict(analysis_contract or {}),
            "analysis_route": {
                "requested_nodes": [],
                "analysis_requirements": material_slots,
            },
            "execution_material": None,
            "original_intent": intent,
            "material_slots": material_slots,
            "clarification": clarification,
        },
        artifact_path=artifact_path,
        analysis_runtime_records=analysis_runtime_records,
    )


def test_waiting_run_persists_unconfirmed_baseline_suggestions_without_accepting_them():
    store = InMemoryConversationStore()
    thread_id = "thread-waiting-baseline-suggestions"
    thread = store.create_thread(thread_id)
    topic = store.create_topic(
        thread.thread_id,
        title="付费金额变化",
        summary="确认比较基准",
    )
    store.set_current_topic(thread.thread_id, topic.topic_id)

    def workflow_runner(request):
        return _waiting_baseline_result(
            request,
            confirmed=False,
            intent_baselines=("previous_day", "same_weekday_last_week"),
            material_baselines=(),
        )

    result = ConversationAgentCore(
        store,
        workflow_runner=workflow_runner,
    ).run_message(
        thread_id=thread_id,
        run_id="run-waiting-baseline-suggestions",
        user_message=(
            "2026年6月1日付费金额为什么上涨？主要由哪些指标变化导致？"
        ),
    )

    assert result["status"] == "waiting_for_clarification"
    persisted = store.runs[result["run_id"]]["request"]
    assert persisted["material_authority"]["intent_material"]["baselines"] == []
    assert persisted["material_authority"]["route_material_slots"][
        "baselines"
    ] == []
    assert persisted["original_intent"]["baseline_binding"] == {
        "confirmed": False,
        "source": "provider_suggestion",
        "candidates": ["previous_day", "same_weekday_last_week"],
    }


def test_waiting_run_rejects_confirmed_baseline_outside_accepted_material():
    store = InMemoryConversationStore()

    def workflow_runner(request):
        return _waiting_baseline_result(
            request,
            confirmed=True,
            intent_baselines=("previous_day",),
            material_baselines=("same_weekday_last_week",),
        )

    result = ConversationAgentCore(
        store,
        workflow_runner=workflow_runner,
    ).run_message(
        thread_id="thread-conflicting-confirmed-baseline",
        run_id="run-conflicting-confirmed-baseline",
        user_message="2026年6月1日付费金额相比前一天为什么上涨？",
    )

    assert result["status"] == "failed"
    assert result["failure_reason"] == "material_authority_projection_failed"
    assert result["failure_stage"] == "material_authority_projection"
    assert result["failure_subreason"] == "material_authority_baselines_invalid"
    failure = next(
        event
        for event in store.audit_events
        if event["event_type"] == "material_authority_projection_failed"
    )
    assert failure["payload"]["failure_stage"] == (
        "material_authority_projection"
    )


def test_waiting_run_classifies_missing_runtime_bundle_at_bundle_stage():
    store = InMemoryConversationStore()

    def workflow_runner(request):
        return _waiting_baseline_result(
            request,
            confirmed=True,
            intent_baselines=("previous_day",),
            material_baselines=("previous_day",),
            analysis_contract={"analysis_contract_id": "analysis:waiting"},
        )

    result = ConversationAgentCore(
        store,
        workflow_runner=workflow_runner,
    ).run_message(
        thread_id="thread-missing-waiting-bundle",
        run_id="run-missing-waiting-bundle",
        user_message="2026年6月1日付费金额相比前一天为什么上涨？",
    )

    assert result["status"] == "failed"
    assert result["failure_reason"] == "analysis_runtime_bundle_validation_failed"
    assert result["failure_stage"] == "runtime_bundle_validation"
    assert result["failure_subreason"] == "ValueError"


def test_waiting_run_keeps_real_store_failure_terminal_and_stage_specific():
    from tests.phase7.test_agent_core_bridge import (
        _queryless_runtime_records_for_request,
    )

    class FailingStore(InMemoryConversationStore):
        def save_analysis_runtime_records(self, **_kwargs):
            raise OSError("runtime store unavailable")

    store = FailingStore()

    def workflow_runner(request):
        return _waiting_baseline_result(
            request,
            confirmed=True,
            intent_baselines=("previous_day",),
            material_baselines=("previous_day",),
            analysis_runtime_records=(
                _queryless_runtime_records_for_request(request)
            ),
        )

    result = ConversationAgentCore(
        store,
        workflow_runner=workflow_runner,
    ).run_message(
        thread_id="thread-waiting-store-failure",
        run_id="run-waiting-store-failure",
        user_message="2026年6月1日付费金额相比前一天为什么上涨？",
    )

    assert result["status"] == "failed"
    assert result["failure_reason"] == "analysis_runtime_store_commit_failed"
    assert result["failure_stage"] == "store_commit"
    assert result["failure_subreason"] == "OSError"


def test_waiting_run_classifies_missing_claimed_artifact_at_artifact_stage():
    records = {
        "analysis_contract": {},
        "query_contracts": (),
        "query_execution_records": (),
        "rows_records": (),
        "snapshot_records": (),
        "completeness_records": (),
        "capability_binding_records": (),
        "evidence_manifests": (),
        "context_manifests": (),
        "trusted_provenance_records": ({"claim_id": "claim:waiting"},),
        "verified_claims": (),
        "claim_links": (),
        "repair_attempts": (),
    }
    store = InMemoryConversationStore()

    def workflow_runner(request):
        return _waiting_baseline_result(
            request,
            confirmed=True,
            intent_baselines=("previous_day",),
            material_baselines=("previous_day",),
            analysis_runtime_records=records,
        )

    result = ConversationAgentCore(
        store,
        workflow_runner=workflow_runner,
    ).run_message(
        thread_id="thread-waiting-artifact-failure",
        run_id="run-waiting-artifact-failure",
        user_message="2026年6月1日付费金额相比前一天为什么上涨？",
    )

    assert result["status"] == "failed"
    assert result["failure_reason"] == "analysis_runtime_artifact_sync_failed"
    assert result["failure_stage"] == "artifact_synchronization"
    assert result["failure_subreason"] == "analysis_runtime_artifact_sync_failed"


def test_waiting_clarification_commit_failure_leaves_no_resumable_partial_state():
    class FailingWaitingCommitStore(InMemoryConversationStore):
        def __init__(self):
            super().__init__()
            self.waiting_commit_called = False

        def finalize_waiting_clarification(self, **_kwargs):
            self.waiting_commit_called = True
            raise RuntimeError("waiting clarification commit unavailable")

    store = FailingWaitingCommitStore()
    thread_id = "thread-waiting-atomic-failure"
    thread = store.create_thread(thread_id)
    topic = store.create_topic(
        thread.thread_id,
        title="付费金额变化",
        summary="确认比较基准",
    )
    store.set_current_topic(thread.thread_id, topic.topic_id)

    def workflow_runner(request):
        return _waiting_baseline_result(
            request,
            confirmed=False,
            intent_baselines=("previous_day",),
            material_baselines=(),
        )

    result = ConversationAgentCore(
        store,
        workflow_runner=workflow_runner,
    ).run_message(
        thread_id=thread_id,
        run_id="run-waiting-atomic-failure",
        user_message=(
            "2026年6月1日付费金额为什么上涨？主要由哪些指标变化导致？"
        ),
    )

    assert store.waiting_commit_called is True
    assert result["status"] == "failed"
    assert result["failure_reason"] == "analysis_runtime_store_commit_failed"
    assert result["failure_stage"] == "store_commit"
    assert store.get_thread(thread_id).pending_clarification_id == ""
    assert store.get_open_clarification(thread_id) is None
    assert store.runs[result["run_id"]]["status"] == "failed"


def test_gateway_selected_option_id_projects_typed_baseline_choice():
    store = InMemoryConversationStore()
    thread = store.create_thread("thread-human-q1-baseline")
    topic = store.create_topic(
        thread.thread_id,
        title="付费金额变化",
        summary="解释昨天付费金额变化",
    )
    store.set_current_topic(thread.thread_id, topic.topic_id)
    intent = _paid_amount_change_intent()
    intent["ambiguous_slots"] = ["baseline"]
    selected_action = {
        "choice_id": "material-baseline-previous-day",
        "action_kind": "bind_material_choice",
        "business_label": "跟前一天（昨日变化）",
        "material_patch": {"baseline_candidates": ["previous_day"]},
        "affected_material_slots": ["baseline"],
    }
    clarification = {
        "questions": [
            {
                "question": "请问您希望将昨天与哪个时期进行比较？",
                "options": [
                    "跟前一天（昨日变化）",
                    "跟上周同日（周同比）",
                    "tell the agent to do differently",
                ],
            }
        ],
        "recommended_assumption": {"option": selected_action["business_label"]},
        "recommended_choice_id": selected_action["choice_id"],
        "choice_actions": [
            selected_action,
            {
                "choice_id": "material-baseline-last-week",
                "action_kind": "bind_material_choice",
                "business_label": "跟上周同日（周同比）",
                "material_patch": {
                    "baseline_candidates": ["same_weekday_last_week"]
                },
                "affected_material_slots": ["baseline"],
            },
            {
                "choice_id": "material-user-redirect",
                "action_kind": "user_redirect",
                "business_label": "tell the agent to do differently",
            },
        ],
    }
    source_run_id = "run-human-q1-baseline-source"
    resolution_id = "resolution-human-q1-baseline"
    attempt_run_id = "run-human-q1-baseline-attempt-1"
    store.upsert_run(
        source_run_id,
        thread_id=thread.thread_id,
        topic_id=topic.topic_id,
        status="waiting_for_clarification",
        request={
            "clarification_source_envelope": _build_clarification_source_envelope(
                source_run_id=source_run_id,
                source_thread_id=thread.thread_id,
                source_topic_id=topic.topic_id,
                source_owner_id=thread.owner_id,
                question=intent["question"],
                analysis_context={
                    "as_of": "2026-07-14T12:00:00+01:00",
                    "target_date": "2026-07-13",
                },
                original_intent=intent,
                material_slots=_material_slots(intent),
                clarification=clarification,
            )
        },
    )
    store.set_pending_clarification(
        thread.thread_id,
        topic.topic_id,
        source_run_id,
    )
    store.save_clarification_state(
        ClarificationState(
            run_id=source_run_id,
            topic_id=topic.topic_id,
            question=clarification["questions"][0]["question"],
            options=[
                ClarificationOption(
                    option_id=action["choice_id"],
                    label=action["business_label"],
                    recommended=action["choice_id"]
                    == clarification["recommended_choice_id"],
                )
                for action in clarification["choice_actions"]
            ],
        )
    )
    store.resolve_clarification_attempt_authority = lambda **values: {
        "resolution_id": resolution_id,
        "source_run_id": values["source_run_id"],
        "attempt_run_id": values["attempt_run_id"],
        "previous_attempt_run_id": None,
        "attempt_number": 1,
        "thread_id": values["thread_id"],
        "topic_id": topic.topic_id,
        "owner_id": thread.owner_id,
        "answer": values["answer"],
        "selected_option_id": values["selected_option_id"],
        "source": values["source"],
        "retry_attempt": False,
        "accepted_choice": deepcopy(selected_action),
        "material_patch": deepcopy(selected_action["material_patch"]),
    }
    workflow_requests = []

    def workflow_runner(request):
        workflow_requests.append(deepcopy(request))
        return WorkflowRunResult(
            status="failed",
            run_id=request["run_id"],
            failure_reason="stop_after_choice_projection",
        )

    result = ConversationAgentCore(
        store,
        workflow_runner=workflow_runner,
    ).run_message(
        thread_id=thread.thread_id,
        run_id=attempt_run_id,
        user_message=selected_action["business_label"],
        clarification={
            "sourceRunId": source_run_id,
            "resolutionId": resolution_id,
            "attemptRunId": attempt_run_id,
            "answer": selected_action["business_label"],
            "selectedOptionId": selected_action["choice_id"],
            "source": "user",
            "retryAttempt": False,
        },
    )

    assert result["status"] == "failed"
    assert workflow_requests[0]["clarification_choice"] == {
        "answer_text": selected_action["business_label"],
        "baseline_candidates": ["previous_day"],
    }
    assert workflow_requests[0]["clarification_attempt_context"][
        "selected_material_action"
    ] == selected_action


def test_bound_baseline_cannot_be_reopened_by_a_stale_provider_question():
    from bi_agent.runtime import langgraph_workflow as workflow

    state = {
        "request": {
            "clarification_choice": {
                "answer_text": "跟前一天（昨日变化）",
                "baseline_candidates": ["previous_day"],
            }
        },
        "intent": {
            **_paid_amount_change_intent(),
            "baseline_candidates": ["previous_day"],
            "ambiguous_slots": [],
        },
        "boundary_decision": {
            "boundary_status": "needs_question",
            "recommended_assumption": {"option": "跟上周同日比较"},
            "clarification_questions": [
                {
                    "question": "请确认比较基线。",
                    "options": [
                        "跟前一天比较",
                        "跟上周同日比较",
                        "tell the agent to do differently",
                    ],
                }
            ],
        },
        "clarification_choice_consumed": True,
        "checkpoint_events": [{}],
    }

    workflow._clarification_policy_gate(state)

    assert state["clarification_outcome"]["boundary_status"] == (
        "low_risk_assumption"
    )
    assert state["clarification_outcome"]["choice"] == {}


def test_waiting_artifact_keeps_raw_deepseek_calls_for_human_replay(tmp_path):
    from bi_agent.runtime import langgraph_workflow as workflow

    llm_call = {
        "task": "business_intent",
        "response_id": "response-human-q1",
        "raw_response_content": '{"time_window":"昨天"}',
        "structured_output": {"time_window": "昨天"},
    }
    state = {
        "run_id": "run-human-q1-waiting-artifact",
        "request": {"artifact_root": str(tmp_path)},
        "intent": _paid_amount_change_intent(),
        "clarification_outcome": {
            "status": "question_tool_opened",
            "questions": [{"question": "请选择基线。", "options": []}],
        },
        "llm_calls": [llm_call],
        "checkpoint_events": [
            {"node": "understand_business_intent", "status": "completed"}
        ],
    }

    workflow._persist_clarification(state)

    assert state["answer_package"]["llm_calls"] == [llm_call]
    assert state["answer_package"]["checkpoint_events"] == (
        state["checkpoint_events"]
    )


def test_query_gap_waiting_artifact_keeps_raw_deepseek_calls(tmp_path):
    from bi_agent.runtime import langgraph_workflow as workflow

    llm_call = {
        "task": "query_gap_clarification",
        "response_id": "response-human-q1-query-gap",
        "raw_response_content": '{"questions":[]}',
        "structured_output": {"questions": []},
    }
    state = {
        "run_id": "run-human-q1-query-gap-artifact",
        "request": {"artifact_root": str(tmp_path)},
        "intent": _paid_amount_change_intent(),
        "query_gap_clarification": {
            "questions": [{"question": "数据不完整，如何继续？", "options": []}]
        },
        "compiled_graph": SimpleNamespace(
            mutations=SimpleNamespace(accepted_graph=[])
        ),
        "analysis_route": {},
        "execution_material": None,
        "query_repair_decisions": [],
        "staged_query_gap_actions": [],
        "llm_calls": [llm_call],
        "checkpoint_events": [
            {
                "node": "generate_query_gap_clarification",
                "status": "completed",
            }
        ],
    }

    workflow._persist_query_gap_clarification(state)

    assert state["answer_package"]["llm_calls"] == [llm_call]
    assert state["answer_package"]["checkpoint_events"] == (
        state["checkpoint_events"]
    )


def test_canonical_yesterday_is_written_back_to_exact_run_date(monkeypatch):
    from bi_agent.runtime import langgraph_workflow as workflow

    intent_output = {
        "question_family": "paid_amount_change_explanation",
        "target_metric": "paid_amount",
        "pattern_family": "custom_baseline",
        "pattern_params": {},
        "scope": "full_sample",
        "time_window": "yesterday",
        "target_claim": "解释昨天付费金额变化。",
        "baseline_candidates": ["previous_day"],
        "analysis_requirements": {
            "context_sources": [],
            "claim_intents": [
                "comparative_change",
                "formula_component_contribution",
            ],
            "requested_dimensions": [],
            "requested_components": [
                "first_paid_users",
                "paid_frequency",
                "avg_order_amount",
                "payment_success_rate",
            ],
        },
        "sub_intents": [],
        "ambiguous_slots": [],
        "answer_contract": {},
    }
    monkeypatch.setattr(
        workflow,
        "_invoke_llm",
        lambda state, node, payload, **kwargs: deepcopy(intent_output),
    )
    state = {
        "request": {
            "question": _paid_amount_change_intent()["question"],
            "analysis_context": {
                "as_of": "2026-07-14T12:00:00+01:00",
            },
        }
    }

    workflow._understand_business_intent(state)

    assert state["intent"]["time_window"] == "2026-07-13"
    assert state["intent"]["target_semantic"] == "2026-07-13"
    assert state["intent"]["baseline_binding"] == {
        "confirmed": False,
        "source": "provider_suggestion",
        "candidates": ["previous_day"],
    }
    assert state["request"]["analysis_context"] == {
        "as_of": "2026-07-14T12:00:00+01:00",
        "business_timezone": "Africa/Lagos",
        "target_date": "2026-07-13",
    }


def test_paid_amount_change_always_requires_formula_framework():
    registry = RuntimeContractRegistry.from_path(
        CANONICAL_RUNTIME_BINDINGS_PATH
    )

    obligation = registry.question_family_obligation(
        "paid_amount_change_explanation"
    )

    assert "formula_decompose" in obligation["required_capabilities"]


def test_business_intent_marks_explicit_factors_as_priority_not_formula_closure():
    from bi_agent.runtime import langgraph_workflow as workflow

    payload = workflow._business_intent_payload(
        {"question": _paid_amount_change_intent()["question"]}
    )

    assert payload["requested_component_policy"] == {
        "role": "user_explicit_priority_checks",
        "formula_closure": "local_capability_contract",
        "main_driver_selection": (
            "post_query_contribution_reconciliation_and_verifier"
        ),
    }


def test_formula_component_presence_alone_cannot_publish_quantified_decomposition():
    result = formula_decompose(
        [
            {
                "formula_id": "frequency_ticket_size",
                "components": (
                    "paid_users",
                    "paid_frequency",
                    "avg_order_amount",
                ),
            }
        ],
        available_components=(
            "paid_users",
            "paid_frequency",
            "avg_order_amount",
        ),
    )

    assert result.wording_limit != "quantified"
    assert "formula_reconciliation_missing:frequency_ticket_size" in (
        result.limitations
    )


def test_fixed_window_coverage_failure_is_terminal_data_unavailable():
    from bi_agent.runtime.query_repair import plan_query_repair

    contract = SimpleNamespace(
        contract_signature="signature-fixed-window",
        query_contract_id="query-fixed-window",
    )
    report = SimpleNamespace(
        report_ref="completeness:fixed-window",
        failure_reasons=(
            "snapshot_stale:2026-07-04<2026-07-13",
            "missing_required_window:target_day",
            "missing_required_window:previous_day",
            "incomplete_window:target_day:0/1",
            "incomplete_window:previous_day:0/1",
        ),
    )

    decision = plan_query_repair(contract, report, attempted_signatures=())

    assert decision.action == "block"
    assert decision.reason == "window_coverage_failure"
    assert decision.requires_llm is False
    assert decision.requires_clarification is False
    assert decision.report_ref == report.report_ref


def test_terminal_window_unavailability_cannot_be_reopened_by_other_typed_gaps():
    from bi_agent.runtime import langgraph_workflow as workflow

    result = SimpleNamespace(
        status="clarify",
        typed_gaps=(
            {
                "gap_type": "contract_partial",
                "requires_clarification": True,
                "affected_capabilities": ["formula_decompose"],
            },
        ),
        bound_capability_inputs={},
    )
    state = {
        "request": {},
        "analysis_runtime_result": result,
        "query_repair_decisions": (
            {
                "action": "block",
                "reason": "window_coverage_failure",
                "requires_clarification": False,
                "report_ref": "completeness:target-baseline",
                "failure_reasons": [
                    "missing_required_window:target_day",
                    "missing_required_window:previous_day",
                    "incomplete_window:target_day:0/1",
                    "incomplete_window:previous_day:0/1",
                ],
            },
        ),
    }

    assert workflow._route_after_query_repair(state) == "block"


def test_terminal_data_unavailable_evidence_keeps_dates_and_runtime_refs():
    from bi_agent.runtime import langgraph_workflow as workflow

    state = {
        "run_id": "run-terminal-window-evidence",
        "intent": {
            **_paid_amount_change_intent(),
            "time_window": "2026-07-13",
        },
        "request": {
            "analysis_contract": {
                "resolved_windows": [
                    {
                        "window_id": "target_day",
                        "role": "target",
                        "label": "2026-07-13",
                        "start_inclusive": "2026-07-13",
                        "end_exclusive": "2026-07-14",
                    },
                    {
                        "window_id": "previous_day",
                        "role": "baseline",
                        "label": "2026-07-12",
                        "start_inclusive": "2026-07-12",
                        "end_exclusive": "2026-07-13",
                    },
                ]
            },
            "query_contracts": [
                {"query_contract_id": "query:paid-amount"}
            ],
            "query_results": [
                {
                    "query_contract_ref": "query:paid-amount",
                    "result_ref": "result:paid-amount",
                }
            ],
            "completeness_reports": [
                {
                    "report_ref": "completeness:paid-amount",
                    "result_ref": "result:paid-amount",
                    "coverage_summary": {
                        "target_day": {"observed": 0, "required": 1},
                        "previous_day": {"observed": 0, "required": 1},
                    },
                    "failure_reasons": [
                        "incomplete_window:target_day:0/1",
                        "incomplete_window:previous_day:0/1",
                    ],
                }
            ],
        },
        "query_repair_decisions": [
            {
                "action": "block",
                "reason": "window_coverage_failure",
                "report_ref": "completeness:paid-amount",
                "failure_reasons": [
                    "snapshot_stale:2026-07-04<2026-07-13",
                    "incomplete_window:target_day:0/1",
                    "incomplete_window:previous_day:0/1",
                ],
            }
        ],
        "analysis_runtime_result": SimpleNamespace(
            typed_gaps=(
                {
                    "gap_type": "window_data_unavailable",
                    "owner": "data_owner",
                    "diagnostic_context": {
                        "target_date": "2026-07-13",
                        "latest_complete_business_date": "2026-07-04",
                        "terminal_for_current_window": True,
                    },
                },
            )
        ),
    }

    evidence = workflow._blocked_data_availability_evidence(state)

    payload = evidence["typed_payload"]
    assert payload["target_date"] == "2026-07-13"
    assert payload["baseline_dates"] == ["2026-07-12"]
    assert payload["latest_complete_business_dates"] == ["2026-07-04"]
    assert payload["query_contract_refs"] == ["query:paid-amount"]
    assert payload["result_refs"] == ["result:paid-amount"]
    assert payload["completeness_refs"] == ["completeness:paid-amount"]
    assert evidence["result_refs"] == ["result:paid-amount"]


def test_query_gap_recommendation_has_one_visible_marker_and_stable_choice_id():
    from bi_agent.runtime import langgraph_workflow as workflow

    preferred = "保留当前范围并发布受限结论"
    deferred = "等待缺失数据后继续"
    business_gaps = [
        {
            "allowed_actions": [
                {
                    "choice_id": "continue-with-boundary",
                    "action_kind": "omit_unavailable_context",
                    "business_semantics": preferred,
                    "affected_capabilities": ["event_evidence"],
                },
                {
                    "choice_id": "wait-for-data",
                    "action_kind": "wait_for_source",
                    "business_semantics": deferred,
                    "affected_capabilities": ["event_evidence"],
                },
            ]
        }
    ]
    output = {
        "questions": [
            {
                "question": "缺失分支如何处理？",
                "options": [
                    preferred,
                    deferred,
                    "tell the agent to do differently",
                ],
            }
        ],
        "recommended_assumption": {"option": preferred},
        "recommendation_reason": "主指标证据完整，可省略缺失背景分支。",
    }

    options, actions = workflow._render_query_gap_actions(
        {},
        business_gaps,
        output=output,
    )

    business_options = options[:-1]
    assert sum(option.endswith("（推荐）") for option in business_options) == 1
    recommended_action = next(
        action
        for action in actions
        if action.get("choice_id") == output["recommended_choice_id"]
    )
    assert recommended_action["business_semantics"] == preferred
    assert recommended_action["business_label"].endswith("（推荐）")
    assert recommended_action["business_semantics"] == preferred
    assert output["recommended_assumption"]["option"] == (
        recommended_action["business_label"]
    )
    first_ids = {
        action.get("business_semantics"): action.get("choice_id")
        for action in actions
        if action.get("business_semantics")
    }
    second_output = {
        "questions": deepcopy(output["questions"]),
        "recommended_assumption": {"option": deferred},
        "recommendation_reason": "等待数据可避免当前分支降级。",
    }
    _, second_actions = workflow._render_query_gap_actions(
        {},
        business_gaps,
        output=second_output,
    )
    assert {
        action.get("business_semantics"): action.get("choice_id")
        for action in second_actions
        if action.get("business_semantics")
    } == first_ids
    assert next(
        action
        for action in second_actions
        if action.get("choice_id") == second_output["recommended_choice_id"]
    )["business_semantics"] == deferred


def test_paid_amount_formula_candidates_come_from_ssot_contract_and_stay_candidates():
    from bi_agent.runtime.formula_candidates import (
        build_formula_candidate_framework,
    )

    contract_path = (
        Path(__file__).resolve().parents[2]
        / "contracts"
        / "metrics"
        / "paid-amount.metric.yaml"
    )
    framework = build_formula_candidate_framework(
        metric_contract_path=contract_path,
        available_runtime_metrics=(
            "paid_amount",
            "paid_users",
            "paid_orders",
            "first_paid_users",
            "paid_frequency",
            "avg_order_amount",
        ),
        available_dimensions=(),
        requested_components=(
            "first_paid_users",
            "paid_frequency",
            "avg_order_amount",
            "payment_success_rate",
        ),
    )

    by_path = {
        candidate["path_id"]: candidate
        for candidate in framework["candidates"]
    }
    assert {
        "paid_dau_arpu",
        "paid_user_arppu",
        "new_user_funnel_dashboard",
        "frequency_ticket_size",
        "region_sum",
        "device_sum",
        "paid_dau_first_pay_retention",
        "avg_order_amount_identity",
        "payment_success_chain",
        "previous_paid_dau_retention",
        "paid_user_conversion_identity",
        "paid_frequency_identity",
        "paid_amount_gameplay_sum",
        "gameplay_paid_amount_arppu",
        "gameplay_frequency_ticket_size",
        "gameplay_active_exposure_click",
        "gameplay_icon_position_mechanism",
        "ggr_gameplay_sum",
        "gameplay_turnover_components",
        "hour_sum",
        "recharge_tier_sum",
    }.issubset(by_path)
    assert framework["selection_state"] == "candidate_only"
    assert framework["primary_formula"] is None
    assert sum(
        candidate["candidate_role"] == "primary_candidate"
        for candidate in framework["candidates"]
    ) == 1
    frequency = by_path["frequency_ticket_size"]
    assert frequency["candidate_role"] == "primary_candidate"
    assert frequency["candidate_status"] == "executable"
    assert frequency["runtime_components"] == [
        "paid_users",
        "paid_frequency",
        "avg_order_amount",
    ]
    assert frequency["matched_requested_components"] == [
        "paid_frequency",
        "avg_order_amount",
    ]
    assert "first_paid_users" in by_path[
        "paid_dau_first_pay_retention"
    ]["matched_requested_components"]
    assert "payment_success_rate" in by_path[
        "payment_success_chain"
    ]["matched_requested_components"]
    assert by_path["payment_success_chain"]["candidate_status"] != (
        "executable"
    )
