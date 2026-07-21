import pytest

from tests.support.scripted_llm import ScriptedLLMClient


def _invoke(client: ScriptedLLMClient, task: str):
    return client.invoke_json(
        task=task,
        prompt_version="test-v1",
        messages=({"role": "user", "content": "{}"},),
        required_keys=("display_summary",),
    )


def test_returns_only_the_explicit_response_for_the_current_task():
    response = {
        "intent_binding": {},
        "business_summary": "已识别本轮分析目标。",
        "status_message": "准备确认业务边界。",
        "display_summary": "已识别本轮分析目标。",
    }
    client = ScriptedLLMClient({"single_authority_intent": response})

    result = _invoke(client, "single_authority_intent")

    assert result.output == response
    assert result.audit["structured_output"] == response
    client.assert_exhausted()


def test_missing_task_fails_without_a_default_business_response():
    client = ScriptedLLMClient(
        {"single_authority_intent": {"display_summary": "显式响应"}}
    )

    with pytest.raises(
        AssertionError,
        match="scripted_llm_response_missing:single_authority_plan_proposal",
    ):
        _invoke(client, "single_authority_plan_proposal")


def test_repeated_task_consumes_an_explicit_response_sequence():
    client = ScriptedLLMClient(
        {
            "single_authority_clarification": [
                {"display_summary": "第一次澄清"},
                {"display_summary": "第二次澄清"},
            ]
        }
    )

    assert (
        _invoke(client, "single_authority_clarification").output["display_summary"]
        == "第一次澄清"
    )
    assert (
        _invoke(client, "single_authority_clarification").output["display_summary"]
        == "第二次澄清"
    )
    with pytest.raises(
        AssertionError,
        match="scripted_llm_response_missing:single_authority_clarification",
    ):
        _invoke(client, "single_authority_clarification")
