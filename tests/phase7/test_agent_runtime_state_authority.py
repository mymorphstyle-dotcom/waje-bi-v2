from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
from typing import Any, Mapping

import pytest
from pydantic import BaseModel, ConfigDict

from bi_agent.runtime.agent_context import (
    AgentContextAssembler,
    ArtifactDescriptor,
    InMemoryArtifactIndex,
)
from bi_agent.runtime.agent_sdk_contracts import (
    AgentSdkAdapterError,
    AgentToolResult,
    AgentSessionError,
    WajeAgentRunRequest,
    WajeAgentRunResult,
    WajeAgentTool,
)
from bi_agent.runtime.agent_tool_discovery import AgentTurnActionBinding
from bi_agent.runtime.agent_turn_runtime import (
    AgentTurnError,
    AgentTurnRequest,
    AgentTurnRuntime,
)
from bi_agent.runtime.llm_client import LLMProviderError
from bi_agent.runtime.postgres_agent_session import PostgresAgentSession
from bi_agent.runtime.thread_item_ledger import (
    InMemoryThreadItemLedger,
    NewThreadItem,
    PostgresThreadItemLedger,
    ThreadHeadTarget,
    ThreadLedgerError,
    ThreadStateVersionConflict,
)


ROOT = Path(__file__).resolve().parents[2]


class FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def fetchone(self) -> Any:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[Any]:
        return list(self.rows)


class ThreadLedgerConnection:
    def __init__(self) -> None:
        self.statements: list[tuple[str, Mapping[str, Any]]] = []
        self.commits = 0
        self.rollbacks = 0
        self.operation_lease_available = True

    def execute(
        self,
        statement: str,
        params: Mapping[str, Any],
    ) -> FakeResult:
        self.statements.append((statement, dict(params)))
        if "pg_try_advisory_lock" in statement:
            return FakeResult([(self.operation_lease_available,)])
        if "pg_advisory_unlock" in statement:
            return FakeResult([(True,)])
        if "FOR UPDATE" in statement:
            return FakeResult([("thread-pg", 4, None, "topic-pg", None, 7, "idle")])
        if "operation_key = ANY" in statement:
            return FakeResult([])
        if (
            "FROM waje_runtime.conversation_messages" in statement
            and "ORDER BY item_sequence DESC" in statement
        ):
            return FakeResult([])
        if "INSERT INTO waje_runtime.conversation_messages" in statement:
            return FakeResult(
                [
                    (
                        params["item_id"],
                        params["thread_id"],
                        params["item_sequence"],
                        params["item_type"],
                        params["role"],
                        params["text"],
                        params["operation_key"],
                        params["item_digest"],
                        params["customer_visible"],
                        json.loads(str(params["payload"])),
                        params["turn_id"],
                        "2026-07-21T00:00:00+00:00",
                    )
                ]
            )
        if "UPDATE waje_runtime.investigation_threads" in statement:
            return FakeResult(
                [
                    (
                        params["thread_id"],
                        5,
                        params["active_task_id"],
                        params["active_topic_ref"],
                        params["pending_action_ref"],
                        params["latest_item_sequence"],
                        params["customer_state"],
                    )
                ]
            )
        raise AssertionError(statement)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class SessionWritingAdapter:
    def __init__(
        self,
        final_output: Mapping[str, Any],
        *,
        tool_events: tuple[tuple[Any, ...], ...] = (),
        error: Exception | None = None,
        post_tool_error: Exception | None = None,
    ) -> None:
        self.final_output = dict(final_output)
        self.tool_events = tool_events
        self.error = error
        self.post_tool_error = post_tool_error
        self.calls: list[WajeAgentRunRequest] = []
        self.histories: list[list[dict[str, Any]]] = []

    async def run(self, request: WajeAgentRunRequest) -> WajeAgentRunResult:
        self.calls.append(request)
        assert request.session is not None
        assert request.event_sink is not None
        self.histories.append(await request.session.get_items())
        if self.error is not None:
            raise self.error
        persisted_sdk_items: list[dict[str, Any]] = [
            {"role": "user", "content": request.input_text}
        ]
        for event in self.tool_events:
            if len(event) == 4:
                tool_name, call_id, arguments, result = event
                succeeded = True
            elif len(event) == 5:
                tool_name, call_id, arguments, result, succeeded = event
            else:
                raise AssertionError("tool_event_shape_invalid")
            await request.event_sink.record_tool_call(
                tool_name=tool_name,
                call_id=call_id,
                arguments=arguments,
            )
            await request.event_sink.record_tool_result(
                tool_name=tool_name,
                call_id=call_id,
                result=result,
                succeeded=succeeded,
            )
            persisted_sdk_items.extend(
                [
                    {
                        "type": "function_call",
                        "name": tool_name,
                        "call_id": call_id,
                        "arguments": json.dumps(arguments),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps(result),
                    },
                ]
            )
        if self.post_tool_error is not None:
            raise self.post_tool_error
        persisted_sdk_items.append(
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(self.final_output, ensure_ascii=False),
                    }
                ],
            }
        )
        await request.session.add_items(persisted_sdk_items)
        return WajeAgentRunResult(
            run_id=request.run_id,
            final_output=self.final_output,
            usage={"input_tokens": 10, "output_tokens": 5},
            model_turns=max(1, len(self.tool_events) + 1),
        )


def _runtime(
    adapter: SessionWritingAdapter,
    *,
    artifacts: InMemoryArtifactIndex | None = None,
) -> tuple[InMemoryThreadItemLedger, AgentTurnRuntime]:
    ledger = InMemoryThreadItemLedger()
    ledger.create_thread("thread-runtime")
    index = artifacts or InMemoryArtifactIndex()
    assembler = AgentContextAssembler(ledger=ledger, artifact_index=index)
    return ledger, AgentTurnRuntime(
        ledger=ledger,
        context_assembler=assembler,
        adapter=adapter,
    )


def _request(*, operation_id: str = "operation-1") -> AgentTurnRequest:
    return AgentTurnRequest(
        thread_id="thread-runtime",
        run_id="run-runtime",
        operation_id=operation_id,
        user_item_id=f"message-{operation_id}",
        user_message="继续解释这个结果。",
        expected_state_version=0,
        instructions="依据持久化材料回答。",
    )


class _NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


def test_thread_item_ledger_is_atomic_versioned_and_idempotent() -> None:
    ledger = InMemoryThreadItemLedger()
    head = ledger.create_thread("thread-ledger")
    item = NewThreadItem(
        item_id="message-1",
        item_type="user_message",
        role="user",
        text="第一条消息",
        operation_key="user:operation-1",
        customer_visible=True,
        payload={"sdk_item": {"role": "user", "content": "第一条消息"}},
    )
    first = ledger.append_items(
        "thread-ledger",
        [item],
        expected_state_version=head.state_version,
        head_target=ThreadHeadTarget(
            active_task_id="run-1",
            active_topic_ref=None,
            pending_action_ref=None,
            customer_state="working",
        ),
    )

    assert first.head.state_version == 1
    assert first.head.latest_item_sequence == 1
    assert first.items[0].sequence == 1
    replay = ledger.append_items(
        "thread-ledger",
        [item],
        expected_state_version=0,
    )
    assert replay.replayed is True
    assert replay.head.state_version == 1

    with pytest.raises(ThreadLedgerError, match="thread_item_replay_conflict"):
        ledger.append_items(
            "thread-ledger",
            [
                NewThreadItem(
                    item_id="message-1",
                    item_type="user_message",
                    role="user",
                    text="冲突内容",
                    operation_key="user:operation-1",
                    customer_visible=True,
                )
            ],
        )
    with pytest.raises(ThreadStateVersionConflict):
        ledger.append_items(
            "thread-ledger",
            [
                NewThreadItem(
                    item_id="message-2",
                    item_type="user_message",
                    role="user",
                    text="第二条消息",
                    operation_key="user:operation-2",
                    customer_visible=True,
                )
            ],
            expected_state_version=0,
        )


def test_postgres_thread_item_ledger_locks_head_and_commits_item_with_cas() -> None:
    connection = ThreadLedgerConnection()
    ledger = PostgresThreadItemLedger(connection)

    result = ledger.append_items(
        "thread-pg",
        [
            NewThreadItem(
                item_id="message-pg",
                item_type="user_message",
                role="user",
                text="持久化消息",
                operation_key="user:pg-operation",
                customer_visible=True,
            )
        ],
        expected_state_version=4,
        head_target=ThreadHeadTarget(
            active_task_id="run-pg",
            active_topic_ref="topic-pg",
            pending_action_ref=None,
            customer_state="working",
        ),
    )

    assert result.items[0].sequence == 8
    assert result.head.state_version == 5
    assert result.head.customer_state == "working"
    assert connection.commits == 1
    assert connection.rollbacks == 0
    sql = "\n".join(statement for statement, _ in connection.statements)
    assert sql.index("FOR UPDATE") < sql.index(
        "INSERT INTO waje_runtime.conversation_messages"
    )
    assert sql.index("INSERT INTO waje_runtime.conversation_messages") < sql.index(
        "UPDATE waje_runtime.investigation_threads"
    )


def test_postgres_thread_item_ledger_types_nullable_replay_boundaries() -> None:
    connection = ThreadLedgerConnection()
    ledger = PostgresThreadItemLedger(connection)

    ledger.list_items(
        "thread-pg",
        limit=40,
        after_sequence=None,
        through_sequence=None,
    )

    statement, params = connection.statements[-1]
    assert "%(after_sequence)s::bigint IS NULL" in statement
    assert "item_sequence > %(after_sequence)s::bigint" in statement
    assert "%(through_sequence)s::bigint IS NULL" in statement
    assert "item_sequence <= %(through_sequence)s::bigint" in statement
    assert "LIMIT %(limit)s::integer" in statement
    assert params == {
        "thread_id": "thread-pg",
        "after_sequence": None,
        "through_sequence": None,
        "limit": 40,
    }


def test_postgres_thread_item_ledger_owns_operation_with_session_advisory_lease() -> (
    None
):
    connection = ThreadLedgerConnection()
    ledger = PostgresThreadItemLedger(connection)

    assert ledger.try_acquire_operation_lease("thread-pg", "operation-pg") is True
    ledger.release_operation_lease("thread-pg", "operation-pg")

    sql = "\n".join(statement for statement, _ in connection.statements)
    assert "pg_try_advisory_lock" in sql
    assert "pg_advisory_unlock" in sql
    assert connection.commits == 2


def test_same_operation_has_one_model_tool_loop_and_replays_after_release() -> None:
    async def scenario() -> tuple[int, str, bool]:
        ledger = InMemoryThreadItemLedger()
        ledger.create_thread("thread-runtime")
        started = asyncio.Event()
        release = asyncio.Event()

        class BlockingAdapter:
            def __init__(self) -> None:
                self.calls = 0

            async def run(self, request: WajeAgentRunRequest) -> WajeAgentRunResult:
                self.calls += 1
                started.set()
                await release.wait()
                return WajeAgentRunResult(
                    run_id=request.run_id,
                    final_output={
                        "answerMarkdown": "只执行一次。",
                        "materialRefs": [],
                        "limitationRefs": [],
                    },
                    usage={"input_tokens": 1, "output_tokens": 1},
                    model_turns=1,
                )

        adapter = BlockingAdapter()

        def runtime() -> AgentTurnRuntime:
            return AgentTurnRuntime(
                ledger=ledger,
                context_assembler=AgentContextAssembler(
                    ledger=ledger,
                    artifact_index=InMemoryArtifactIndex(),
                ),
                adapter=adapter,
            )

        request = _request()
        first = asyncio.create_task(runtime().run(request))
        await started.wait()
        with pytest.raises(AgentTurnError, match="agent_turn_operation_in_progress"):
            await runtime().run(request)
        release.set()
        completed = await first
        replayed = await runtime().run(request)
        return adapter.calls, completed.status, replayed.replayed

    calls, status, replayed = asyncio.run(scenario())

    assert calls == 1
    assert status == "completed"
    assert replayed is True


def test_postgres_agent_session_replays_ledger_and_rejects_history_mutation() -> None:
    ledger = InMemoryThreadItemLedger()
    ledger.create_thread("thread-session")
    first = ledger.append_items(
        "thread-session",
        [
            NewThreadItem(
                item_id="message-old-user",
                item_type="user_message",
                role="user",
                text="旧问题",
                operation_key="user:old",
                customer_visible=True,
                payload={
                    "sdk_item": {"role": "user", "content": "旧问题"},
                    "sdk_replay": True,
                },
            ),
            NewThreadItem(
                item_id="tool-call-old",
                item_type="tool_call",
                role="tool",
                text="",
                operation_key="tool-call:old:call-1",
                customer_visible=False,
                payload={
                    "sdk_item": {
                        "type": "function_call",
                        "name": "inspect_analysis_artifact",
                        "call_id": "call-1",
                        "arguments": "{}",
                    },
                    "sdk_replay": True,
                },
            ),
            NewThreadItem(
                item_id="tool-result-old",
                item_type="tool_result",
                role="tool",
                text="",
                operation_key="tool-result:old:call-1",
                customer_visible=False,
                payload={
                    "sdk_item": {
                        "type": "function_call_output",
                        "name": "inspect_analysis_artifact",
                        "call_id": "call-1",
                        "output": "x" * 100_000,
                    },
                    "sdk_replay": True,
                },
            ),
            NewThreadItem(
                item_id="message-old-model-assistant",
                item_type="assistant_message",
                role="assistant",
                text="旧回答",
                operation_key="agent:old:model-assistant",
                customer_visible=False,
                payload={
                    "sdk_item": {"role": "assistant", "content": "旧回答"},
                    "sdk_replay": True,
                },
            ),
            NewThreadItem(
                item_id="message-old-assistant",
                item_type="assistant_message",
                role="assistant",
                text="旧回答",
                operation_key="assistant:old",
                customer_visible=True,
                payload={"sdk_replay": False},
            ),
        ],
    )
    current = ledger.append_items(
        "thread-session",
        [
            NewThreadItem(
                item_id="message-current",
                item_type="user_message",
                role="user",
                text="当前问题",
                operation_key="user:current",
                customer_visible=True,
            )
        ],
    ).items[0]
    session = PostgresAgentSession(
        ledger=ledger,
        thread_id="thread-session",
        operation_id="current",
        input_item_id=current.item_id,
        input_text=current.text,
        replay_through_sequence=current.sequence - 1,
    )

    assert asyncio.run(session.get_items()) == [
        {"role": "user", "content": "旧问题"},
        {"role": "assistant", "content": "旧回答"},
    ]
    asyncio.run(
        session.record_tool_call(
            tool_name="inspect_artifact",
            call_id="call-1",
            arguments={"artifact_ref": "artifact-1"},
        )
    )
    asyncio.run(
        session.record_tool_result(
            tool_name="inspect_artifact",
            call_id="call-1",
            result={"artifactRefs": ["artifact-1"]},
            succeeded=True,
        )
    )
    items = ledger.list_items("thread-session")
    assert [item.item_type for item in items[-2:]] == ["tool_call", "tool_result"]
    assert first.head.latest_item_sequence == 5
    with pytest.raises(RuntimeError, match="agent_session_append_only"):
        asyncio.run(session.clear_session())


def test_agent_session_maps_storage_failures_to_typed_session_error() -> None:
    class FailedLedger:
        def list_items(self, *args: Any, **kwargs: Any):
            raise ConnectionError("raw database endpoint must stay internal")

    session = PostgresAgentSession(
        ledger=FailedLedger(),  # type: ignore[arg-type]
        thread_id="thread-session-failure",
        operation_id="operation-session-failure",
        input_item_id="message-session-failure",
        input_text="读取历史。",
        replay_through_sequence=0,
    )

    with pytest.raises(AgentSessionError) as captured:
        asyncio.run(session.get_items())

    assert captured.value.code == "agent_session_read_failed"
    assert "database endpoint" not in str(captured.value)


def test_agent_turn_runtime_persists_direct_response_and_replays_operation() -> None:
    adapter = SessionWritingAdapter(
        {
            "answerMarkdown": "已根据当前上下文完成解释。",
            "materialRefs": [],
            "limitationRefs": [],
        }
    )
    ledger, runtime = _runtime(adapter)

    result = asyncio.run(runtime.run(_request()))

    assert result.status == "completed"
    assert result.thread_head.customer_state == "completed"
    assert result.terminal_admission is not None
    assert result.terminal_admission.completion_kind == "direct_response"
    assert result.customer_projection()["completionKind"] == "direct_response"
    assert result.assistant_item.customer_visible is True
    assert result.assistant_item.text == "已根据当前上下文完成解释。"
    assert result.terminal_item.item_type == "task_terminal"
    assert (
        len(
            [
                item
                for item in ledger.list_items("thread-runtime")
                if item.role == "assistant" and item.customer_visible
            ]
        )
        == 1
    )
    assert adapter.histories == [[]]

    replay = asyncio.run(runtime.run(_request()))
    assert replay.replayed is True
    assert len(adapter.calls) == 1
    assert replay.assistant_item.item_id == result.assistant_item.item_id


def test_agent_turn_runtime_reuses_gateway_preaccepted_user_item() -> None:
    adapter = SessionWritingAdapter(
        {
            "answerMarkdown": "已接续 Gateway 接受的消息。",
            "materialRefs": [],
            "limitationRefs": [],
        }
    )
    ledger, runtime = _runtime(adapter)
    request = _request()
    ledger.append_items(
        request.thread_id,
        [
            NewThreadItem(
                item_id=request.user_item_id,
                item_type="user_message",
                role="user",
                text=request.user_message,
                operation_key=f"user:{request.operation_id}",
                customer_visible=True,
                payload={
                    "sdk_item": {
                        "role": "user",
                        "content": request.user_message,
                    },
                    "sdk_replay": True,
                    "run_id": request.run_id,
                },
            )
        ],
        expected_state_version=0,
        head_target=ThreadHeadTarget(
            active_task_id=request.run_id,
            active_topic_ref=None,
            pending_action_ref=None,
            customer_state="working",
        ),
    )

    result = asyncio.run(runtime.run(request))

    assert result.status == "completed"
    assert (
        len(
            [
                item
                for item in ledger.list_items(request.thread_id)
                if item.operation_key == f"user:{request.operation_id}"
            ]
        )
        == 1
    )
    assert adapter.histories == [[]]


def test_agent_turn_runtime_persists_tool_call_before_result_and_closes_refs() -> None:
    adapter = SessionWritingAdapter(
        {
            "answerMarkdown": "工具材料已核对。",
            "materialRefs": ["artifact:tool-result"],
            "limitationRefs": [],
        },
        tool_events=(
            (
                "inspect_analysis_artifact",
                "call-tool-1",
                {"artifact_ref": "artifact:source"},
                {"artifactRefs": ["artifact:tool-result"]},
            ),
            (
                "explain_claim",
                "call-tool-2",
                {"claim_ref": "claim:1"},
                {"materialRefs": ["artifact:tool-result"]},
            ),
        ),
    )
    ledger, runtime = _runtime(adapter)

    result = asyncio.run(runtime.run(_request()))

    assert result.status == "completed"
    tool_items = [
        item
        for item in ledger.list_items("thread-runtime")
        if item.item_type in {"tool_call", "tool_result"}
    ]
    assert [item.item_type for item in tool_items] == [
        "tool_call",
        "tool_result",
        "tool_call",
        "tool_result",
    ]
    assert [item.operation_key for item in tool_items] == [
        "tool-call:operation-1:call-tool-1",
        "tool-result:operation-1:call-tool-1",
        "tool-call:operation-1:call-tool-2",
        "tool-result:operation-1:call-tool-2",
    ]


def test_current_operation_tool_refs_close_without_post_answer_compaction() -> None:
    adapter = SessionWritingAdapter(
        {
            "answerMarkdown": "本轮工具材料已核对。",
            "materialRefs": ["artifact:current-operation"],
            "limitationRefs": [],
        },
        tool_events=(
            (
                "explain_claim",
                "call-current-operation",
                {"claim_ref": "claim:current"},
                {"materialRefs": ["artifact:current-operation"]},
            ),
        ),
    )
    ledger = InMemoryThreadItemLedger()
    ledger.create_thread("thread-runtime")
    ledger.append_items(
        "thread-runtime",
        [
            NewThreadItem(
                item_id=f"historical-message-{sequence}",
                item_type="user_message",
                role="user",
                text=f"历史消息 {sequence}",
                operation_key=f"user:historical-{sequence}",
                customer_visible=True,
            )
            for sequence in range(1, 4)
        ],
    )
    runtime = AgentTurnRuntime(
        ledger=ledger,
        context_assembler=AgentContextAssembler(
            ledger=ledger,
            artifact_index=InMemoryArtifactIndex(),
            recent_item_limit=6,
            compaction_retention=2,
        ),
        adapter=adapter,
    )

    result = asyncio.run(
        runtime.run(
            replace(
                _request(),
                expected_state_version=1,
            )
        )
    )

    assert result.status == "completed"
    assert result.final_output == {
        "answerMarkdown": "本轮工具材料已核对。",
        "materialRefs": ["artifact:current-operation"],
        "limitationRefs": [],
    }
    assert ledger.get_head("thread-runtime").latest_item_sequence > 6


def test_agent_turn_runtime_rejects_unknown_material_ref_with_failed_terminal() -> None:
    adapter = SessionWritingAdapter(
        {
            "answerMarkdown": "引用了不存在的材料。",
            "materialRefs": ["artifact:unknown"],
            "limitationRefs": [],
        }
    )
    ledger, runtime = _runtime(adapter)

    result = asyncio.run(runtime.run(_request()))

    assert result.status == "failed"
    assert result.error_code == "agent_final_material_ref_unknown"
    assert result.thread_head.customer_state == "idle"
    assert result.terminal_admission is not None
    assert result.terminal_admission.completion_kind == "failed_turn"
    customer = result.customer_projection()
    assert "agent_final_material_ref_unknown" not in json.dumps(customer)
    assert "provider" not in json.dumps(customer)
    assert ledger.get_item_by_operation_key("thread-runtime", "terminal:operation-1")
    adapter.final_output = {
        "answerMarkdown": "已按纠正后的合同回答。",
        "materialRefs": [],
        "limitationRefs": [],
    }
    corrected = asyncio.run(
        runtime.run(
            replace(
                _request(operation_id="operation-2"),
                expected_state_version=result.thread_head.state_version,
            )
        )
    )
    assert corrected.status == "completed"
    assert corrected.thread_head.customer_state == "completed"


def test_agent_turn_runtime_maps_provider_failure_to_server_only_terminal_detail() -> (
    None
):
    adapter = SessionWritingAdapter(
        {},
        error=LLMProviderError(
            kind="provider_rate_limited",
            retryability="retryable",
            status_code=429,
            error_code="private-provider-code",
        ),
    )
    _, runtime = _runtime(adapter)

    result = asyncio.run(runtime.run(_request()))

    assert result.status == "failed"
    assert result.error_code == "provider_rate_limited"
    assert result.assistant_item.text == "当前请求暂时未能完成，请稍后重试。"
    assert "private-provider-code" not in json.dumps(result.customer_projection())


def test_post_tool_model_failure_delivers_persisted_customer_safe_summary() -> None:
    tool_result = AgentToolResult(
        status="limited",
        output={"schemaVersion": "waje-model-material.v1"},
        artifactRefs=["artifact:publication-1"],
        materialRefs=["claim:payment-outcome"],
        limitationRefs=["limitation:process-evidence-unavailable"],
        retryability="never",
        customerSummary=(
            "已发布材料显示支付成功率从58.75%升至63.23%；"
            "现有终态快照不能解释失败环节、重试或耗时。"
        ),
        technicalDetailRef=None,
    )
    adapter = SessionWritingAdapter(
        {},
        tool_events=(
            (
                "inspect_analysis_artifact",
                "call-inspect",
                {},
                tool_result.model_dump(mode="json", by_alias=True),
            ),
        ),
        post_tool_error=LLMProviderError(
            kind="provider_unavailable",
            retryability="retryable",
            status_code=503,
            error_code="private-provider-code",
        ),
    )
    ledger, runtime = _runtime(adapter)
    tool = WajeAgentTool(
        name="inspect_analysis_artifact",
        description="Read one persisted customer-safe analysis artifact.",
        input_model=_NoArguments,
        handler=lambda _arguments: tool_result,
        failure_recovery="customer_summary",
    )
    action_binding = AgentTurnActionBinding.create(
        catalog_digest="catalog-digest",
        input_digest="input-digest",
        action_context_digest="context-digest",
        selected_tools=[tool.name],
        initial_action="call_tool",
        required_tool_name=tool.name,
        required_tool_arguments={},
        material_decision_topics=[],
    )

    result = asyncio.run(
        runtime.run(
            replace(
                _request(),
                tools=(tool,),
                action_binding=action_binding,
            )
        )
    )

    assert result.status == "completed_with_limits"
    assert result.error_code == "provider_unavailable"
    assert result.assistant_item.text == tool_result.customer_summary
    assert result.final_output == {
        "answerMarkdown": tool_result.customer_summary,
        "materialRefs": [
            "artifact:publication-1",
            "claim:payment-outcome",
        ],
        "limitationRefs": ["limitation:process-evidence-unavailable"],
    }
    assert result.terminal_admission is not None
    assert result.terminal_admission.completion_kind == "tool_response"
    assert result.terminal_admission.executed_tool_names == [
        "inspect_analysis_artifact"
    ]
    assert result.thread_head.customer_state == "completed_with_limits"
    assert "private-provider-code" not in json.dumps(result.customer_projection())
    terminal = ledger.get_item_by_operation_key(
        "thread-runtime",
        "terminal:operation-1",
    )
    assert terminal is not None
    assert terminal.payload["error_code"] == "provider_unavailable"


def test_terminal_tool_failure_delivers_summary_without_a_second_model_turn() -> None:
    tool_result = AgentToolResult(
        status="failed",
        output=None,
        artifactRefs=[],
        materialRefs=[],
        limitationRefs=[],
        retryability="replan_required",
        customerSummary="当前线程中没有找到可用于解释的已发布材料。",
        technicalDetailRef=None,
    )
    adapter = SessionWritingAdapter(
        {},
        tool_events=(
            (
                "inspect_analysis_artifact",
                "call-missing-artifact",
                {},
                tool_result.model_dump(mode="json", by_alias=True),
                False,
            ),
        ),
        post_tool_error=AgentSdkAdapterError("agent_tool_terminal_failure"),
    )
    ledger, runtime = _runtime(adapter)
    tool = WajeAgentTool(
        name="inspect_analysis_artifact",
        description="Read one persisted customer-safe analysis artifact.",
        input_model=_NoArguments,
        handler=lambda _arguments: tool_result,
        failure_recovery="customer_summary",
    )
    action_binding = AgentTurnActionBinding.create(
        catalog_digest="catalog-digest",
        input_digest="input-digest",
        action_context_digest="context-digest",
        selected_tools=[tool.name],
        initial_action="call_tool",
        required_tool_name=tool.name,
        required_tool_arguments={},
        material_decision_topics=[],
    )

    result = asyncio.run(
        runtime.run(
            replace(
                _request(),
                tools=(tool,),
                action_binding=action_binding,
            )
        )
    )

    assert result.status == "completed_with_limits"
    assert result.error_code == "agent_tool_terminal_failure"
    assert result.assistant_item.text == tool_result.customer_summary
    assert result.final_output == {
        "answerMarkdown": tool_result.customer_summary,
        "materialRefs": [],
        "limitationRefs": [],
    }
    tool_results = [
        item
        for item in ledger.list_items("thread-runtime")
        if item.item_type == "tool_result"
    ]
    assert len(tool_results) == 1
    assert tool_results[0].payload["succeeded"] is False


def test_post_tool_model_failure_does_not_recover_an_unapproved_tool_result() -> None:
    tool_result = AgentToolResult(
        status="succeeded",
        output={"schemaVersion": "internal-operation.v1"},
        artifactRefs=[],
        materialRefs=[],
        limitationRefs=[],
        retryability="never",
        customerSummary="工具执行完成。",
        technicalDetailRef=None,
    )
    adapter = SessionWritingAdapter(
        {},
        tool_events=(
            (
                "ordinary_tool",
                "call-ordinary",
                {},
                tool_result.model_dump(mode="json", by_alias=True),
            ),
        ),
        post_tool_error=LLMProviderError(
            kind="provider_unavailable",
            retryability="retryable",
        ),
    )
    _, runtime = _runtime(adapter)
    tool = WajeAgentTool(
        name="ordinary_tool",
        description="Execute one ordinary operation.",
        input_model=_NoArguments,
        handler=lambda _arguments: tool_result,
    )
    action_binding = AgentTurnActionBinding.create(
        catalog_digest="catalog-digest",
        input_digest="input-digest",
        action_context_digest="context-digest",
        selected_tools=[tool.name],
        initial_action="call_tool",
        required_tool_name=tool.name,
        required_tool_arguments={},
        material_decision_topics=[],
    )

    result = asyncio.run(
        runtime.run(
            replace(
                _request(),
                tools=(tool,),
                action_binding=action_binding,
            )
        )
    )

    assert result.status == "failed"
    assert result.terminal_admission is not None
    assert result.terminal_admission.completion_kind == "failed_turn"


def test_post_tool_model_failure_rejects_an_unsafe_recovery_summary() -> None:
    tool_result = AgentToolResult(
        status="succeeded",
        output={"schemaVersion": "waje-model-material.v1"},
        artifactRefs=["artifact:publication-1"],
        materialRefs=[],
        limitationRefs=[],
        retryability="never",
        customerSummary=f"内部引用 sha256:{'a' * 64}",
        technicalDetailRef=None,
    )
    adapter = SessionWritingAdapter(
        {},
        tool_events=(
            (
                "inspect_analysis_artifact",
                "call-inspect",
                {},
                tool_result.model_dump(mode="json", by_alias=True),
            ),
        ),
        post_tool_error=LLMProviderError(
            kind="provider_unavailable",
            retryability="retryable",
        ),
    )
    _, runtime = _runtime(adapter)
    tool = WajeAgentTool(
        name="inspect_analysis_artifact",
        description="Read one persisted customer-safe analysis artifact.",
        input_model=_NoArguments,
        handler=lambda _arguments: tool_result,
        failure_recovery="customer_summary",
    )
    action_binding = AgentTurnActionBinding.create(
        catalog_digest="catalog-digest",
        input_digest="input-digest",
        action_context_digest="context-digest",
        selected_tools=[tool.name],
        initial_action="call_tool",
        required_tool_name=tool.name,
        required_tool_arguments={},
        material_decision_topics=[],
    )

    result = asyncio.run(
        runtime.run(
            replace(
                _request(),
                tools=(tool,),
                action_binding=action_binding,
            )
        )
    )

    assert result.status == "failed"
    assert result.terminal_admission is not None
    assert result.terminal_admission.completion_kind == "failed_turn"
    assert "sha256:" not in result.assistant_item.text


def test_context_assembler_restores_recent_items_and_customer_safe_artifact_index() -> (
    None
):
    artifacts = InMemoryArtifactIndex()
    artifacts.add(
        "thread-runtime",
        ArtifactDescriptor(
            artifact_ref="artifact:publication-1",
            artifact_type="bi_publication",
            version="publication:v1",
            digest="a" * 64,
            source_refs=("run-source", "claim:1"),
            visibility_policy_ref="visibility:customer-safe",
            customer_summary="已发布业务结论。",
            created_at="2026-07-21T00:00:00+00:00",
        ),
    )
    adapter = SessionWritingAdapter(
        {
            "answerMarkdown": "引用原发布材料。",
            "materialRefs": ["artifact:publication-1"],
            "limitationRefs": [],
        }
    )
    ledger, runtime = _runtime(adapter, artifacts=artifacts)

    result = asyncio.run(runtime.run(_request()))

    assert result.status == "completed"
    assert "artifact:publication-1" in adapter.calls[0].instructions
    assert adapter.calls[0].input_text == "继续解释这个结果。"
    assert '"delivery":"postgres_agent_session"' in adapter.calls[0].instructions
    assert "继续解释这个结果" not in adapter.calls[0].instructions
    assert result.context_version
    assert ledger.get_head("thread-runtime").latest_item_sequence >= 4


def test_schema_extends_existing_conversation_history_without_second_ledger() -> None:
    schema = (ROOT / "tools/runtime/conversation-runtime.sql").read_text(
        encoding="utf-8"
    )
    assert "CREATE TABLE IF NOT EXISTS waje_runtime.thread_item" not in schema
    assert "item_sequence bigint" in schema
    assert "operation_key text" in schema
    assert "state_version bigint" in schema
    assert "conversation_messages_allocate_sequence" in schema
    assert "idx_conversation_messages_operation_key" in schema

    gateway = (ROOT / "app/api/_conversationStore.ts").read_text(encoding="utf-8")
    assert "AND customer_visible = true" in gateway
    assert "const operationKey = `user:${normalized.requestIdentity}`" in gateway
    assert 'item_type: "user_message"' in gateway
    assert "SET active_task_id = $2, customer_state = 'working'" in gateway
