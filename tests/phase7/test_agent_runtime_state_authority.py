from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from bi_agent.runtime.agent_context import (
    AgentContextAssembler,
    ArtifactDescriptor,
    InMemoryArtifactIndex,
)
from bi_agent.runtime.agent_sdk_contracts import (
    WajeAgentRunRequest,
    WajeAgentRunResult,
)
from bi_agent.runtime.agent_turn_runtime import (
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

    def execute(
        self,
        statement: str,
        params: Mapping[str, Any],
    ) -> FakeResult:
        self.statements.append((statement, dict(params)))
        if "FOR UPDATE" in statement:
            return FakeResult([("thread-pg", 4, None, "topic-pg", None, 7, "idle")])
        if "operation_key = ANY" in statement:
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
        tool_events: tuple[tuple[str, str, Mapping[str, Any], Any], ...] = (),
        error: Exception | None = None,
    ) -> None:
        self.final_output = dict(final_output)
        self.tool_events = tool_events
        self.error = error
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
        for tool_name, call_id, arguments, result in self.tool_events:
            await request.event_sink.record_tool_call(
                tool_name=tool_name,
                call_id=call_id,
                arguments=arguments,
            )
            await request.event_sink.record_tool_result(
                tool_name=tool_name,
                call_id=call_id,
                result=result,
                succeeded=True,
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

    assert asyncio.run(session.get_items()) == [{"role": "user", "content": "旧问题"}]
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
    assert first.head.latest_item_sequence == 2
    with pytest.raises(RuntimeError, match="agent_session_append_only"):
        asyncio.run(session.clear_session())


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
    assert result.thread_head.customer_state == "failed"
    customer = result.customer_projection()
    assert "agent_final_material_ref_unknown" not in json.dumps(customer)
    assert "provider" not in json.dumps(customer)
    assert ledger.get_item_by_operation_key("thread-runtime", "terminal:operation-1")


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
    assert "继续解释这个结果" in adapter.calls[0].instructions
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
