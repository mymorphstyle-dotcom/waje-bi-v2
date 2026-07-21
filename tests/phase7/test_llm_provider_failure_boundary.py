from __future__ import annotations

from collections.abc import Callable
import json
import os
from time import perf_counter
from typing import Any

import httpx
from openai import BadRequestError
import pytest

from bi_agent.runtime.llm_client import (
    LLMConfigurationError,
    LLMOutputError,
    LLMProviderError,
    LLMTimeoutError,
    OpenAICompatibleLLMClient,
    _llm_provider_error_from_openai,
    _request_openai_json_in_subprocess,
)


def _retryable_provider_worker(*_: Any) -> dict[str, Any]:
    raise LLMProviderError(
        kind="provider_rate_limited",
        retryability="retryable",
    )


def _diagnostic_provider_worker(*_: Any) -> dict[str, Any]:
    raise LLMProviderError(
        kind="provider_request_rejected",
        retryability="not_retryable",
        status_code=400,
        error_code="context_length_exceeded",
        error_type="invalid_request_error",
        error_param="messages",
    )


def _programming_error_worker(*_: Any) -> dict[str, Any]:
    raise AssertionError("worker_programming_error_text_must_not_be_classified")


def _exit_without_result_worker(*_: Any) -> dict[str, Any]:
    os._exit(17)


class _FailureClient(OpenAICompatibleLLMClient):
    def __init__(self, factory: Callable[[], BaseException]) -> None:
        super().__init__(
            provider="test-mainland",
            model="provider-boundary-test",
            api_key="test-key",
            base_url="https://model.provider.example.cn/v1",
            max_attempts=3,
        )
        self.factory = factory
        self.calls = 0

    def _request_json_once(self, *_: Any, **__: Any) -> dict[str, Any]:
        self.calls += 1
        raise self.factory()


class _OutputClient(OpenAICompatibleLLMClient):
    def __init__(self) -> None:
        super().__init__(
            provider="test-mainland",
            model="provider-boundary-test",
            api_key="test-key",
            base_url="https://model.provider.example.cn/v1",
            max_attempts=3,
        )

    def _request_json_once(self, *_: Any, **__: Any) -> dict[str, Any]:
        return {
            "response_id": "response:typed-output",
            "content": '{"items":[]}',
            "usage": {},
            "reasoning_content_present": False,
        }


def _invoke(client: OpenAICompatibleLLMClient) -> None:
    client.invoke_json(
        task="provider_boundary_test",
        prompt_version="test.v1",
        messages=({"role": "user", "content": "{}"},),
        required_keys=(),
    )


def test_base_client_emits_one_typed_failure_per_provider_request() -> None:
    retryable = _FailureClient(
        lambda: LLMProviderError(
            kind="provider_rate_limited",
            retryability="retryable",
        )
    )
    with pytest.raises(LLMProviderError) as captured:
        _invoke(retryable)
    assert retryable.calls == 1
    assert captured.value.kind == "provider_rate_limited"
    assert captured.value.retryability == "retryable"

    rejected = _FailureClient(
        lambda: LLMProviderError(
            kind="provider_request_rejected",
            retryability="not_retryable",
        )
    )
    with pytest.raises(LLMProviderError):
        _invoke(rejected)
    assert rejected.calls == 1


def test_value_error_from_typed_validator_is_audited_as_invalid_llm_output() -> None:
    client = _OutputClient()

    def reject(_: Any) -> None:
        raise ValueError("candidate_claim_output_item_shape_invalid")

    with pytest.raises(
        LLMOutputError,
        match="^candidate_claim_output_item_shape_invalid$",
    ) as captured:
        client.invoke_json(
            task="provider_boundary_test",
            prompt_version="test.v1",
            messages=({"role": "user", "content": "{}"},),
            required_keys=("items",),
            output_validator=reject,
        )

    assert captured.value.audit["failure_code"] == (
        "candidate_claim_output_item_shape_invalid"
    )
    assert captured.value.audit["attempt_failures"][0]["failure_code"] == (
        "candidate_claim_output_item_shape_invalid"
    )
    audit = captured.value.audit
    attempt = audit["attempt_failures"][0]
    assert "messages" not in audit
    assert "raw_response_content" not in audit
    assert "structured_output" not in audit
    assert audit["input_message_count"] == 1
    assert audit["input_bytes"] > 0
    assert len(audit["input_hash"]) == 64
    assert attempt["raw_response_bytes"] == len('{"items":[]}'.encode("utf-8"))
    assert len(attempt["raw_response_digest"]) == 64
    assert attempt["structured_output_bytes"] > 0
    assert len(attempt["structured_output_digest"]) == 64
    serialized = json.dumps(audit)
    assert '{"items":[]}' not in serialized


def test_success_audit_keeps_full_input_and_output_replay_provenance() -> None:
    result = _OutputClient().invoke_json(
        task="provider_boundary_test",
        prompt_version="test.v1",
        messages=({"role": "user", "content": "{}"},),
        required_keys=("items",),
    )

    assert result.audit["messages"] == [{"role": "user", "content": "{}"}]
    assert result.audit["raw_response_content"] == '{"items":[]}'
    assert result.audit["structured_output"] == {"items": []}


def test_programming_error_from_typed_validator_remains_visible() -> None:
    client = _OutputClient()

    def fail(_: Any) -> None:
        raise AssertionError("validator_programming_error")

    with pytest.raises(AssertionError, match="^validator_programming_error$"):
        client.invoke_json(
            task="provider_boundary_test",
            prompt_version="test.v1",
            messages=({"role": "user", "content": "{}"},),
            required_keys=("items",),
            output_validator=fail,
        )


def test_openai_status_error_keeps_only_structured_provider_diagnostics() -> None:
    response = httpx.Response(
        400,
        request=httpx.Request(
            "POST",
            "https://provider.example/v1/chat/completions",
        ),
    )
    provider_error = BadRequestError(
        "raw exception message must stay private",
        response=response,
        body={
            "error": {
                "message": "raw provider body must stay private",
                "code": "context_length_exceeded",
                "type": "invalid_request_error",
                "param": "messages",
            }
        },
    )

    mapped = _llm_provider_error_from_openai(provider_error)

    assert mapped.kind == "provider_request_rejected"
    assert mapped.retryability == "not_retryable"
    assert mapped.provider_error == {
        "status_code": 400,
        "code": "context_length_exceeded",
        "type": "invalid_request_error",
        "param": "messages",
    }
    serialized = json.dumps(mapped.provider_error)
    assert "raw exception message" not in serialized
    assert "raw provider body" not in serialized


def test_provider_failure_audit_exposes_safe_diagnostics_without_raw_error() -> None:
    client = _FailureClient(
        lambda: LLMProviderError(
            kind="provider_request_rejected",
            retryability="not_retryable",
            status_code=400,
            error_code="context_length_exceeded",
            error_type="invalid_request_error",
            error_param="messages",
        )
    )

    with pytest.raises(LLMProviderError) as captured:
        _invoke(client)

    expected = {
        "status_code": 400,
        "code": "context_length_exceeded",
        "type": "invalid_request_error",
        "param": "messages",
    }
    assert captured.value.provider_error == expected
    assert captured.value.audit["attempt_failures"] == [
        {
            "attempt": 1,
            "failure_code": "provider_request_rejected",
            "response_id": "",
            "reasoning_content_present": False,
            "provider_error": expected,
        }
    ]
    serialized = json.dumps(captured.value.audit)
    assert captured.value.audit["task"] == "provider_boundary_test"
    assert captured.value.audit["provider"] == "test-mainland"
    assert captured.value.audit["model"] == "provider-boundary-test"
    assert captured.value.audit["prompt_version"] == "test.v1"
    assert captured.value.audit["status"] == "failed"
    assert captured.value.audit["attempt_count"] == 1
    assert captured.value.audit["duration_ms"] >= 0
    assert captured.value.audit["usage"] == {}
    assert captured.value.audit["input_message_count"] == 1
    assert captured.value.audit["input_bytes"] > 0
    assert len(captured.value.audit["input_hash"]) == 64
    assert "messages" not in captured.value.audit
    assert "message" not in captured.value.audit["attempt_failures"][0]
    assert "body" not in captured.value.audit["attempt_failures"][0]
    assert "raw_response_content" not in serialized


@pytest.mark.parametrize(
    "factory, expected_error, expected_calls",
    (
        (lambda: LLMTimeoutError("timeout text"), LLMTimeoutError, 1),
        (
            lambda: LLMConfigurationError("configuration text"),
            LLMConfigurationError,
            1,
        ),
        (lambda: AssertionError("programming text"), AssertionError, 1),
    ),
)
def test_retry_policy_does_not_retry_configuration_or_programming_errors(
    factory: Callable[[], BaseException],
    expected_error: type[BaseException],
    expected_calls: int,
) -> None:
    client = _FailureClient(factory)
    with pytest.raises(expected_error):
        _invoke(client)
    assert client.calls == expected_calls


def test_subprocess_rebuilds_only_typed_provider_failures() -> None:
    with pytest.raises(LLMProviderError) as captured:
        _request_openai_json_in_subprocess(
            {},
            ({"role": "user", "content": "{}"},),
            5,
            request_worker=_retryable_provider_worker,
        )
    assert captured.value.kind == "provider_rate_limited"
    assert captured.value.retryability == "retryable"


def test_subprocess_preserves_safe_provider_diagnostics() -> None:
    with pytest.raises(LLMProviderError) as captured:
        _request_openai_json_in_subprocess(
            {},
            ({"role": "user", "content": "{}"},),
            5,
            request_worker=_diagnostic_provider_worker,
        )

    assert captured.value.kind == "provider_request_rejected"
    assert captured.value.retryability == "not_retryable"
    assert captured.value.provider_error == {
        "status_code": 400,
        "code": "context_length_exceeded",
        "type": "invalid_request_error",
        "param": "messages",
    }


def test_subprocess_programming_error_is_not_downgraded_to_provider_failure() -> None:
    with pytest.raises(
        RuntimeError,
        match="^llm_subprocess_worker_failed:AssertionError$",
    ):
        _request_openai_json_in_subprocess(
            {},
            ({"role": "user", "content": "{}"},),
            5,
            request_worker=_programming_error_worker,
        )


def test_subprocess_exit_without_result_cannot_stall_when_timeout_is_disabled() -> None:
    started = perf_counter()
    with pytest.raises(RuntimeError, match="^llm_subprocess_failed:exitcode=17$"):
        _request_openai_json_in_subprocess(
            {},
            ({"role": "user", "content": "{}"},),
            None,
            request_worker=_exit_without_result_worker,
        )
    assert perf_counter() - started < 3


@pytest.mark.parametrize(
    "field, value, expected_error",
    (
        ("model_tier", "typo", "invalid_llm_model_tier"),
        ("thinking", "auto", "invalid_llm_thinking_mode"),
    ),
)
def test_closed_runtime_controls_reject_unknown_values(
    field: str,
    value: str,
    expected_error: str,
) -> None:
    client = _FailureClient(lambda: AssertionError("must not call provider"))
    kwargs = {
        "task": "provider_boundary_test",
        "prompt_version": "test.v1",
        "messages": ({"role": "user", "content": "{}"},),
        "required_keys": (),
        field: value,
    }
    with pytest.raises(LLMConfigurationError, match=f"^{expected_error}$"):
        client.invoke_json(**kwargs)
    assert client.calls == 0


def test_shared_langgraph_client_requires_explicit_mainland_endpoint_and_key() -> None:
    base = {
        "WAJE_LLM_PROVIDER": "deepseek",
        "WAJE_LLM_MODEL": "deepseek-v4-flash",
        "WAJE_LLM_BASE_URL": "https://api.deepseek.com/v1",
    }
    with pytest.raises(LLMConfigurationError, match="^missing_llm_api_key$"):
        OpenAICompatibleLLMClient.from_env(
            {**base, "OPENAI_API_KEY": "must-not-be-used"}
        )
    with pytest.raises(LLMConfigurationError, match="^missing_llm_base_url$"):
        OpenAICompatibleLLMClient.from_env(
            {
                "WAJE_LLM_PROVIDER": "deepseek",
                "WAJE_LLM_MODEL": "deepseek-v4-flash",
                "DEEPSEEK_API_KEY": "deepseek-key",
            }
        )
    with pytest.raises(LLMConfigurationError, match="^openai_endpoint_forbidden$"):
        OpenAICompatibleLLMClient.from_env(
            {
                "WAJE_LLM_PROVIDER": "deepseek",
                "WAJE_LLM_MODEL": "deepseek-v4-flash",
                "WAJE_LLM_BASE_URL": "https://api.openai.com/v1",
                "DEEPSEEK_API_KEY": "deepseek-key",
            }
        )
