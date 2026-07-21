from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import bi_agent.runtime.general_agent_entry as general_agent_entry
from bi_agent.runtime.agent_context import AgentContextAssembler, InMemoryArtifactIndex
from bi_agent.runtime.agent_sdk_contracts import WajeAgentRunRequest, WajeAgentRunResult
from bi_agent.runtime.agent_turn_runtime import AgentTurnRuntime
from bi_agent.runtime.general_agent_entry import (
    GENERAL_AGENT_INSTRUCTIONS,
    GeneralAgentRuntimeBindings,
    GeneralAgentTurnCommand,
    run_general_agent_turn,
)
from bi_agent.runtime.thread_item_ledger import InMemoryThreadItemLedger


ROOT = Path(__file__).resolve().parents[2]


class DirectAdapter:
    def __init__(self) -> None:
        self.calls: list[WajeAgentRunRequest] = []

    async def run(self, request: WajeAgentRunRequest) -> WajeAgentRunResult:
        self.calls.append(request)
        return WajeAgentRunResult(
            run_id=request.run_id,
            final_output={
                "answerMarkdown": "这是通用 Agent 的直接回答。",
                "materialRefs": [],
                "limitationRefs": [],
            },
            usage={"input_tokens": 8, "output_tokens": 6},
            model_turns=1,
        )


def _bindings() -> tuple[GeneralAgentRuntimeBindings, DirectAdapter]:
    ledger = InMemoryThreadItemLedger()
    ledger.create_thread("thread-entry")
    adapter = DirectAdapter()
    runtime = AgentTurnRuntime(
        ledger=ledger,
        context_assembler=AgentContextAssembler(
            ledger=ledger,
            artifact_index=InMemoryArtifactIndex(),
        ),
        adapter=adapter,
    )
    return (
        GeneralAgentRuntimeBindings(
            store=SimpleNamespace(thread_item_ledger=ledger),
            provider=SimpleNamespace(),
            runtime=runtime,
            tools=(),
        ),
        adapter,
    )


def test_general_agent_entry_runs_one_sdk_neutral_direct_turn() -> None:
    bindings, adapter = _bindings()
    command = GeneralAgentTurnCommand(
        threadId="thread-entry",
        actorId="actor-1",
        operationId="request-1",
        message="解释一下你能做什么。",
    )

    result = asyncio.run(run_general_agent_turn(command, bindings=bindings))

    assert result.status == "completed"
    assert result.customer_projection() == {
        "message": {
            "role": "assistant",
            "text": "这是通用 Agent 的直接回答。",
            "createdAt": result.assistant_item.created_at,
        },
        "status": "completed",
        "transport": {"stateVersion": "2", "latestItemSequence": 3},
    }
    assert adapter.calls[0].instructions.startswith(GENERAL_AGENT_INSTRUCTIONS.strip())
    assert command.agent_run_id.startswith("agent-run-")
    assert command.user_item_id.startswith("agent-message-")


def test_general_agent_entry_identity_is_stable_and_request_scoped() -> None:
    first = GeneralAgentTurnCommand(
        threadId="thread-entry",
        actorId="actor-1",
        operationId="request-1",
        message="问题一",
    )
    replay = GeneralAgentTurnCommand(
        threadId="thread-entry",
        actorId="actor-1",
        operationId="request-1",
        message="问题一",
    )
    other = GeneralAgentTurnCommand(
        threadId="thread-entry",
        actorId="actor-1",
        operationId="request-2",
        message="问题一",
    )

    assert first.agent_run_id == replay.agent_run_id
    assert first.user_item_id == replay.user_item_id
    assert first.agent_run_id != other.agent_run_id
    assert first.user_item_id != other.user_item_id


def test_general_agent_entry_accepts_only_typed_pending_action_resolution() -> None:
    command = GeneralAgentTurnCommand.model_validate(
        {
            "threadId": "thread-entry",
            "actorId": "actor-1",
            "operationId": "request-2",
            "message": "采用推荐口径。",
            "pendingActionResolution": {
                "actionRef": "pending-action:1",
                "decision": "answered",
                "selectedOptionId": "recommended",
                "answerText": "采用推荐口径。",
            },
        }
    )
    assert command.pending_action_resolution is not None
    assert command.pending_action_resolution.selected_option_id == "recommended"

    with pytest.raises(ValueError):
        GeneralAgentTurnCommand.model_validate(
            {
                "threadId": "thread-entry",
                "actorId": "actor-1",
                "operationId": "request-2",
                "message": "采用推荐口径。",
                "pendingActionResolution": {
                    "actionRef": "pending-action:1",
                    "decision": "continue",
                    "answerText": "采用推荐口径。",
                },
            }
        )


def test_cli_acknowledges_only_after_the_turn_is_durably_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> tuple[list[str], list[str]]:
        ledger = InMemoryThreadItemLedger()
        ledger.create_thread("thread-entry")
        release = asyncio.Event()
        events: list[str] = []

        class BlockingAdapter:
            async def run(self, request: WajeAgentRunRequest) -> WajeAgentRunResult:
                events.append("model_started")
                await release.wait()
                return WajeAgentRunResult(
                    run_id=request.run_id,
                    final_output={
                        "answerMarkdown": "持久化接受后继续。",
                        "materialRefs": [],
                        "limitationRefs": [],
                    },
                    usage={},
                    model_turns=1,
                )

        class Provider:
            async def close(self) -> None:
                events.append("provider_closed")

        runtime = AgentTurnRuntime(
            ledger=ledger,
            context_assembler=AgentContextAssembler(
                ledger=ledger,
                artifact_index=InMemoryArtifactIndex(),
            ),
            adapter=BlockingAdapter(),
        )
        store = SimpleNamespace(
            thread_item_ledger=ledger,
            connection=SimpleNamespace(close=lambda: events.append("store_closed")),
        )
        bindings = GeneralAgentRuntimeBindings(
            store=store,
            provider=Provider(),
            runtime=runtime,
            tools=(),
        )
        command = GeneralAgentTurnCommand(
            threadId="thread-entry",
            actorId="actor-1",
            operationId="request-ack",
            message="开始处理。",
        )

        def acknowledge() -> None:
            assert ledger.get_item_by_operation_key(
                "thread-entry", "user:request-ack"
            ) is not None
            assert ledger.get_head("thread-entry").customer_state == "working"
            events.append("acknowledged")
            release.set()

        monkeypatch.setattr(general_agent_entry, "_emit_startup_ack", acknowledge)
        result = await general_agent_entry._run_cli_turn(command, bindings)
        assert result.status == "completed"
        return events, [item.text for item in ledger.list_items("thread-entry")]

    events, texts = asyncio.run(scenario())

    assert events == [
        "model_started",
        "acknowledged",
        "provider_closed",
        "store_closed",
    ]
    assert "持久化接受后继续。" in texts


def test_gateway_process_contract_has_no_agents_sdk_type_imports() -> None:
    source = (ROOT / "bi_agent/runtime/general_agent_entry.py").read_text(
        encoding="utf-8"
    )
    assert "from agents" not in source
    assert "OpenAI" not in source
    assert "api.openai.com" not in source
