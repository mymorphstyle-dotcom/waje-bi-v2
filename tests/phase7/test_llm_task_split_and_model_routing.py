from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from bi_agent.runtime import langgraph_workflow as workflow
from bi_agent.runtime import llm_client as llm_client_module
from bi_agent.runtime.llm_client import LLMOutputError, OpenAICompatibleLLMClient
from bi_agent.runtime.llm_prompts import build_prompt
from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry
from tests.support.scripted_llm import ScriptedLLMClient


RUNTIME_BINDINGS = "contracts/runtime/clickhouse-analysis-bindings.yaml"


def _route_requirements() -> dict:
    return {
        "target_metrics": ["paid_amount"],
        "requested_components": ["paid_users", "avg_order_amount"],
        "requested_dimensions": [],
        "baselines": ["previous_day"],
        "context_sources": [],
        "dataset_requirements": ["paid_order_success"],
        "diagnostic_tags": ["change_explanation"],
        "claim_intents": [
            "comparative_change",
            "formula_component_contribution",
        ],
        "scope": "full_sample",
    }


def _route_state() -> dict:
    return {
        "request": {
            "run_mode": "production",
            "question": "2026年6月1日付费金额为什么上涨？",
        },
        "intent": {
            "question_family": "paid_amount_change_explanation",
            "question_families": ["paid_amount_change_explanation"],
            "target_metric": "paid_amount",
            "time_window": "2026-06-01",
            "target_semantic": "2026-06-01",
            "baseline_candidates": ["previous_day"],
            "baseline_binding": {
                "candidates": ["previous_day"],
                "confirmed": True,
                "source": "user_clarification",
            },
            "scope": "full_sample",
        },
        "confirmed_understanding": {
            "confirmed_intent": {
                "business_summary": "分析目标日付费金额相对前一天的变化原因。"
            }
        },
        "llm_calls": [],
    }


def test_route_plan_and_route_narrative_have_disjoint_prompt_contracts():
    plan = build_prompt(
        "analysis_route_plan",
        {
            "intent": _route_state()["intent"],
            "known_capabilities": [],
        },
    )
    narrative = build_prompt(
        "final_route_narrative",
        {
            "route_context": {
                "target": "2026年6月1日",
                "baseline": "前一天",
                "direction_status": "待验证",
                "route_steps": [],
            }
        },
    )

    assert set(plan.required_keys) == {
        "requested_nodes",
        "analysis_requirements",
    }
    assert set(narrative.required_keys) == {
        "route_summary",
        "sections",
        "decision_summary",
        "display_summary",
    }
    plan_text = "\n".join(message["content"] for message in plan.messages)
    narrative_text = "\n".join(
        message["content"] for message in narrative.messages
    )
    assert "capability_sections" not in plan.required_keys
    assert "requested_nodes" not in narrative.required_keys
    assert "expected_evidence must be an object" not in narrative_text
    assert "expected_evidence" in narrative_text
    assert "final_route_machine" not in narrative_text
    assert "budget_state" not in narrative_text
    assert "Propose the concrete business analysis route" in plan_text


def test_final_route_narrative_input_contains_only_business_projection():
    state = _route_state()
    payload, step_bindings = workflow._build_final_route_narrative_payload(
        state,
        requested=("compare_periods", "formula_decompose"),
    )

    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    for forbidden in (
        "compare_periods",
        "formula_decompose",
        "paid_amount",
        "paid_order_success",
        "change_explanation",
        "previous_day",
        "budget_state",
        "claim_intents",
    ):
        assert forbidden not in encoded
    assert "周期对比" in encoded
    assert "公式拆解" in encoded
    assert "待数据验证" in encoded
    assert step_bindings == {
        "step_1": "compare_periods",
        "step_2": "formula_decompose",
    }


def test_final_route_narrative_direction_status_preserves_question_semantics():
    state = _route_state()
    state["intent"]["target_claim"] = "解释目标日付费金额上涨的原因"
    increase_payload, _ = workflow._build_final_route_narrative_payload(
        state,
        requested=("compare_periods",),
    )

    state["intent"]["target_claim"] = "比较目标日与前一天的付费金额变化"
    state["request"]["question"] = (
        "2026年6月1日付费金额相较前一天发生了什么变化？"
    )
    unknown_payload, _ = workflow._build_final_route_narrative_payload(
        state,
        requested=("compare_periods",),
    )

    assert increase_payload["route_context"]["direction_status"] == (
        "用户提出的上涨仍待数据验证"
    )
    assert unknown_payload["route_context"]["direction_status"] == (
        "变化方向待数据验证"
    )


def test_final_route_narrative_failure_keeps_machine_route_available():
    state = _route_state()
    route = {
        "analysis_requirements": _route_requirements(),
        "obligation_resolution": {"status": "accepted"},
    }
    registry = RuntimeContractRegistry.from_path(RUNTIME_BINDINGS)

    with patch(
        "bi_agent.runtime.langgraph_workflow._invoke_llm",
        side_effect=workflow.WorkflowFailure(
            "llm_narrative_invalid:route_summary",
            failure_type="llm",
        ),
    ):
        finalized = workflow._finalize_production_analysis_route_narrative(
            state,
            route=route,
            requested=("compare_periods", "formula_decompose"),
            registry=registry,
        )

    assert finalized["analysis_requirements"] == route["analysis_requirements"]
    assert finalized["obligation_resolution"] == route["obligation_resolution"]
    assert finalized["route_narrative_status"] == "unavailable"
    assert finalized["route_narrative_failure"] == (
        "llm_narrative_invalid:route_summary"
    )
    assert "route_summary" not in finalized
    assert "capability_sections" not in finalized


class _CapturingTierClient(OpenAICompatibleLLMClient):
    def __init__(self):
        super().__init__(
            provider="openai_compatible",
            model="deepseek-v4-flash",
            critical_model="deepseek-v4-pro",
            api_key="test-key",
            base_url="https://example.invalid",
            max_attempts=1,
        )
        self.seen_models: list[str] = []

    def _request_json_once(self, messages, *, attempt=1, model=None, thinking=None):
        self.seen_models.append(str(model or ""))
        return {"response_id": "response-1", "content": "{}", "usage": {}}


def test_shared_client_uses_critical_model_for_critical_tier():
    client = _CapturingTierClient()

    result = client.invoke_json(
        task="analysis_route_plan",
        prompt_version="test",
        messages=({"role": "user", "content": "{}"},),
        required_keys=(),
        model_tier="critical",
    )

    assert client.seen_models == ["deepseek-v4-pro"]
    assert result.audit["model"] == "deepseek-v4-pro"
    assert result.audit["model_tier"] == "critical"


def test_shared_client_reads_critical_model_from_environment():
    client = OpenAICompatibleLLMClient.from_env(
        {
            "WAJE_LLM_PROVIDER": "openai_compatible",
            "WAJE_LLM_MODEL": "deepseek-v4-flash",
            "WAJE_LLM_CRITICAL_MODEL": "deepseek-v4-pro",
            "WAJE_LLM_API_KEY": "test-key",
        }
    )

    assert client.model == "deepseek-v4-flash"
    assert client.critical_model == "deepseek-v4-pro"


def test_shared_client_advertises_explicit_thinking_mode_support():
    assert OpenAICompatibleLLMClient.supports_thinking_mode is True


class _CapturingProfileClient(OpenAICompatibleLLMClient):
    def __init__(self, *, response_content: str = "{}"):
        super().__init__(
            provider="openai_compatible",
            model="deepseek-v4-flash",
            critical_model="deepseek-v4-pro",
            api_key="test-key",
            base_url="https://api.deepseek.com",
            max_attempts=1,
        )
        self.response_content = response_content
        self.seen_profiles: list[dict[str, str | None]] = []

    def _request_json_once(self, messages, *, attempt=1, model=None, thinking=None):
        self.seen_profiles.append(
            {
                "model": model,
                "thinking": thinking,
            }
        )
        return {
            "response_id": "response-profile-1",
            "content": self.response_content,
            "usage": {},
            "reasoning_content_present": True,
        }


def test_shared_client_forwards_explicit_thinking_and_audits_actual_profile():
    client = _CapturingProfileClient()

    result = client.invoke_json(
        task="analysis_route_plan",
        prompt_version="test",
        messages=({"role": "user", "content": "{}"},),
        required_keys=(),
        model_tier="critical",
        thinking="disabled",
    )

    assert client.seen_profiles == [
        {
            "model": "deepseek-v4-pro",
            "thinking": "disabled",
        }
    ]
    assert result.audit["model"] == "deepseek-v4-pro"
    assert result.audit["model_tier"] == "critical"
    assert result.audit["thinking"] == "disabled"
    assert result.audit["reasoning_content_present"] is True
    assert "reasoning_content" not in result.audit


def test_failed_client_audit_keeps_profile_without_reasoning_body():
    client = _CapturingProfileClient(response_content='{"unexpected": true}')

    try:
        client.invoke_json(
            task="analysis_route_plan",
            prompt_version="test",
            messages=({"role": "user", "content": "{}"},),
            required_keys=("requested_nodes",),
            model_tier="critical",
            thinking="enabled",
        )
    except LLMOutputError as exc:
        audit = exc.audit
    else:
        raise AssertionError("expected missing output key failure")

    assert audit["model"] == "deepseek-v4-pro"
    assert audit["model_tier"] == "critical"
    assert audit["thinking"] == "enabled"
    assert audit["reasoning_content_present"] is True
    assert "reasoning_content" not in audit
    assert all(
        "reasoning_content" not in attempt
        for attempt in audit["attempt_failures"]
    )


def test_deepseek_endpoint_sends_thinking_and_keeps_only_reasoning_presence():
    create_calls: list[dict] = []

    class FakeCompletions:
        def create(self, **request):
            create_calls.append(dict(request))
            return SimpleNamespace(
                id="response-deepseek-1",
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="{}",
                            reasoning_content="hidden provider reasoning",
                        )
                    )
                ],
                usage=None,
            )

    class FakeOpenAI:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    with patch.object(llm_client_module, "OpenAI", FakeOpenAI):
        result = llm_client_module._request_openai_json_once(
            {
                "api_key": "test-key",
                "base_url": "https://api.deepseek.com",
                "timeout_seconds": None,
                "model": "deepseek-v4-pro",
                "thinking": "enabled",
            },
            ({"role": "user", "content": "{}"},),
        )

    assert create_calls[0]["extra_body"] == {
        "thinking": {"type": "enabled"}
    }
    assert result["reasoning_content_present"] is True
    assert "reasoning_content" not in result
    assert "hidden provider reasoning" not in json.dumps(result)


def test_non_deepseek_endpoint_does_not_receive_deepseek_thinking_field():
    create_calls: list[dict] = []

    class FakeCompletions:
        def create(self, **request):
            create_calls.append(dict(request))
            return SimpleNamespace(
                id="response-openai-1",
                choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))],
                usage=None,
            )

    class FakeOpenAI:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    with patch.object(llm_client_module, "OpenAI", FakeOpenAI):
        result = llm_client_module._request_openai_json_once(
            {
                "api_key": "test-key",
                "base_url": "https://api.openai.com/v1",
                "timeout_seconds": None,
                "model": "gpt-test",
                "thinking": "disabled",
            },
            ({"role": "user", "content": "{}"},),
        )

    assert "extra_body" not in create_calls[0]
    assert result["reasoning_content_present"] is False


def test_causal_audit_narrative_rejects_machine_enum_leakage():
    try:
        llm_client_module._localize_narrative_fields(
            {
                "causal_assessment": "not_supported",
                "publishable_wording": "会计贡献结论可以保留。",
                "display_summary": "深层机制状态为not_supported。",
            }
        )
    except LLMOutputError as exc:
        assert str(exc) == "llm_narrative_invalid:display_summary"
    else:
        raise AssertionError("expected causal narrative enum leakage failure")


class _TierAwareWorkflowClient:
    supports_model_tier = True
    supports_thinking_mode = True

    def __init__(self, responses):
        self.calls: list[dict] = []
        self.delegate = ScriptedLLMClient(responses)

    def invoke_json(self, **kwargs):
        self.calls.append(dict(kwargs))
        return self.delegate.invoke_json(
            task=kwargs["task"],
            prompt_version=kwargs["prompt_version"],
            messages=kwargs["messages"],
            required_keys=kwargs["required_keys"],
        )


def test_workflow_uses_narrow_explicit_model_profiles_by_node_responsibility():
    expected_profiles = {
        "business_intent": ("default", "enabled"),
        "semantic_audit": ("default", "enabled"),
        "analysis_route_plan": ("critical", "disabled"),
        "route_repair": ("critical", "disabled"),
        "next_action": ("critical", "disabled"),
        "promotion_direction": ("critical", "disabled"),
        "evidence_interpretation": ("default", "disabled"),
        "answer_synthesis": ("default", "disabled"),
        "boundary_decision": ("default", "disabled"),
        "data_coverage_interpretation": ("default", "disabled"),
        "causal_audit": ("default", "disabled"),
        "answer_repair": ("default", "disabled"),
        "final_answer_audit": ("default", "disabled"),
        "final_business_summary": ("default", "disabled"),
        "final_route_narrative": ("default", "disabled"),
    }
    client = _TierAwareWorkflowClient(
        {
            "business_intent": {
                "question_family": "paid_amount_change_explanation",
                "target_metric": "paid_amount",
                "pattern_family": "period_comparison",
                "pattern_params": {},
                "scope": "full_sample",
                "time_window": "2026-06-01",
                "target_claim": "comparative_change",
                "baseline_candidates": ["previous_day"],
                "analysis_requirements": {},
                "status_message": "已识别业务意图。",
                "display_summary": "准备核对目标日相对前一天的变化。",
            },
            "semantic_audit": {"audit_status": "passed", "issues": []},
            "analysis_route_plan": {
                "requested_nodes": ["compare_periods"],
                "analysis_requirements": _route_requirements(),
            },
            "route_repair": {
                "requested_nodes": ["compare_periods"],
                "repair_summary": "保留可执行的周期对比。",
                "decision_summary": "当前路线可以执行。",
                "display_summary": "分析路线已确认。",
            },
            "next_action": {
                "next_action": "synthesize_answer",
                "decision_summary": "证据已足够形成回答。",
                "display_summary": "准备形成业务回答。",
            },
            "promotion_direction": {
                "requested_nodes": [],
                "decision_summary": "当前无需扩展分析路线。",
                "display_summary": "保留当前分析范围。",
            },
            "evidence_interpretation": {
                "interpretation": "目标日与基线的变化已经得到数据支持。",
                "decision_summary": "可以发布方向性结论。",
                "evidence_boundary": "当前证据只支持已查询窗口。",
            },
            "answer_synthesis": {
                "answer_text": "目标日相对前一天的变化已经得到验证。",
                "display_summary": "已形成业务回答草稿。",
            },
            "boundary_decision": {
                "boundary_status": "clear",
                "recommended_assumption": {},
                "clarification_questions": [],
                "decision_summary": "当前问题边界明确。",
                "display_summary": "可以继续分析。",
            },
            "data_coverage_interpretation": {
                "coverage_status": "sufficient",
                "business_impact": "当前数据覆盖目标窗口与对比窗口。",
                "decision_summary": "数据足以支持周期对比。",
                "display_summary": "数据覆盖满足本轮分析。",
            },
            "causal_audit": {
                "causal_assessment": "not_supported",
                "publishable_wording": "当前只发布可验证的会计分解。",
                "supporting_reasons": ["缺少独立机制证据。"],
                "evidence_limit": "不发布深层因果机制。",
                "display_summary": "结论保留因果边界。",
            },
            "answer_repair": {
                "answer_text": "已按证据边界修正业务回答。",
                "display_summary": "业务回答已经修正。",
            },
            "final_answer_audit": {"material_findings": []},
            "final_business_summary": {
                "summary_text": "目标日相对前一天的变化已经得到验证。",
                "statement_bindings": [],
                "display_summary": "已形成最终业务回答。",
            },
            "final_route_narrative": {
                "route_summary": "先验证变化，再检查影响因素。",
                "sections": [],
                "decision_summary": "该路线覆盖当前业务问题。",
                "display_summary": "分析路线已经确认。",
            },
        }
    )
    state = {"llm_client": client, "llm_calls": []}

    for task in expected_profiles:
        workflow._invoke_llm(state, task, {})

    assert {
        call["task"]: (call.get("model_tier"), call.get("thinking"))
        for call in client.calls
    } == expected_profiles
