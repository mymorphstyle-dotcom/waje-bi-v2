from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Mapping

import httpx
from agents.tracing import get_trace_provider
from pydantic import BaseModel, ConfigDict
import pytest

from bi_agent.runtime.agent_sdk_contracts import (
    WajeAgentRunRequest,
    WajeAgentTool,
)
from bi_agent.runtime.agents_sdk_adapter import WajeAgentsSdkAdapter
from bi_agent.runtime.agents_sdk_trace import (
    InMemoryAgentTraceSink,
    PostgresAgentTraceSink,
    WajeTraceProcessor,
)
from bi_agent.runtime.llm_client import LLMConfigurationError, LLMProviderError
from bi_agent.runtime.mainland_model_provider import (
    MainlandModelCapabilities,
    MainlandModelProvider,
    MainlandModelSettings,
    MainlandProviderConfig,
    ProviderCapabilityError,
)
from bi_agent.runtime.provider_capability_probe import ProviderCapabilityProbe


ROOT = Path(__file__).resolve().parents[2]


class _NumberInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: int


class _TypedAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str
    evidence_refs: list[str]


def _capabilities(**overrides: Any) -> MainlandModelCapabilities:
    values = {
        "text_generation": True,
        "function_calling": True,
        "structured_output": True,
        "streaming_text": True,
        "streaming_tool_calls": True,
        "typed_error_mapping": True,
        "context_window_tokens": 1_000_000,
        "max_output_tokens": 8_192,
        "thinking": True,
    }
    values.update(overrides)
    return MainlandModelCapabilities(**values)


def _config(
    *,
    base_url: str = "https://model.provider.example.cn/v1",
    max_attempts: int = 1,
    circuit_failure_threshold: int = 5,
) -> MainlandProviderConfig:
    return MainlandProviderConfig(
        provider="test-mainland",
        base_url=base_url,
        api_key="mainland-test-key",
        model="mainland-model",
        model_settings=MainlandModelSettings(
            max_output_tokens=512,
            thinking="disabled",
        ),
        capabilities=_capabilities(),
        max_attempts=max_attempts,
        circuit_failure_threshold=circuit_failure_threshold,
    )


def _chat_response(
    *,
    content: str | None = None,
    tool_name: str = "",
    arguments: str = "",
    call_id: str = "",
    status_code: int = 200,
    reasoning_content: str | None = None,
) -> httpx.Response:
    if status_code != 200:
        return httpx.Response(
            status_code,
            json={
                "error": {
                    "message": "private provider error",
                    "type": "authentication_error",
                    "code": "invalid_api_key",
                }
            },
        )
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning_content is not None:
        message["reasoning_content"] = reasoning_content
    finish_reason = "stop"
    if tool_name:
        finish_reason = "tool_calls"
        message["tool_calls"] = [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": tool_name, "arguments": arguments},
            }
        ]
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-mainland-test",
            "object": "chat.completion",
            "created": 1,
            "model": "mainland-model",
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": 4,
                "completion_tokens": 2,
                "total_tokens": 6,
            },
        },
    )


def _stream_response(chunks: list[Mapping[str, Any]]) -> httpx.Response:
    body = "".join(
        f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n" for chunk in chunks
    )
    body += "data: [DONE]\n\n"
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=body.encode("utf-8"),
    )


def _text_stream(text: str) -> httpx.Response:
    return _stream_response(
        [
            {
                "id": "chatcmpl-stream-text",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "mainland-model",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": text},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chatcmpl-stream-text",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "mainland-model",
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "stop"}
                ],
            },
        ]
    )


def _tool_stream(
    *,
    tool_name: str,
    call_id: str,
    argument_parts: tuple[str, ...],
) -> httpx.Response:
    chunks: list[Mapping[str, Any]] = []
    for index, arguments in enumerate(argument_parts):
        function: dict[str, Any] = {"arguments": arguments}
        tool_call: dict[str, Any] = {"index": 0, "function": function}
        if index == 0:
            function["name"] = tool_name
            tool_call.update({"id": call_id, "type": "function"})
        chunks.append(
            {
                "id": "chatcmpl-stream-tool",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "mainland-model",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"tool_calls": [tool_call]},
                        "finish_reason": None,
                    }
                ],
            }
        )
    chunks.append(
        {
            "id": "chatcmpl-stream-tool",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "mainland-model",
            "choices": [
                {"index": 0, "delta": {}, "finish_reason": "tool_calls"}
            ],
        }
    )
    return _stream_response(chunks)


def _adapter(
    handler: Any,
    *,
    config: MainlandProviderConfig | None = None,
) -> tuple[MainlandModelProvider, WajeAgentsSdkAdapter, InMemoryAgentTraceSink]:
    provider = MainlandModelProvider(
        config or _config(),
        http_transport=httpx.MockTransport(handler),
    )
    sink = InMemoryAgentTraceSink()
    return provider, WajeAgentsSdkAdapter(provider=provider, trace_sink=sink), sink


def test_direct_response_uses_only_explicit_chat_completions_without_openai_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _chat_response(content="大陆模型直答")

    provider, adapter, sink = _adapter(handler)
    try:
        result = asyncio.run(
            adapter.run(
                WajeAgentRunRequest(
                    run_id="run-direct",
                    agent_name="waje_general_agent",
                    instructions="直接回答用户。",
                    input_text="请给出一句回答。",
                    trace_metadata={
                        "waje_run_id": "spoofed-run",
                        "provider": "spoofed-provider",
                    },
                )
            )
        )
    finally:
        asyncio.run(provider.close())

    assert result.final_output == "大陆模型直答"
    assert result.model_turns == 1
    assert "model" not in result.__dict__
    assert [request.url.path for request in requests] == ["/v1/chat/completions"]
    assert {request.url.host for request in requests} == {
        "model.provider.example.cn"
    }
    payload = json.loads(requests[0].content)
    assert "previous_response_id" not in payload
    assert "conversation_id" not in payload
    assert "store" not in payload
    assert sink.records
    assert {record["event_type"] for record in sink.records} >= {
        "trace_started",
        "trace_finished",
        "span_started",
        "span_finished",
    }
    assert "mainland-model" in json.dumps(sink.records)
    assert all(
        record["waje_trace_metadata"]["waje_run_id"] == "run-direct"
        for record in sink.records
    )
    assert all(
        record["waje_trace_metadata"]["provider"] == "test-mainland"
        for record in sink.records
    )
    processors = get_trace_provider().__dict__["_multi_processor"].__dict__[
        "_processors"
    ]
    assert len(processors) == 1
    assert isinstance(processors[0], WajeTraceProcessor)


def test_postgres_trace_sink_routes_by_waje_trace_metadata() -> None:
    class Store:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def add_audit_event(self, event_type: str, **kwargs: Any) -> None:
            self.calls.append({"event_type": event_type, **kwargs})

    store = Store()
    sink = PostgresAgentTraceSink(store)
    sink.write_trace_record(
        {
            "object": "trace.span",
            "id": "span-local",
            "trace_id": "trace-local",
            "waje_trace_metadata": {
                "waje_run_id": "run-local",
                "waje_thread_id": "thread-local",
                "waje_topic_id": "topic-local",
            },
        }
    )

    assert store.calls == [
        {
            "event_type": "agents_sdk_trace_recorded",
            "thread_id": "thread-local",
            "topic_id": "topic-local",
            "run_id": "run-local",
            "ref": "trace-local",
            "payload": {
                "object": "trace.span",
                "id": "span-local",
                "trace_id": "trace-local",
                "waje_trace_metadata": {
                    "waje_run_id": "run-local",
                    "waje_thread_id": "thread-local",
                    "waje_topic_id": "topic-local",
                },
            },
        }
    ]


def test_runner_completes_one_function_tool_call() -> None:
    responses = iter(
        (
            _chat_response(
                tool_name="increment",
                arguments='{"value": 1}',
                call_id="call_increment_once",
            ),
            _chat_response(content="2"),
        )
    )
    tool_inputs: list[dict[str, Any]] = []

    def handler(_: httpx.Request) -> httpx.Response:
        return next(responses)

    def increment(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        tool_inputs.append(dict(arguments))
        return {"value": arguments["value"] + 1}

    provider, adapter, _ = _adapter(handler)
    try:
        result = asyncio.run(
            adapter.run(
                WajeAgentRunRequest(
                    run_id="run-one-tool",
                    agent_name="waje_general_agent",
                    instructions="调用工具后回答。",
                    input_text="把 1 加一。",
                    tools=(
                        WajeAgentTool(
                            name="increment",
                            description="Increment one integer.",
                            input_model=_NumberInput,
                            handler=increment,
                        ),
                    ),
                )
            )
        )
    finally:
        asyncio.run(provider.close())

    assert tool_inputs == [{"value": 1}]
    assert result.final_output == "2"
    assert result.model_turns == 2


def test_runner_completes_multi_round_tool_loop_with_stable_typed_arguments() -> None:
    responses = iter(
        (
            _chat_response(
                tool_name="increment",
                arguments='{"value": 1}',
                call_id="call_increment_1",
            ),
            _chat_response(
                tool_name="increment",
                arguments='{"value": 2}',
                call_id="call_increment_2",
            ),
            _chat_response(content="3"),
        )
    )
    requests: list[dict[str, Any]] = []
    tool_inputs: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return next(responses)

    def increment(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        tool_inputs.append(dict(arguments))
        return {"value": arguments["value"] + 1}

    provider, adapter, _ = _adapter(handler)
    try:
        result = asyncio.run(
            adapter.run(
                WajeAgentRunRequest(
                    run_id="run-multi-tool",
                    agent_name="waje_general_agent",
                    instructions="按需重复调用工具，完成后回答。",
                    input_text="从 1 连续加两次。",
                    tools=(
                        WajeAgentTool(
                            name="increment",
                            description="Increment one integer.",
                            input_model=_NumberInput,
                            handler=increment,
                        ),
                    ),
                    max_turns=5,
                )
            )
        )
    finally:
        asyncio.run(provider.close())

    assert tool_inputs == [{"value": 1}, {"value": 2}]
    assert result.final_output == "3"
    assert result.model_turns == 3
    assert [
        message["tool_call_id"]
        for request in requests[1:]
        for message in request["messages"]
        if message["role"] == "tool"
    ] == ["call_increment_1", "call_increment_1", "call_increment_2"]


def test_runner_returns_waje_mapping_for_strongly_typed_final_output() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return _chat_response(
            content=json.dumps(
                {"answer": "已验证", "evidence_refs": ["evidence:1"]},
                ensure_ascii=False,
            )
        )

    provider, adapter, _ = _adapter(handler)
    try:
        result = asyncio.run(
            adapter.run(
                WajeAgentRunRequest(
                    run_id="run-typed-output",
                    agent_name="waje_general_agent",
                    instructions="按最终 schema 返回。",
                    input_text="生成结构化回答。",
                    output_type=_TypedAnswer,
                )
            )
        )
    finally:
        asyncio.run(provider.close())

    assert result.final_output == {
        "answer": "已验证",
        "evidence_refs": ["evidence:1"],
    }
    assert requests[0]["response_format"] == {"type": "json_object"}
    assert "JSON" in requests[0]["messages"][0]["content"]
    assert '"evidence_refs"' in requests[0]["messages"][0]["content"]


def test_streaming_projects_text_and_tool_deltas_without_reasoning_content() -> None:
    responses = iter(
        (
            _tool_stream(
                tool_name="increment",
                call_id="call_stream_increment",
                argument_parts=('{"value":', " 4}"),
            ),
            _text_stream("5"),
        )
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return next(responses)

    provider, adapter, _ = _adapter(handler)
    try:
        result = asyncio.run(
            adapter.run_streamed(
                WajeAgentRunRequest(
                    run_id="run-stream-tool",
                    agent_name="waje_general_agent",
                    instructions="调用工具后流式回答。",
                    input_text="把 4 加一。",
                    tools=(
                        WajeAgentTool(
                            name="increment",
                            description="Increment one integer.",
                            input_model=_NumberInput,
                            handler=lambda arguments: {
                                "value": arguments["value"] + 1
                            },
                        ),
                    ),
                )
            )
        )
    finally:
        asyncio.run(provider.close())

    assert result.final_output == "5"
    assert {event.kind for event in result.stream_events} >= {
        "tool_call_delta",
        "tool_called",
        "tool_output",
        "model_text_delta",
    }
    assert "reasoning" not in json.dumps(
        [event.__dict__ for event in result.stream_events]
    )


def test_provider_maps_http_error_and_opens_circuit_at_provider_boundary() -> None:
    request_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return _chat_response(status_code=500)

    provider, adapter, _ = _adapter(
        handler,
        config=_config(circuit_failure_threshold=2),
    )
    request = WajeAgentRunRequest(
        run_id="run-provider-failure",
        agent_name="waje_general_agent",
        instructions="回答。",
        input_text="触发错误。",
    )
    try:
        for _ in range(2):
            with pytest.raises(LLMProviderError) as captured:
                asyncio.run(adapter.run(request))
            assert captured.value.kind == "provider_unavailable"
            assert captured.value.retryability == "retryable"
        with pytest.raises(LLMProviderError) as circuit:
            asyncio.run(adapter.run(request))
    finally:
        asyncio.run(provider.close())

    assert circuit.value.error_code == "provider_circuit_open"
    assert request_count == 2


def test_provider_maps_authentication_failure_to_waje_typed_error() -> None:
    provider, adapter, _ = _adapter(
        lambda _: _chat_response(status_code=401),
    )
    try:
        with pytest.raises(LLMProviderError) as captured:
            asyncio.run(
                adapter.run(
                    WajeAgentRunRequest(
                        run_id="run-provider-auth",
                        agent_name="waje_general_agent",
                        instructions="回答。",
                        input_text="验证认证错误。",
                    )
                )
            )
    finally:
        asyncio.run(provider.close())

    assert captured.value.kind == "provider_authentication_failed"
    assert captured.value.retryability == "not_retryable"
    assert captured.value.provider_error == {
        "status_code": 401,
        "code": "invalid_api_key",
        "type": "authentication_error",
    }
    assert "private provider error" not in json.dumps(
        captured.value.provider_error
    )


def test_provider_retry_is_centralized_in_explicit_openai_compatible_client() -> None:
    request_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request_count == 1:
            return _chat_response(status_code=500)
        return _chat_response(content="retry-complete")

    provider, adapter, _ = _adapter(handler, config=_config(max_attempts=2))
    try:
        result = asyncio.run(
            adapter.run(
                WajeAgentRunRequest(
                    run_id="run-provider-retry",
                    agent_name="waje_general_agent",
                    instructions="回答。",
                    input_text="验证 provider retry。",
                )
            )
        )
    finally:
        asyncio.run(provider.close())

    assert result.final_output == "retry-complete"
    assert request_count == 2


def test_capability_probe_covers_required_live_contract_and_declared_limits() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        prompt = "\n".join(
            str(message.get("content") or "") for message in payload["messages"]
        )
        if "WAJE_STRUCTURED_PROBE_OK" in prompt:
            return _chat_response(
                content='{"marker":"WAJE_STRUCTURED_PROBE_OK","ok":true}'
            )
        if "WAJE_TOOL_PROBE_OK" in prompt:
            if any(message["role"] == "tool" for message in payload["messages"]):
                return _text_stream("WAJE_TOOL_PROBE_OK")
            return _tool_stream(
                tool_name="probe_echo",
                call_id="call_probe_echo",
                argument_parts=('{"marker":"WAJE_', 'TOOL_PROBE_OK"}'),
            )
        if "WAJE_STREAM_PROBE_OK" in prompt:
            return _text_stream("WAJE_STREAM_PROBE_OK")
        return _chat_response(
            content="WAJE_TEXT_PROBE_OK",
            reasoning_content="provider thinking capability observed",
        )

    config = MainlandProviderConfig(
        provider="deepseek",
        base_url="https://api.deepseek.com/v1",
        api_key="deepseek-test-key",
        model="deepseek-v4-flash",
        model_settings=MainlandModelSettings(
            max_output_tokens=512,
            thinking="enabled",
        ),
        capabilities=_capabilities(),
        max_attempts=1,
    )
    provider, adapter, _ = _adapter(handler, config=config)
    try:
        report = asyncio.run(
            ProviderCapabilityProbe(provider=provider, adapter=adapter).run()
        )
    finally:
        asyncio.run(provider.close())

    assert report.provider == "deepseek"
    assert report.transport == "openai_compatible_chat_completions"
    assert all(report.checks.values())
    assert report.context_window_tokens == 1_000_000
    assert report.max_output_tokens == 8_192
    assert report.thinking is True
    assert all(request["max_tokens"] == 512 for request in requests)
    assert all(
        request["thinking"] == {"type": "enabled"} for request in requests
    )
    structured_request = next(
        request
        for request in requests
        if "WAJE_STRUCTURED_PROBE_OK"
        in "\n".join(
            str(message.get("content") or "")
            for message in request["messages"]
        )
    )
    assert structured_request["response_format"] == {"type": "json_object"}


def test_provider_configuration_forbids_defaults_openai_endpoint_and_missing_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(LLMConfigurationError, match="openai_endpoint_forbidden"):
        MainlandProviderConfig(
            provider="deepseek",
            base_url="https://api.openai.com/v1",
            api_key="key",
            model="model",
            model_settings=MainlandModelSettings(128, "disabled"),
            capabilities=_capabilities(),
        )

    with pytest.raises(LLMConfigurationError, match="sdk_default_model_forbidden"):
        provider = MainlandModelProvider(_config())
        try:
            provider.get_model(None)
        finally:
            asyncio.run(provider.close())

    with pytest.raises(ProviderCapabilityError) as missing:
        MainlandModelProvider(
            MainlandProviderConfig(
                provider="test-mainland",
                base_url="https://model.provider.example.cn/v1",
                api_key="key",
                model="model",
                model_settings=MainlandModelSettings(128, "disabled"),
                capabilities=_capabilities(function_calling=False),
            )
        )
    assert missing.value.missing == ("function_calling",)

    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-used")
    with pytest.raises(LLMConfigurationError, match="missing_llm_api_key"):
        MainlandProviderConfig.deepseek_from_env(
            {
                "WAJE_LLM_PROVIDER": "deepseek",
                "WAJE_LLM_BASE_URL": "https://api.deepseek.com/v1",
                "WAJE_LLM_MODEL": "deepseek-v4-flash",
                "WAJE_LLM_CONTEXT_WINDOW_TOKENS": "1000000",
                "WAJE_LLM_MAX_OUTPUT_TOKENS": "8192",
                "WAJE_LLM_THINKING": "enabled",
                "OPENAI_API_KEY": os.environ["OPENAI_API_KEY"],
            }
        )


def test_deepseek_factory_resolves_explicit_v4_model_settings_for_current_config() -> None:
    config = MainlandProviderConfig.deepseek_from_env(
        {
            "WAJE_LLM_PROVIDER": "current-deepseek-adapter",
            "WAJE_LLM_BASE_URL": "https://api.deepseek.com/v1",
            "WAJE_LLM_MODEL": "deepseek-v4-flash",
            "WAJE_LLM_API_KEY": "deepseek-key",
        }
    )

    assert config.provider == "deepseek"
    assert config.capabilities.context_window_tokens == 1_000_000
    assert config.capabilities.max_output_tokens == 8_192
    assert config.model_settings.max_output_tokens == 8_192
    assert config.model_settings.thinking == "enabled"

    with pytest.raises(
        LLMConfigurationError,
        match="missing_waje_llm_context_window_tokens",
    ):
        MainlandProviderConfig.deepseek_from_env(
            {
                "WAJE_LLM_PROVIDER": "current-deepseek-adapter",
                "WAJE_LLM_BASE_URL": "https://api.deepseek.com/v1",
                "WAJE_LLM_MODEL": "future-deepseek-model",
                "WAJE_LLM_API_KEY": "deepseek-key",
            }
        )


def test_sdk_imports_remain_inside_python_adapter_provider_and_trace_boundary() -> None:
    allowed = {
        ROOT / "bi_agent/runtime/agents_sdk_adapter.py",
        ROOT / "bi_agent/runtime/agents_sdk_trace.py",
        ROOT / "bi_agent/runtime/mainland_model_provider.py",
    }
    leaked: list[str] = []
    for path in (ROOT / "bi_agent").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if path not in allowed and (
            "from agents" in source or "import agents" in source
        ):
            leaked.append(str(path.relative_to(ROOT)))
    assert leaked == []

    customer_sources = "\n".join(
        path.read_text(encoding="utf-8") for path in (ROOT / "app").rglob("*.ts*")
    )
    for sdk_type in (
        "OpenAIChatCompletionsModel",
        "RunResultStreaming",
        "FunctionTool",
        "ModelProvider",
    ):
        assert sdk_type not in customer_sources
