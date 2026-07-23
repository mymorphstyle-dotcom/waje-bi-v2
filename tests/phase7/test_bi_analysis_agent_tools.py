from __future__ import annotations

import asyncio
import json
from typing import Any, Mapping, Sequence

import httpx
from pydantic import ValidationError
import pytest

from bi_agent.runtime.agent_context import AgentContextAssembler, InMemoryArtifactIndex
from bi_agent.runtime.agent_sdk_contracts import WajeAgentRunRequest
from bi_agent.runtime.agent_turn_runtime import AgentTurnRequest, AgentTurnRuntime
from bi_agent.runtime.agents_sdk_adapter import WajeAgentsSdkAdapter
from bi_agent.runtime.agents_sdk_trace import InMemoryAgentTraceSink
from bi_agent.runtime.bi_analysis_tools import (
    BiAnalysisTaskSubmission,
    BiAnalysisToolError,
    ContinueBiAnalysisInput,
    PostgresBiAnalysisTaskGateway,
    bi_analysis_tools,
)
from bi_agent.runtime.mainland_model_provider import (
    MainlandModelCapabilities,
    MainlandModelProvider,
    MainlandModelSettings,
    MainlandProviderConfig,
)
from bi_agent.runtime.thread_item_ledger import InMemoryThreadItemLedger
from tools.runtime.recover_run_dispatches import _validated_agent_core_command


class RecordingGateway:
    def __init__(self) -> None:
        self.starts: list[dict[str, Any]] = []
        self.continuations: list[dict[str, Any]] = []
        self.error: Exception | None = None

    def start_analysis(self, **kwargs: Any) -> BiAnalysisTaskSubmission:
        if self.error is not None:
            raise self.error
        self.starts.append(dict(kwargs))
        return BiAnalysisTaskSubmission(
            task_ref="run-bi-start",
            task_state="queued",
            replayed=False,
        )

    def continue_analysis(self, **kwargs: Any) -> BiAnalysisTaskSubmission:
        if self.error is not None:
            raise self.error
        self.continuations.append(dict(kwargs))
        return BiAnalysisTaskSubmission(
            task_ref="run-bi-continue",
            task_state="queued",
            replayed=False,
            source_task_ref=str(kwargs["source_task_ref"]),
        )


class RecordingEventSink:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.results: list[dict[str, Any]] = []

    async def record_tool_call(self, **kwargs: Any) -> None:
        self.calls.append(dict(kwargs))

    async def record_tool_result(self, **kwargs: Any) -> None:
        self.results.append(dict(kwargs))


class Rows:
    def __init__(self, rows: Sequence[Any] = ()) -> None:
        self.rows = list(rows)

    def fetchone(self) -> Any | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[Any]:
        return list(self.rows)


class BiAnalysisConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.parameters: list[dict[str, Any]] = []
        self.commits = 0
        self.rollbacks = 0
        self.thread_id = "thread-bi"
        self.messages = {
            "message-bi": {
                "message_id": "message-bi",
                "thread_id": self.thread_id,
                "item_type": "user_message",
                "operation_key": "user:operation-bi",
                "role": "user",
            }
        }
        self.runs: dict[str, dict[str, Any]] = {}
        self.dispatches: dict[str, dict[str, Any]] = {}
        self.source_rows: list[dict[str, Any]] = [
            {
                "run_id": "run-published",
                "thread_id": self.thread_id,
                "status": "completed",
                "intent_revision_id": "intent-published",
                "transition_id": "transition-published",
            }
        ]

    def execute(
        self,
        statement: str,
        params: Mapping[str, Any] | None = None,
    ) -> Rows:
        normalized = " ".join(statement.split())
        values = dict(params or {})
        self.statements.append(normalized)
        self.parameters.append(values)
        if normalized == "BEGIN" or "pg_advisory_xact_lock" in normalized:
            return Rows()
        if "bi_analysis_tool_source_publication" in normalized:
            source_ref = str(values["source_task_ref"])
            return Rows(
                [row for row in self.source_rows if row["run_id"] == source_ref]
            )
        if "bi_analysis_tool_dispatch_replay" in normalized:
            existing = self.dispatches.get(str(values["request_identity"]))
            return Rows([existing] if existing else [])
        if "FROM waje_runtime.investigation_threads" in normalized:
            return Rows(
                [{"thread_id": self.thread_id}]
                if values.get("thread_id") == self.thread_id
                else []
            )
        if "bi_analysis_tool_source_message" in normalized:
            message = self.messages.get(str(values["source_message_id"]))
            return Rows([message] if message else [])
        if "INSERT INTO waje_runtime.analysis_runs" in normalized:
            self.runs[str(values["task_ref"])] = {
                "run_id": values["task_ref"],
                "thread_id": values["thread_id"],
                "status": "queued",
            }
            return Rows()
        if "INSERT INTO waje_runtime.run_dispatches" in normalized:
            payload = json.loads(str(values["request_payload"]))
            self.dispatches[str(values["request_identity"])] = {
                "run_id": values["task_ref"],
                "thread_id": values["thread_id"],
                "message_id": values["source_message_id"],
                "request_digest": values["request_digest"],
                "request_payload": payload,
                "status": "queued",
                "dispatch_id": values["dispatch_ref"],
            }
            return Rows()
        if (
            "UPDATE waje_runtime.investigation_threads" in normalized
            or "INSERT INTO waje_runtime.audit_events" in normalized
        ):
            return Rows()
        raise AssertionError(normalized)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _capabilities() -> MainlandModelCapabilities:
    return MainlandModelCapabilities(
        text_generation=True,
        function_calling=True,
        structured_output=True,
        streaming_text=True,
        streaming_tool_calls=True,
        typed_error_mapping=True,
        context_window_tokens=64_000,
        max_output_tokens=2_048,
        thinking=False,
    )


def _chat_response(
    *,
    content: str | None = None,
    tool_name: str = "",
    arguments: str = "",
    call_id: str = "",
) -> httpx.Response:
    message: dict[str, Any] = {"role": "assistant", "content": content}
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
            "id": "chatcmpl-bi-tool",
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
                "prompt_tokens": 3,
                "completion_tokens": 2,
                "total_tokens": 5,
            },
        },
    )


def test_run_tool_preserves_open_business_text_and_returns_shared_contract() -> None:
    gateway = RecordingGateway()
    run_tool, _ = bi_analysis_tools(
        gateway=gateway,
        thread_id="thread-bi",
        source_message_id="message-bi",
        operation_id="operation-bi",
    )
    question = "请判断新客付费金额变化，并说明证据边界。"

    result = run_tool.handler({"businessQuestion": question})

    assert gateway.starts == [
        {
            "thread_id": "thread-bi",
            "source_message_id": "message-bi",
            "operation_id": "operation-bi",
            "business_question": question,
        }
    ]
    assert result.model_dump(mode="json", by_alias=True) == {
        "status": "succeeded",
        "output": {
            "operation": "run_bi_analysis",
            "taskRef": "run-bi-start",
            "taskState": "queued",
            "sourceTaskRef": None,
            "replayed": False,
        },
        "artifactRefs": [],
        "materialRefs": [],
        "limitationRefs": [],
        "retryability": "never",
        "customerSummary": "BI 分析任务已进入持久化执行队列。",
        "technicalDetailRef": None,
    }


def test_continue_tool_requires_explicit_current_plan_revision_fields() -> None:
    schema = ContinueBiAnalysisInput.model_json_schema(by_alias=True)
    assert schema["properties"]["supersededPlanFields"]["items"]["enum"] == [
        "goal_bindings",
        "desired_decisions",
        "analysis_axes",
        "target_metric_refs",
        "baseline_refs",
        "resolved_window_refs",
        "time_spec",
        "scope",
        "filters",
        "direction_premise",
    ]
    with pytest.raises(ValidationError):
        ContinueBiAnalysisInput.model_validate(
            {
                "sourceTaskRef": "run-published",
                "revisionRequest": "把窗口改成最近七个完整自然日。",
                "supersededPlanFields": ["provider_guess"],
            }
        )

    gateway = RecordingGateway()
    _, continuation = bi_analysis_tools(
        gateway=gateway,
        thread_id="thread-bi",
        source_message_id="message-bi",
        operation_id="operation-bi",
    )
    result = continuation.handler(
        {
            "sourceTaskRef": "run-published",
            "revisionRequest": "把窗口改成最近七个完整自然日。",
            "supersededPlanFields": [
                "time_spec",
                "resolved_window_refs",
            ],
        }
    )

    assert result.status == "succeeded"
    assert gateway.continuations[0]["revision_request"] == (
        "把窗口改成最近七个完整自然日。"
    )
    assert gateway.continuations[0]["superseded_plan_fields"] == [
        "time_spec",
        "resolved_window_refs",
    ]


def test_tool_maps_submission_failure_without_exposing_backend_error() -> None:
    gateway = RecordingGateway()
    gateway.error = BiAnalysisToolError(
        "private_database_connection_details",
        retryability="same_input",
    )
    run_tool, _ = bi_analysis_tools(
        gateway=gateway,
        thread_id="thread-bi",
        source_message_id="message-bi",
        operation_id="operation-bi",
    )

    result = run_tool.handler({"businessQuestion": "分析付费金额变化。"})
    serialized = result.model_dump(mode="json", by_alias=True)

    assert result.status == "failed"
    assert result.retryability == "same_input"
    assert result.output is None
    assert "private_database" not in json.dumps(serialized, ensure_ascii=False)


def test_postgres_gateway_queues_existing_recoverable_workflow_without_new_history() -> (
    None
):
    connection = BiAnalysisConnection()
    gateway = PostgresBiAnalysisTaskGateway(connection)

    submission = gateway.start_analysis(
        thread_id="thread-bi",
        source_message_id="message-bi",
        operation_id="operation-bi",
        business_question="分析付费金额变化并说明证据强度。",
    )

    dispatch = next(iter(connection.dispatches.values()))
    assert submission.task_ref == dispatch["run_id"]
    assert submission.task_state == "queued"
    assert submission.replayed is False
    assert dispatch["message_id"] == "message-bi"
    assert dispatch["request_payload"] == {
        "message": "分析付费金额变化并说明证据强度。"
    }
    assert (
        _validated_agent_core_command(
            dispatch["request_payload"],
            producer_kind="thread_message",
            run_id=submission.task_ref,
        )
        == dispatch["request_payload"]
    )
    assert connection.commits == 1
    audit_statement = next(
        statement
        for statement in connection.statements
        if "bi_analysis_tool_queued" in statement
    )
    for parameter in ("tool_name", "dispatch_ref", "source_task_ref"):
        assert f"%({parameter})s::text" in audit_statement
    assert not any(
        "INSERT INTO waje_runtime.conversation_messages" in statement
        for statement in connection.statements
    )
    assert not any(
        authority_table in statement
        for statement in connection.statements
        for authority_table in (
            "INSERT INTO waje_runtime.intent_revisions",
            "INSERT INTO waje_runtime.plan_revisions",
            "INSERT INTO waje_runtime.capability_evidence_ledger_entries",
            "INSERT INTO waje_runtime.publication_customer_payloads",
        )
    )


def test_postgres_gateway_replays_same_operation_and_rejects_changed_payload() -> None:
    connection = BiAnalysisConnection()
    gateway = PostgresBiAnalysisTaskGateway(connection)
    kwargs = {
        "thread_id": "thread-bi",
        "source_message_id": "message-bi",
        "operation_id": "operation-bi",
        "business_question": "分析付费金额变化。",
    }

    first = gateway.start_analysis(**kwargs)
    replay = gateway.start_analysis(**kwargs)

    assert replay.task_ref == first.task_ref
    assert replay.replayed is True
    assert len(connection.runs) == 1
    assert len(connection.dispatches) == 1

    with pytest.raises(
        BiAnalysisToolError,
        match="^bi_analysis_tool_replay_conflict$",
    ):
        gateway.start_analysis(**{**kwargs, "business_question": "改成另一项分析。"})
    assert connection.rollbacks == 1


def test_task_identity_is_scoped_by_thread_even_when_operation_ids_match() -> None:
    first_connection = BiAnalysisConnection()
    second_connection = BiAnalysisConnection()
    second_connection.thread_id = "thread-other"
    second_connection.messages = {
        "message-other": {
            "message_id": "message-other",
            "thread_id": "thread-other",
            "item_type": "user_message",
            "operation_key": "user:operation-bi",
            "role": "user",
        }
    }

    first = PostgresBiAnalysisTaskGateway(first_connection).start_analysis(
        thread_id="thread-bi",
        source_message_id="message-bi",
        operation_id="operation-bi",
        business_question="分析付费金额变化。",
    )
    second = PostgresBiAnalysisTaskGateway(second_connection).start_analysis(
        thread_id="thread-other",
        source_message_id="message-other",
        operation_id="operation-bi",
        business_question="分析付费金额变化。",
    )

    assert first.task_ref != second.task_ref


def test_continue_gateway_binds_published_source_intent_and_parent_transition() -> None:
    connection = BiAnalysisConnection()
    connection.messages["message-bi"]["operation_key"] = "user:operation-continue"
    gateway = PostgresBiAnalysisTaskGateway(connection)

    submission = gateway.continue_analysis(
        thread_id="thread-bi",
        source_message_id="message-bi",
        operation_id="operation-continue",
        source_task_ref="run-published",
        revision_request="把窗口改成最近七个完整自然日。",
        superseded_plan_fields=("time_spec", "resolved_window_refs"),
    )

    dispatch = next(iter(connection.dispatches.values()))
    context = dispatch["request_payload"]["intentRevisionContext"]
    assert submission.source_task_ref == "run-published"
    assert context == {
        "supersedes_intent_revision_id": "intent-published",
        "superseded_plan_fields": ["time_spec", "resolved_window_refs"],
        "intent_revision_reason_ref": context["intent_revision_reason_ref"],
        "parent_transition_id": "transition-published",
    }
    assert context["intent_revision_reason_ref"].startswith(
        "agent-tool-revision:sha256:"
    )
    assert (
        _validated_agent_core_command(
            dispatch["request_payload"],
            producer_kind="thread_message",
            run_id=submission.task_ref,
        )
        == dispatch["request_payload"]
    )
    assert connection.commits == 1


def test_continue_gateway_fails_when_source_has_no_customer_publication() -> None:
    connection = BiAnalysisConnection()
    connection.source_rows = []
    gateway = PostgresBiAnalysisTaskGateway(connection)

    with pytest.raises(
        BiAnalysisToolError,
        match="^bi_analysis_published_source_missing$",
    ):
        gateway.continue_analysis(
            thread_id="thread-bi",
            source_message_id="message-bi",
            operation_id="operation-bi",
            source_task_ref="run-unpublished",
            revision_request="调整分析窗口。",
            superseded_plan_fields=("time_spec",),
        )

    assert connection.dispatches == {}
    assert connection.rollbacks == 1


def test_sdk_runner_calls_bi_tool_through_mainland_chat_completions_without_openai_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    response = _chat_response(
        tool_name="run_bi_analysis",
        arguments=json.dumps(
            {"businessQuestion": "分析付费金额变化。"},
            ensure_ascii=False,
        ),
        call_id="call-run-bi",
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response

    provider = MainlandModelProvider(
        MainlandProviderConfig(
            provider="test-mainland",
            base_url="https://model.provider.example.cn/v1",
            api_key="mainland-key",
            model="mainland-model",
            model_settings=MainlandModelSettings(
                max_output_tokens=512,
                thinking="disabled",
            ),
            capabilities=_capabilities(),
            max_attempts=1,
        ),
        http_transport=httpx.MockTransport(handler),
    )
    gateway = RecordingGateway()
    tools = bi_analysis_tools(
        gateway=gateway,
        thread_id="thread-bi",
        source_message_id="message-bi",
        operation_id="operation-bi",
    )
    adapter = WajeAgentsSdkAdapter(
        provider=provider,
        trace_sink=InMemoryAgentTraceSink(),
    )
    try:
        result = asyncio.run(
            adapter.run(
                WajeAgentRunRequest(
                    run_id="agent-run-bi",
                    agent_name="waje_general_agent",
                    instructions="需要新分析时调用持久化 BI 工具。",
                    input_text="分析付费金额变化。",
                    tools=tools,
                    max_turns=4,
                )
            )
        )
    finally:
        asyncio.run(provider.close())

    assert json.loads(str(result.final_output))["output"]["taskRef"] == "run-bi-start"
    assert result.model_turns == 1
    assert gateway.starts[0]["business_question"] == "分析付费金额变化。"
    assert [request.url.path for request in requests] == ["/v1/chat/completions"]
    assert {request.url.host for request in requests} == {"model.provider.example.cn"}
    assert json.loads(requests[0].content)["parallel_tool_calls"] is False
    assert all(request.url.host != "api.openai.com" for request in requests)


def test_agent_turn_runtime_suspends_real_sdk_bi_tool_on_mainland_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _chat_response(
            tool_name="run_bi_analysis",
            arguments='{"businessQuestion":"分析付费金额变化。"}',
            call_id="call-runtime-run-bi",
        )

    provider = MainlandModelProvider(
        MainlandProviderConfig(
            provider="test-mainland",
            base_url="https://model.provider.example.cn/v1",
            api_key="mainland-key",
            model="mainland-model",
            model_settings=MainlandModelSettings(512, "disabled"),
            capabilities=_capabilities(),
            max_attempts=1,
        ),
        http_transport=httpx.MockTransport(handler),
    )
    ledger = InMemoryThreadItemLedger()
    ledger.create_thread("thread-runtime-bi")
    gateway = RecordingGateway()
    runtime = AgentTurnRuntime(
        ledger=ledger,
        context_assembler=AgentContextAssembler(
            ledger=ledger,
            artifact_index=InMemoryArtifactIndex(),
        ),
        adapter=WajeAgentsSdkAdapter(
            provider=provider,
            trace_sink=InMemoryAgentTraceSink(),
        ),
    )
    request = AgentTurnRequest(
        thread_id="thread-runtime-bi",
        run_id="agent-run-runtime-bi",
        operation_id="operation-runtime-bi",
        user_item_id="message-runtime-bi",
        user_message="分析付费金额变化。",
        expected_state_version=0,
        instructions="需要新分析时调用持久化 BI 工具。",
        tools=bi_analysis_tools(
            gateway=gateway,
            thread_id="thread-runtime-bi",
            source_message_id="message-runtime-bi",
            operation_id="operation-runtime-bi",
        ),
        max_turns=4,
    )
    try:
        result = asyncio.run(runtime.run(request))
        replay = asyncio.run(runtime.run(request))
    finally:
        asyncio.run(provider.close())

    assert result.status == "working"
    assert result.terminal_item is None
    assert result.checkpoint_item is not None
    assert result.thread_head.active_task_id == "run-bi-start"
    assert result.assistant_item.item_type == "progress"
    assert replay.replayed is True
    assert len(requests) == 1
    assert requests[0].url.path == "/v1/chat/completions"
    assert requests[0].url.host == "model.provider.example.cn"
    assert requests[0].url.host != "api.openai.com"
    assert json.loads(requests[0].content)["parallel_tool_calls"] is False
    assert "run-bi-start" not in json.dumps(
        result.customer_projection(),
        ensure_ascii=False,
    )


def test_sdk_audit_marks_typed_failed_tool_result_as_failed() -> None:
    responses = iter(
        (
            _chat_response(
                tool_name="run_bi_analysis",
                arguments='{"businessQuestion":"分析付费金额变化。"}',
                call_id="call-run-bi-failed",
            ),
            _chat_response(content="任务提交失败，请稍后重试。"),
        )
    )
    provider = MainlandModelProvider(
        MainlandProviderConfig(
            provider="test-mainland",
            base_url="https://model.provider.example.cn/v1",
            api_key="mainland-key",
            model="mainland-model",
            model_settings=MainlandModelSettings(512, "disabled"),
            capabilities=_capabilities(),
            max_attempts=1,
        ),
        http_transport=httpx.MockTransport(lambda _: next(responses)),
    )
    gateway = RecordingGateway()
    gateway.error = BiAnalysisToolError(
        "bi_analysis_task_submission_failed",
        retryability="same_input",
    )
    sink = RecordingEventSink()
    adapter = WajeAgentsSdkAdapter(
        provider=provider,
        trace_sink=InMemoryAgentTraceSink(),
    )
    try:
        asyncio.run(
            adapter.run(
                WajeAgentRunRequest(
                    run_id="agent-run-bi-failed",
                    agent_name="waje_general_agent",
                    instructions="需要分析时调用工具。",
                    input_text="分析付费金额变化。",
                    tools=bi_analysis_tools(
                        gateway=gateway,
                        thread_id="thread-bi",
                        source_message_id="message-bi",
                        operation_id="operation-bi",
                    ),
                    event_sink=sink,
                )
            )
        )
    finally:
        asyncio.run(provider.close())

    assert sink.calls[0]["tool_name"] == "run_bi_analysis"
    assert sink.results[0]["succeeded"] is False
    assert sink.results[0]["result"]["status"] == "failed"
