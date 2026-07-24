from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from bi_agent.conversation import agent_core
from bi_agent.runtime import langgraph_workflow as workflow
from bi_agent.runtime import llm_client as llm_client_module
from bi_agent.runtime.durable_call_journal import InMemoryDurableCallJournal
from bi_agent.runtime.llm_client import LLMOutputError, OpenAICompatibleLLMClient
from bi_agent.runtime.mainland_model_provider import MainlandModelProvider
from tests.support.scripted_llm import ScriptedLLMClient


class _CapturingTierClient(OpenAICompatibleLLMClient):
    def __init__(self):
        super().__init__(
            provider="test-mainland",
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
        task="single_authority_plan_proposal",
        prompt_version="test",
        messages=({"role": "user", "content": "{}"},),
        required_keys=(),
        model_tier="critical",
    )

    assert client.seen_models == ["deepseek-v4-pro"]
    assert result.audit["model"] == "deepseek-v4-pro"
    assert result.audit["model_tier"] == "critical"


def test_shared_client_reads_critical_model_from_environment():
    client = MainlandModelProvider.structured_client_from_env(
        {
            "WAJE_LLM_PROVIDER": "deepseek",
            "WAJE_LLM_MODEL": "deepseek-v4-flash",
            "WAJE_LLM_CRITICAL_MODEL": "deepseek-v4-pro",
            "WAJE_LLM_API_KEY": "test-key",
            "WAJE_LLM_BASE_URL": "https://api.deepseek.com/v1",
        }
    )

    assert client.model == "deepseek-v4-flash"
    assert client.critical_model == "deepseek-v4-pro"


def test_base_structured_client_uses_its_provider_request_boundary():
    client = OpenAICompatibleLLMClient(
        provider="deepseek",
        model="deepseek-v4-flash",
        api_key="test-key",
        base_url="https://api.deepseek.com",
        max_attempts=1,
    )
    provider_result = {
        "response_id": "response-base-client-1",
        "content": "{}",
        "usage": {},
        "finish_reason": "stop",
        "reasoning_content_present": False,
    }

    with patch.object(
        llm_client_module,
        "_request_openai_json_in_subprocess",
        return_value=provider_result,
    ) as request:
        result = client.invoke_json(
            task="base_client_boundary",
            prompt_version="test.v1",
            messages=({"role": "user", "content": "{}"},),
            required_keys=(),
        )

    assert result.output == {}
    assert result.audit["finish_reason"] == "stop"
    request.assert_called_once()
    request_config = request.call_args.args[0]
    assert request_config["base_url"] == "https://api.deepseek.com"
    assert request_config["model"] == "deepseek-v4-flash"


def test_conversation_core_resolves_structured_client_through_mainland_provider():
    structured_client = object()

    with patch.object(
        MainlandModelProvider,
        "structured_client_from_env",
        return_value=structured_client,
    ) as factory:
        connection = object()
        resolved = agent_core._conversation_llm_from_env(
            circuit_connection=connection,
        )

    assert resolved is structured_client
    factory.assert_called_once_with(circuit_connection=connection)


def test_shared_client_advertises_explicit_thinking_mode_support():
    assert OpenAICompatibleLLMClient.supports_thinking_mode is True


class _CapturingProfileClient(OpenAICompatibleLLMClient):
    def __init__(self, *, response_content: str = "{}"):
        super().__init__(
            provider="deepseek",
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
        task="single_authority_plan_proposal",
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
    assert result.audit["finish_reason"] == ""
    assert result.audit["input_bytes"] > 0
    assert result.audit["output_bytes"] == 2
    assert "reasoning_content" not in result.audit


def test_failed_client_audit_keeps_profile_without_reasoning_body():
    client = _CapturingProfileClient(response_content='{"unexpected": true}')

    try:
        client.invoke_json(
            task="single_authority_plan_proposal",
            prompt_version="test",
            messages=({"role": "user", "content": "{}"},),
            required_keys=("issue_tree",),
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
    assert audit["finish_reason"] == ""
    assert audit["output_bytes"] == len('{"unexpected": true}'.encode("utf-8"))
    assert "reasoning_content" not in audit
    assert all(
        "reasoning_content" not in attempt for attempt in audit["attempt_failures"]
    )


def test_shared_client_rejects_json_surrounded_by_provider_prose():
    client = _CapturingProfileClient(
        response_content='分析结果如下：\n{"business_summary":"付费金额上升。"}\n以上。'
    )

    try:
        client.invoke_json(
            task="conversation_orchestrator",
            prompt_version="test",
            messages=({"role": "user", "content": "{}"},),
            required_keys=("business_summary",),
        )
    except LLMOutputError as exc:
        assert str(exc) == "llm_output_not_json"
    else:
        raise AssertionError("expected strict JSON contract failure")


def test_shared_client_preserves_valid_json_business_narrative_verbatim():
    original = (
        "paid_amount vs control 仍有结构性差异；"
        "我会保留这个表达，让 typed validator 判断边界。"
    )
    client = _CapturingProfileClient(
        response_content=json.dumps(
            {"business_summary": original},
            ensure_ascii=False,
        )
    )

    result = client.invoke_json(
        task="conversation_orchestrator",
        prompt_version="test",
        messages=({"role": "user", "content": "{}"},),
        required_keys=("business_summary",),
    )

    assert result.output["business_summary"] == original
    assert result.audit["structured_output"]["business_summary"] == original


def test_deepseek_endpoint_sends_thinking_and_keeps_only_reasoning_presence():
    create_calls: list[dict] = []

    class FakeCompletions:
        def create(self, **request):
            create_calls.append(dict(request))
            return SimpleNamespace(
                id="response-deepseek-1",
                choices=[
                    SimpleNamespace(
                        finish_reason="stop",
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

    assert create_calls[0]["extra_body"] == {"thinking": {"type": "enabled"}}
    assert result["reasoning_content_present"] is True
    assert result["finish_reason"] == "stop"
    assert "reasoning_content" not in result
    assert "hidden provider reasoning" not in json.dumps(result)


def test_other_mainland_endpoint_does_not_receive_deepseek_thinking_field():
    create_calls: list[dict] = []

    class FakeCompletions:
        def create(self, **request):
            create_calls.append(dict(request))
            return SimpleNamespace(
                id="response-mainland-1",
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
                "base_url": "https://model.provider.example.cn/v1",
                "timeout_seconds": None,
                "model": "mainland-test",
                "thinking": "disabled",
            },
            ({"role": "user", "content": "{}"},),
        )

    assert "extra_body" not in create_calls[0]
    assert result["reasoning_content_present"] is False


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


def test_workflow_uses_explicit_single_authority_model_profiles():
    expected_profiles = {
        "single_authority_intent": (None, "enabled"),
        "single_authority_clarification": (None, "enabled"),
        "single_authority_plan_proposal": ("critical", "disabled"),
    }
    client = _TierAwareWorkflowClient(
        {
            "single_authority_intent": {
                "intent_binding": {},
                "business_summary": "已识别本次业务目标。",
                "status_message": "准备确认业务边界。",
            },
            "single_authority_clarification": {
                "question": "需要按哪个基线比较？",
                "options": [],
                "recommendation_reason": "该基线与目标窗口直接相邻。",
                "status_message": "等待确认比较基线。",
            },
            "single_authority_decision_binding": {
                "binding_kind": "slot_value",
                "slot_id": "comparison_baseline",
                "value_ref": "previous_day",
                "target_refs": [],
                "affected_binding_fields": [],
                "replacement_user_text": "",
                "status_message": "已绑定比较基线。",
            },
            "single_authority_plan_proposal": {
                "issue_tree": [],
                "auxiliary_axes": [],
                "hypotheses": [],
                "priority_proposals": [],
                "assumption_proposals": [],
            },
        }
    )
    state = {
        "llm_client": client,
        "llm_calls": [],
        "run_id": "run-model-profile",
        "intent_revision": {"intent_revision_id": "intent-model-profile"},
        "request": {
            "authority_store": SimpleNamespace(
                attempt_journal=InMemoryDurableCallJournal()
            )
        },
    }

    for task in expected_profiles:
        workflow._invoke_llm(state, task, {})

    assert {
        call["task"]: (call.get("model_tier"), call.get("thinking"))
        for call in client.calls
    } == expected_profiles
    assert workflow.LLM_TASK_PROFILES["single_authority_decision_binding"] == (
        "critical",
        "enabled",
    )
    assert workflow.LLM_TASK_PROFILES["single_authority_intent"] == (
        "default",
        "enabled",
    )
    assert workflow.LLM_TASK_PROFILES["single_authority_clarification"] == (
        "default",
        "enabled",
    )
