from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
from unittest.mock import AsyncMock, patch

import httpx
from agents.tracing import get_trace_provider
from pydantic import BaseModel, ConfigDict
import pytest

from bi_agent.runtime.agent_context import AgentContextAssembler, InMemoryArtifactIndex
from bi_agent.runtime.agent_interaction_tools import agent_interaction_tools
from bi_agent.runtime.agent_sdk_contracts import (
    AgentSdkAdapterError,
    AgentToolResult,
    WajeAgentRunRequest,
    WajeAgentTool,
    WajePreboundToolCall,
)
from bi_agent.runtime.agent_turn_runtime import AgentTurnRequest, AgentTurnRuntime
from bi_agent.runtime.agents_sdk_adapter import (
    WajeAgentsSdkAdapter,
    _install_sdk_log_redaction,
    _to_sdk_tool,
)
from bi_agent.runtime.agents_sdk_trace import (
    AgentTraceStorageError,
    AgentTraceStoragePolicy,
    InMemoryAgentTraceSink,
    PostgresAgentTraceSink,
    WajeTraceProcessor,
)
from bi_agent.runtime.llm_client import (
    LLMConfigurationError,
    LLMProviderError,
    llm_provider_error_from_exception,
)
from bi_agent.runtime.mainland_model_provider import (
    MainlandModelCapabilities,
    MainlandModelProvider,
    MainlandModelSettings,
    MainlandProviderConfig,
    PostgresProviderCircuit,
    ProviderCapabilityError,
)
from bi_agent.runtime.provider_capability_probe import ProviderCapabilityProbe


def test_agents_sdk_error_logger_redacts_model_input(caplog: pytest.LogCaptureFixture) -> None:
    _install_sdk_log_redaction()
    caplog.set_level(logging.ERROR, logger="openai.agents")

    logging.getLogger("openai.agents").error(
        "Error getting response; filtered.input=%s",
        [{"content": "customer-secret"}],
    )

    assert "model input redacted" in caplog.text
    assert "customer-secret" not in caplog.text
from bi_agent.runtime.thread_item_ledger import InMemoryThreadItemLedger


ROOT = Path(__file__).resolve().parents[2]


class _NumberInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: int


class _TypedAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str
    evidence_refs: list[str]


def test_customer_summary_tool_failure_stops_after_persisting_one_result() -> None:
    class EventSink:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []
            self.results: list[tuple[str, str, bool, Any]] = []

        async def record_tool_call(self, **kwargs: Any) -> None:
            self.calls.append((kwargs["tool_name"], kwargs["call_id"]))

        async def record_tool_result(self, **kwargs: Any) -> None:
            self.results.append(
                (
                    kwargs["tool_name"],
                    kwargs["call_id"],
                    kwargs["succeeded"],
                    kwargs["result"],
                )
            )

    sink = EventSink()
    result = AgentToolResult(
        status="failed",
        output=None,
        artifactRefs=[],
        materialRefs=[],
        limitationRefs=[],
        retryability="replan_required",
        customerSummary="当前线程中没有找到可用于解释的已发布材料。",
        technicalDetailRef=None,
    )
    sdk_tool = _to_sdk_tool(
        WajeAgentTool(
            name="inspect_analysis_artifact",
            description="Read one persisted customer-safe analysis artifact.",
            input_model=_NumberInput,
            handler=lambda _arguments: result,
            failure_recovery="customer_summary",
        ),
        event_sink=sink,
    )

    with pytest.raises(AgentSdkAdapterError) as captured:
        asyncio.run(
            sdk_tool.on_invoke_tool(
                SimpleNamespace(tool_call_id="call-missing-artifact"),
                '{"value":1}',
            )
        )

    assert captured.value.code == "agent_tool_terminal_failure"
    assert sink.calls == [
        ("inspect_analysis_artifact", "call-missing-artifact")
    ]
    assert len(sink.results) == 1
    assert sink.results[0][:3] == (
        "inspect_analysis_artifact",
        "call-missing-artifact",
        False,
    )
    assert sink.results[0][3]["customerSummary"] == result.customer_summary


def test_sdk_wrapper_preserves_nested_waje_terminal_tool_failure() -> None:
    from bi_agent.runtime.agents_sdk_adapter import _mapped_sdk_error

    typed = AgentSdkAdapterError("agent_tool_terminal_failure")
    try:
        raise typed
    except AgentSdkAdapterError as cause:
        try:
            raise RuntimeError("sdk tool wrapper") from cause
        except RuntimeError as wrapper:
            mapped = _mapped_sdk_error(wrapper)

    assert mapped is typed


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
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
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
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
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
    assert {request.url.host for request in requests} == {"model.provider.example.cn"}
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
    processors = (
        get_trace_provider().__dict__["_multi_processor"].__dict__["_processors"]
    )
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


def test_postgres_trace_sink_enforces_payload_and_run_record_limits() -> None:
    class Store:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def add_audit_event(self, event_type: str, **kwargs: Any) -> None:
            self.calls.append({"event_type": event_type, **kwargs})

    store = Store()
    sink = PostgresAgentTraceSink(
        store,
        policy=AgentTraceStoragePolicy(
            max_record_bytes=300,
            max_records_per_run=1,
            retention_days=1,
        ),
    )
    record = {
        "id": "trace-limited",
        "waje_trace_metadata": {
            "waje_run_id": "run-limited",
            "waje_thread_id": "thread-limited",
        },
    }
    sink.write_trace_record(record)
    with pytest.raises(
        AgentTraceStorageError,
        match="agent_trace_storage_run_record_limit_exceeded",
    ):
        sink.write_trace_record(record)

    oversized = PostgresAgentTraceSink(
        store,
        policy=AgentTraceStoragePolicy(
            max_record_bytes=80,
            max_records_per_run=5,
            retention_days=1,
        ),
    )
    with pytest.raises(
        AgentTraceStorageError,
        match="agent_trace_storage_record_too_large",
    ):
        oversized.write_trace_record({**record, "span_data": {"input": "x" * 200}})

    rejections = [
        call for call in store.calls
        if call["event_type"] == "agents_sdk_trace_record_rejected"
    ]
    assert [item["payload"]["error_code"] for item in rejections] == [
        "agent_trace_storage_run_record_limit_exceeded",
        "agent_trace_storage_record_too_large",
    ]
    assert all("span_data" not in item["payload"] for item in rejections)


def test_repeated_adapters_share_one_processor_and_route_each_run_to_its_sink() -> None:
    first_provider, first_adapter, first_sink = _adapter(
        lambda _: _chat_response(content="first")
    )
    second_provider, second_adapter, second_sink = _adapter(
        lambda _: _chat_response(content="second")
    )
    try:
        asyncio.run(
            first_adapter.run(
                WajeAgentRunRequest(
                    run_id="run-trace-first",
                    agent_name="waje_general_agent",
                    instructions="回答。",
                    input_text="第一轮。",
                )
            )
        )
        asyncio.run(
            second_adapter.run(
                WajeAgentRunRequest(
                    run_id="run-trace-second",
                    agent_name="waje_general_agent",
                    instructions="回答。",
                    input_text="第二轮。",
                )
            )
        )
    finally:
        asyncio.run(first_provider.close())
        asyncio.run(second_provider.close())

    processors = (
        get_trace_provider().__dict__["_multi_processor"].__dict__["_processors"]
    )
    assert len(processors) == 1
    assert isinstance(processors[0], WajeTraceProcessor)
    assert {
        record["waje_trace_metadata"]["waje_run_id"]
        for record in first_sink.records
    } == {"run-trace-first"}
    assert {
        record["waje_trace_metadata"]["waje_run_id"]
        for record in second_sink.records
    } == {"run-trace-second"}


def test_trace_persistence_failure_is_a_typed_turn_failure() -> None:
    class FailedSink:
        def write_trace_record(self, record: Mapping[str, Any]) -> None:
            raise ConnectionError("raw trace database endpoint")

    provider = MainlandModelProvider(
        _config(),
        http_transport=httpx.MockTransport(
            lambda _: _chat_response(content="model completed")
        ),
    )
    adapter = WajeAgentsSdkAdapter(provider=provider, trace_sink=FailedSink())
    try:
        with pytest.raises(AgentSdkAdapterError) as captured:
            asyncio.run(
                adapter.run(
                    WajeAgentRunRequest(
                        run_id="run-trace-persistence-failure",
                        agent_name="waje_general_agent",
                        instructions="回答。",
                        input_text="验证 trace 故障。",
                    )
                )
            )
    finally:
        asyncio.run(provider.close())

    assert captured.value.code == "agent_trace_persistence_failed"
    assert captured.value.retryability == "retryable"
    assert captured.value.trace_failure["error_type"] == "ConnectionError"
    assert "database endpoint" not in str(captured.value)


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
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
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
                    initial_tool_choice="increment",
                    required_tool_name="increment",
                )
            )
        )
    finally:
        asyncio.run(provider.close())

    assert tool_inputs == [{"value": 1}]
    assert result.final_output == "2"
    assert result.model_turns == 2
    assert [(call.tool_name, call.call_id) for call in result.tool_calls] == [
        ("increment", "call_increment_once")
    ]
    first_payload = json.loads(requests[0].content) if requests else {}
    assert first_payload["tool_choice"] == {
        "type": "function",
        "function": {"name": "increment"},
    }


def test_prebound_read_only_tool_uses_one_real_provider_request() -> None:
    requests: list[dict[str, Any]] = []
    tool_inputs: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return _chat_response(
            content=json.dumps(
                {
                    "answer": "已读取持久化材料。",
                    "evidence_refs": ["evidence:one"],
                },
                ensure_ascii=False,
            )
        )

    def inspect(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        tool_inputs.append(dict(arguments))
        return {"value": arguments["value"] + 1}

    provider, adapter, sink = _adapter(handler)
    tool = WajeAgentTool(
        name="inspect",
        description="Read one persisted value.",
        input_model=_NumberInput,
        handler=inspect,
        prebinding_policy="read_only",
    )
    try:
        result = asyncio.run(
            adapter.run(
                WajeAgentRunRequest(
                    run_id="run-prebound-read",
                    agent_name="waje_general_agent",
                    instructions="读取工具材料后回答。",
                    input_text="解释已发布材料。",
                    tools=(tool,),
                    output_type=_TypedAnswer,
                    initial_tool_choice="inspect",
                    required_tool_name="inspect",
                    prebound_tool_call=WajePreboundToolCall(
                        tool_name="inspect",
                        call_id="call_waje_prebound_read",
                        arguments={"value": 1},
                    ),
                )
            )
        )
    finally:
        asyncio.run(provider.close())

    assert tool_inputs == [{"value": 1}]
    assert len(requests) == 1
    assert result.model_turns == 2
    assert result.usage["requests"] == 1
    assert result.final_output == {
        "answer": "已读取持久化材料。",
        "evidence_refs": ["evidence:one"],
    }
    assert [(call.tool_name, call.call_id) for call in result.tool_calls] == [
        ("inspect", "call_waje_prebound_read")
    ]
    synthetic_call = next(
        tool_call
        for message in requests[0]["messages"]
        for tool_call in message.get("tool_calls", [])
    )
    assert synthetic_call == {
        "id": "call_waje_prebound_read",
        "type": "function",
        "function": {"name": "inspect", "arguments": '{"value":1}'},
    }
    assert "tools" not in requests[0]
    assert "tool_choice" not in requests[0]
    assert any(
        record["waje_trace_metadata"]["provider_request_plan"]
        == "prebound_read_then_provider"
        for record in sink.records
    )


def test_prebound_tool_requires_explicit_read_only_policy() -> None:
    tool = WajeAgentTool(
        name="inspect",
        description="Read one value.",
        input_model=_NumberInput,
        handler=lambda arguments: arguments,
    )

    with pytest.raises(ValueError, match="agent_prebound_tool_policy_forbidden"):
        WajeAgentRunRequest(
            run_id="run-prebound-policy",
            agent_name="waje_general_agent",
            instructions="读取。",
            input_text="读取。",
            tools=(tool,),
            initial_tool_choice="inspect",
            required_tool_name="inspect",
            prebound_tool_call=WajePreboundToolCall(
                tool_name="inspect",
                call_id="call_waje_prebound_policy",
                arguments={"value": 1},
            ),
        )


def test_required_suspending_tool_corrects_contract_error_inside_runner() -> None:
    invalid_arguments = json.dumps(
        {
            "materialDecision": "请选择比较基线。",
            "materialDecisionTopics": ["baseline_or_counterfactual"],
            "options": [
                {
                    "optionId": "month",
                    "label": "对比上月",
                    "description": "与上一个完整月份比较。",
                    "recommended": False,
                },
                {
                    "optionId": "quarter",
                    "label": "对比上季度",
                    "description": "与上一个完整季度比较。",
                    "recommended": False,
                },
            ],
        },
        ensure_ascii=False,
    )
    valid_arguments = json.dumps(
        {
            "materialDecision": "请选择比较基线。",
            "materialDecisionTopics": ["baseline_or_counterfactual"],
            "options": [
                {
                    "optionId": "month",
                    "label": "对比上月",
                    "description": "与上一个完整月份比较。",
                    "recommended": True,
                },
                {
                    "optionId": "quarter",
                    "label": "对比上季度",
                    "description": "与上一个完整季度比较。",
                    "recommended": False,
                },
            ],
        },
        ensure_ascii=False,
    )
    responses = iter(
        (
            _chat_response(
                tool_name="ask_user",
                arguments=invalid_arguments,
                call_id="call_clarify_invalid",
            ),
            _chat_response(
                tool_name="ask_user",
                arguments=valid_arguments,
                call_id="call_clarify_valid",
            ),
        )
    )
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return next(responses)

    ask_user, _ = agent_interaction_tools(
        thread_id="thread-tool-correction",
        operation_id="operation-tool-correction",
        customer_language="zh-Hans",
    )
    provider, adapter, _ = _adapter(handler)
    try:
        result = asyncio.run(
            adapter.run(
                WajeAgentRunRequest(
                    run_id="run-tool-correction",
                    agent_name="waje_general_agent",
                    instructions="必须生成合法的中文澄清选项。",
                    input_text="需要选择哪个比较基线？",
                    tools=(ask_user,),
                    initial_tool_choice="ask_user",
                    required_tool_name="ask_user",
                    max_turns=4,
                )
            )
        )
    finally:
        asyncio.run(provider.close())

    final_output = json.loads(str(result.final_output))
    assert final_output["status"] == "needs_input"
    assert [call.call_id for call in result.tool_calls] == [
        "call_clarify_invalid",
        "call_clarify_valid",
    ]
    assert requests[0]["tool_choice"] == requests[1]["tool_choice"] == {
        "type": "function",
        "function": {"name": "ask_user"},
    }
    correction = next(
        message
        for message in requests[1]["messages"]
        if message.get("tool_call_id") == "call_clarify_invalid"
    )
    correction_payload = json.loads(correction["content"])
    assert correction_payload["retryability"] == "correct_arguments"
    assert correction_payload["errorCode"] == "pending_action_question_shape_invalid"
    assert "recommended=true on exactly one option" in correction_payload[
        "instruction"
    ]


def test_runner_rejects_plain_text_when_a_specific_tool_is_required() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return _chat_response(content="我来启动分析。")

    provider, adapter, _ = _adapter(handler)
    try:
        with pytest.raises(
            AgentSdkAdapterError,
            match="agent_required_tool_call_missing",
        ):
            asyncio.run(
                adapter.run(
                    WajeAgentRunRequest(
                        run_id="run-required-tool-missing",
                        agent_name="waje_general_agent",
                        instructions="必须调用工具。",
                        input_text="执行分析。",
                        tools=(
                            WajeAgentTool(
                                name="increment",
                                description="Increment one integer.",
                                input_model=_NumberInput,
                                handler=lambda arguments: arguments,
                            ),
                        ),
                        initial_tool_choice="increment",
                        required_tool_name="increment",
                    )
                )
            )
    finally:
        asyncio.run(provider.close())


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


def test_agent_turn_runtime_uses_real_sdk_session_for_multi_tool_loop_without_openai_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    responses = iter(
        (
            _chat_response(
                tool_name="increment",
                arguments='{"value": 1}',
                call_id="call_runtime_increment_1",
            ),
            _chat_response(
                tool_name="increment",
                arguments='{"value": 2}',
                call_id="call_runtime_increment_2",
            ),
            _chat_response(
                content=json.dumps(
                    {
                        "answerMarkdown": "连续计算结果为 3。",
                        "materialRefs": [],
                        "limitationRefs": [],
                    },
                    ensure_ascii=False,
                )
            ),
        )
    )
    requests: list[httpx.Request] = []
    tool_inputs: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return next(responses)

    def increment(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        tool_inputs.append(dict(arguments))
        return {"value": arguments["value"] + 1}

    provider, adapter, sink = _adapter(handler)
    ledger = InMemoryThreadItemLedger()
    ledger.create_thread("thread-runtime-sdk")
    runtime = AgentTurnRuntime(
        ledger=ledger,
        context_assembler=AgentContextAssembler(
            ledger=ledger,
            artifact_index=InMemoryArtifactIndex(),
        ),
        adapter=adapter,
    )
    try:
        result = asyncio.run(
            runtime.run(
                AgentTurnRequest(
                    thread_id="thread-runtime-sdk",
                    run_id="run-runtime-sdk",
                    operation_id="operation-runtime-sdk",
                    user_item_id="message-runtime-sdk",
                    user_message="从 1 连续加两次。",
                    expected_state_version=0,
                    instructions="按需使用工具后回答。",
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

    assert result.status == "completed"
    assert result.assistant_item.text == "连续计算结果为 3。"
    assert tool_inputs == [{"value": 1}, {"value": 2}]
    assert len(requests) == 3
    assert [
        item.item_type
        for item in ledger.list_items("thread-runtime-sdk")
        if item.item_type in {"tool_call", "tool_result"}
    ] == ["tool_call", "tool_result", "tool_call", "tool_result"]
    assert sink.records
    assert all(
        record.get("schema_version") == "waje-agent-trace.v1" for record in sink.records
    )


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


def test_provider_retries_blank_structured_final_output_within_shared_budget() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return _chat_response(content=" " * 128)
        return _chat_response(
            content=json.dumps(
                {"answer": "补全成功", "evidence_refs": ["evidence:retry"]},
                ensure_ascii=False,
            )
        )

    provider, adapter, _ = _adapter(
        handler,
        config=_config(max_attempts=2),
    )
    try:
        result = asyncio.run(
            adapter.run(
                WajeAgentRunRequest(
                    run_id="run-typed-output-retry",
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
        "answer": "补全成功",
        "evidence_refs": ["evidence:retry"],
    }
    assert len(requests) == 2
    assert all(
        request["response_format"] == {"type": "json_object"}
        for request in requests
    )


def test_provider_does_not_validate_intermediate_tool_call_as_final_output() -> None:
    responses = iter(
        (
            _chat_response(
                tool_name="increment",
                arguments='{"value":2}',
                call_id="call_typed_increment",
            ),
            _chat_response(
                content=json.dumps(
                    {"answer": "结果为 3", "evidence_refs": ["tool:increment"]},
                    ensure_ascii=False,
                )
            ),
        )
    )
    request_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return next(responses)

    provider, adapter, _ = _adapter(
        handler,
        config=_config(max_attempts=2),
    )
    try:
        result = asyncio.run(
            adapter.run(
                WajeAgentRunRequest(
                    run_id="run-typed-tool-output",
                    agent_name="waje_general_agent",
                    instructions="先调用工具，再按最终 schema 返回。",
                    input_text="将 2 加 1。",
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
                    output_type=_TypedAnswer,
                )
            )
        )
    finally:
        asyncio.run(provider.close())

    assert result.final_output == {
        "answer": "结果为 3",
        "evidence_refs": ["tool:increment"],
    }
    assert request_count == 2


def test_provider_maps_exhausted_structured_output_retry_to_typed_error() -> None:
    request_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return _chat_response(content="\n\t ")

    provider, adapter, _ = _adapter(
        handler,
        config=_config(max_attempts=2),
    )
    try:
        with pytest.raises(LLMProviderError) as captured:
            asyncio.run(
                adapter.run(
                    WajeAgentRunRequest(
                        run_id="run-typed-output-exhausted",
                        agent_name="waje_general_agent",
                        instructions="按最终 schema 返回。",
                        input_text="生成结构化回答。",
                        output_type=_TypedAnswer,
                    )
                )
            )
    finally:
        asyncio.run(provider.close())

    assert captured.value.kind == "provider_output_invalid"
    assert captured.value.retryability == "retryable"
    assert captured.value.error_code == "structured_output_invalid"
    assert request_count == 2


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
                            handler=lambda arguments: {"value": arguments["value"] + 1},
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


def test_postgres_circuit_preserves_failures_across_provider_instances() -> None:
    class Rows:
        def __init__(self, row: Any = None) -> None:
            self.row = row

        def fetchone(self) -> Any:
            return self.row

    class Connection:
        def __init__(self) -> None:
            self.events: list[str] = []
            self.commits = 0
            self.rollbacks = 0

        def execute(self, statement: str, params: Mapping[str, Any] | None = None):
            values = dict(params or {})
            if "mainland_provider_circuit_state" in statement:
                threshold = int(values["failure_threshold"])
                recent = list(reversed(self.events[-threshold:]))
                latest = recent[0] if recent else ""
                reached = len(recent) == threshold and all(
                    event == "mainland_provider_circuit_failure"
                    for event in recent
                )
                return Rows(
                    {
                        "latest_event_type": latest,
                        "failure_threshold_reached": reached,
                        "recovery_window_open": bool(latest),
                    }
                )
            if "INSERT INTO waje_runtime.audit_events" in statement:
                self.events.append(str(values["event_type"]))
            return Rows()

        def commit(self) -> None:
            self.commits += 1

        def rollback(self) -> None:
            self.rollbacks += 1

    connection = Connection()
    config = _config(circuit_failure_threshold=2)
    first = PostgresProviderCircuit(connection, config)
    second = PostgresProviderCircuit(connection, config)
    retryable = LLMProviderError(
        kind="provider_unavailable",
        retryability="retryable",
    )

    first.record_failure(retryable)
    second.record_failure(retryable)
    with pytest.raises(LLMProviderError) as captured:
        PostgresProviderCircuit(connection, config).before_request()

    assert captured.value.error_code == "provider_circuit_open"
    assert first.circuit_ref == second.circuit_ref
    assert connection.events == [
        "mainland_provider_circuit_failure",
        "mainland_provider_circuit_failure",
    ]
    assert connection.rollbacks == 0


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
    assert "private provider error" not in json.dumps(captured.value.provider_error)


def test_provider_maps_raw_http_transport_failures_to_typed_errors() -> None:
    timeout = llm_provider_error_from_exception(
        httpx.ReadTimeout("raw timeout detail")
    )
    unavailable = llm_provider_error_from_exception(
        httpx.ConnectError("raw host detail")
    )

    assert (timeout.kind, timeout.retryability) == (
        "provider_timeout",
        "retryable",
    )
    assert (unavailable.kind, unavailable.retryability) == (
        "provider_unavailable",
        "retryable",
    )
    assert "raw" not in str(timeout)
    assert "raw" not in str(unavailable)


def test_adapter_maps_unclassified_sdk_failure_to_stable_waje_error() -> None:
    provider, adapter, _ = _adapter(lambda _: _chat_response(content="unused"))
    try:
        with (
            patch(
                "bi_agent.runtime.agents_sdk_adapter.Runner.run",
                new=AsyncMock(
                    side_effect=RuntimeError(
                        "raw SDK detail and private provider payload"
                    )
                ),
            ),
            pytest.raises(AgentSdkAdapterError) as captured,
        ):
            asyncio.run(
                adapter.run(
                    WajeAgentRunRequest(
                        run_id="run-sdk-unclassified",
                        agent_name="waje_general_agent",
                        instructions="回答。",
                        input_text="触发未分类 SDK 故障。",
                    )
                )
            )
    finally:
        asyncio.run(provider.close())

    assert captured.value.code == "agents_sdk_runtime_failed"
    assert captured.value.retryability == "retryable"
    assert "private provider payload" not in str(captured.value)


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
        capabilities=_capabilities(
            deterministic_tool_choice_thinking="disabled",
        ),
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
    assert report.observations["origins"] == ["https://api.deepseek.com"]
    assert report.observations["paths"] == ["/v1/chat/completions"]
    assert report.observations["models"] == ["deepseek-v4-flash"]
    assert report.observations["requested_max_output_tokens"] == [512]
    assert report.checks["context_budget_enforcement"] is True
    assert report.checks["typed_error_mapping"] is True
    assert all(request["max_tokens"] == 512 for request in requests)
    tool_probe_requests = [
        request
        for request in requests
        if "WAJE_TOOL_PROBE_OK"
        in "\n".join(
            str(message.get("content") or "") for message in request["messages"]
        )
    ]
    assert tool_probe_requests
    assert all(
        request["thinking"] == {"type": "disabled"}
        for request in tool_probe_requests
    )
    assert all(
        request["thinking"] == {"type": "enabled"}
        for request in requests
        if request not in tool_probe_requests
    )
    structured_request = next(
        request
        for request in requests
        if "WAJE_STRUCTURED_PROBE_OK"
        in "\n".join(
            str(message.get("content") or "") for message in request["messages"]
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

    with pytest.raises(LLMConfigurationError, match="provider_https_required"):
        MainlandProviderConfig(
            provider="deepseek",
            base_url="http://api.deepseek.com/v1",
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


def test_deepseek_factory_resolves_explicit_v4_model_settings_for_current_config() -> (
    None
):
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
    assert config.model_settings.temperature == 0.0
    assert config.capabilities.deterministic_tool_choice_thinking == "disabled"

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


def test_sdk_node_can_disable_thinking_without_changing_provider_default() -> None:
    config = MainlandProviderConfig.deepseek_from_env(
        {
            "WAJE_LLM_PROVIDER": "current-deepseek-adapter",
            "WAJE_LLM_BASE_URL": "https://api.deepseek.com/v1",
            "WAJE_LLM_MODEL": "deepseek-v4-flash",
            "WAJE_LLM_API_KEY": "deepseek-key",
        }
    )
    provider = MainlandModelProvider(
        config,
        http_transport=httpx.MockTransport(
            lambda _: _chat_response(content='{"ok":true}')
        ),
    )
    try:
        disabled = provider.sdk_model_settings(
            structured_output=True,
            thinking="disabled",
        )
        defaulted = provider.sdk_model_settings(structured_output=True)
    finally:
        asyncio.run(provider.close())

    assert disabled.extra_body["thinking"] == {"type": "disabled"}
    assert defaulted.extra_body["thinking"] == {"type": "enabled"}
    assert config.model_settings.thinking == "enabled"


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


def test_all_production_model_transports_use_mainland_provider_configuration() -> None:
    runtime_root = ROOT / "bi_agent"
    tool_root = ROOT / "tools"
    provider_path = runtime_root / "runtime/mainland_model_provider.py"
    structured_transport_path = runtime_root / "runtime/llm_client.py"
    production_paths = tuple(runtime_root.rglob("*.py")) + tuple(tool_root.rglob("*.py"))

    environment_keys = (
        "WAJE_LLM_PROVIDER",
        "WAJE_LLM_BASE_URL",
        "WAJE_LLM_API_KEY",
        "WAJE_LLM_MODEL",
        "WAJE_LLM_CRITICAL_MODEL",
    )
    environment_readers = [
        str(path.relative_to(ROOT))
        for path in production_paths
        if path != provider_path
        and any(key in path.read_text(encoding="utf-8") for key in environment_keys)
    ]
    assert environment_readers == []

    direct_structured_clients = [
        str(path.relative_to(ROOT))
        for path in production_paths
        if path != provider_path
        and "OpenAICompatibleLLMClient(" in path.read_text(encoding="utf-8")
    ]
    assert direct_structured_clients == []

    openai_client_constructors = [
        str(path.relative_to(ROOT))
        for path in production_paths
        if "OpenAI(" in path.read_text(encoding="utf-8")
        or "AsyncOpenAI(" in path.read_text(encoding="utf-8")
    ]
    assert sorted(openai_client_constructors) == sorted(
        [
            str(provider_path.relative_to(ROOT)),
            str(structured_transport_path.relative_to(ROOT)),
        ]
    )

    assert "OpenAICompatibleLLMClient.from_env" not in "\n".join(
        path.read_text(encoding="utf-8") for path in production_paths
    )
