import pytest

from tests.support.scripted_llm import ScriptedLLMClient


def _invoke(client: ScriptedLLMClient, task: str):
    return client.invoke_json(
        task=task,
        prompt_version="test-v1",
        messages=({"role": "user", "content": "{}"},),
        required_keys=("summary_text",),
    )


def test_returns_only_the_explicit_response_for_the_current_task():
    response = {
        "summary_text": "本轮结论由当前测试明确提供。",
        "statement_bindings": [],
    }
    client = ScriptedLLMClient({"final_business_summary": response})

    result = _invoke(client, "final_business_summary")

    assert result.output == response
    assert result.audit["structured_output"] == response
    client.assert_exhausted()


def test_missing_task_fails_without_a_default_business_response():
    client = ScriptedLLMClient(
        {"final_business_summary": {"summary_text": "显式响应"}}
    )

    with pytest.raises(
        AssertionError,
        match="scripted_llm_response_missing:business_intent",
    ):
        _invoke(client, "business_intent")


def test_repeated_task_consumes_an_explicit_response_sequence():
    client = ScriptedLLMClient(
        {
            "answer_repair": [
                {"summary_text": "第一次修订"},
                {"summary_text": "第二次修订"},
            ]
        }
    )

    assert _invoke(client, "answer_repair").output["summary_text"] == "第一次修订"
    assert _invoke(client, "answer_repair").output["summary_text"] == "第二次修订"
    with pytest.raises(
        AssertionError,
        match="scripted_llm_response_missing:answer_repair",
    ):
        _invoke(client, "answer_repair")
