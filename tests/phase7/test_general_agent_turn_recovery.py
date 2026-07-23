from __future__ import annotations

import asyncio
from typing import Any, Mapping

from bi_agent.runtime.agent_context import AgentContextAssembler, InMemoryArtifactIndex
from bi_agent.runtime.agent_sdk_contracts import WajeAgentRunRequest, WajeAgentRunResult
from bi_agent.runtime.agent_turn_runtime import (
    AgentTurnError,
    AgentTurnResult,
    AgentTurnRuntime,
)
from bi_agent.runtime.general_agent_entry import (
    GeneralAgentRuntimeBindings,
    run_general_agent_turn,
)
from bi_agent.runtime.general_agent_turn_recovery import recover_general_agent_turns
from bi_agent.runtime.thread_item_ledger import (
    InMemoryThreadItemLedger,
    NewThreadItem,
    ThreadHeadTarget,
)


class _Rows:
    def __init__(self, values: list[Mapping[str, Any]]) -> None:
        self.values = values

    def fetchall(self) -> list[Mapping[str, Any]]:
        return self.values


class _Connection:
    def __init__(self, rows: list[Mapping[str, Any]]) -> None:
        self.rows = rows
        self.statements: list[tuple[str, Mapping[str, Any]]] = []
        self.rollbacks = 0

    def execute(self, statement: str, params: Mapping[str, Any]) -> _Rows:
        self.statements.append((statement, params))
        return _Rows(self.rows)

    def rollback(self) -> None:
        self.rollbacks += 1


def _row(*, operation_id: str = "operation-recovery") -> dict[str, Any]:
    from bi_agent.runtime.general_agent_entry import GeneralAgentTurnCommand

    command = GeneralAgentTurnCommand(
        threadId="thread-recovery",
        actorId="actor-recovery",
        operationId=operation_id,
        message="继续完成刚才接受的请求。",
    )
    return {
        "thread_id": command.thread_id,
        "owner_id": command.actor_id,
        "run_id": command.agent_run_id,
        "message_id": command.user_item_id,
        "text": command.message,
        "operation_key": f"user:{command.operation_id}",
        "payload": {
            "run_id": command.agent_run_id,
            "sdk_replay": True,
            "sdk_item": {"role": "user", "content": command.message},
        },
    }


def _result(command: Any, *, status: str = "completed") -> AgentTurnResult:
    item = type(
        "Item",
        (),
        {
            "text": "恢复完成。",
            "created_at": "2026-07-22T00:00:00+00:00",
            "payload": {},
        },
    )()
    head = type(
        "Head",
        (),
        {"state_version": 2, "latest_item_sequence": 3},
    )()
    return AgentTurnResult(
        thread_id=command.thread_id,
        run_id=command.agent_run_id,
        operation_id=command.operation_id,
        status=status,
        final_output=None,
        assistant_item=item,
        terminal_item=None,
        checkpoint_item=None,
        thread_head=head,
        context_version="context-recovery",
        model_turns=1,
        replayed=False,
    )


def test_worker_recovers_exact_durable_general_agent_command() -> None:
    connection = _Connection([_row()])
    commands: list[Any] = []

    def run(command: Any) -> AgentTurnResult:
        commands.append(command)
        return _result(command)

    summary = recover_general_agent_turns(
        store=type("Store", (), {"connection": connection})(),
        runner=run,
        limit=7,
    )

    assert summary == {
        "discovered": ["operation-recovery"],
        "completed": [
            {
                "run_id": commands[0].agent_run_id,
                "operation_id": "operation-recovery",
                "status": "completed",
            }
        ],
        "in_progress": [],
        "failed": [],
    }
    assert commands[0].actor_id == "actor-recovery"
    assert commands[0].message == "继续完成刚才接受的请求。"
    statement, params = connection.statements[0]
    assert params == {"limit": 7, "thread_id": None}
    assert "thread.customer_state = 'working'" in statement
    assert "thread.active_task_id LIKE 'agent-run-%%'" in statement
    assert "terminal.operation_key" in statement
    assert "checkpoint.operation_key" in statement
    assert "candidate.payload->>'run_id' = thread.active_task_id" in statement
    assert connection.rollbacks == 1


def test_live_operation_is_deferred_without_starting_a_second_model_loop() -> None:
    connection = _Connection([_row(operation_id="operation-live")])

    def run(command: Any) -> AgentTurnResult:
        raise AgentTurnError(
            "agent_turn_operation_in_progress",
            retryability="retryable",
        )

    summary = recover_general_agent_turns(
        store=type("Store", (), {"connection": connection})(),
        runner=run,
    )

    assert summary["completed"] == []
    assert summary["in_progress"] == ["operation-live"]
    assert summary["failed"] == []


def test_accepted_turn_is_completed_after_original_process_disappears() -> None:
    row = _row(operation_id="operation-crashed")
    connection = _Connection([row])
    ledger = InMemoryThreadItemLedger()
    ledger.create_thread("thread-recovery")
    ledger.append_items(
        "thread-recovery",
        [
            NewThreadItem(
                item_id=row["message_id"],
                item_type="user_message",
                role="user",
                text=row["text"],
                operation_key=row["operation_key"],
                customer_visible=True,
                payload=row["payload"],
            )
        ],
        expected_state_version=0,
        head_target=ThreadHeadTarget(
            active_task_id=row["run_id"],
            active_topic_ref=None,
            pending_action_ref=None,
            customer_state="working",
        ),
    )

    class Adapter:
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, request: WajeAgentRunRequest) -> WajeAgentRunResult:
            self.calls += 1
            return WajeAgentRunResult(
                run_id=request.run_id,
                final_output={
                    "answerMarkdown": "已从持久化输入恢复并完成。",
                    "materialRefs": [],
                    "limitationRefs": [],
                },
                usage={},
                model_turns=1,
            )

    adapter = Adapter()
    bindings = GeneralAgentRuntimeBindings(
        store=type("RuntimeStore", (), {"thread_item_ledger": ledger})(),
        provider=object(),
        runtime=AgentTurnRuntime(
            ledger=ledger,
            context_assembler=AgentContextAssembler(
                ledger=ledger,
                artifact_index=InMemoryArtifactIndex(),
            ),
            adapter=adapter,
        ),
        tools=(),
    )

    summary = recover_general_agent_turns(
        store=type("Store", (), {"connection": connection})(),
        runner=lambda command: asyncio.run(
            run_general_agent_turn(command, bindings=bindings)
        ),
    )

    assert summary["completed"][0]["status"] == "completed"
    assert adapter.calls == 1
    assert ledger.get_item_by_operation_key(
        "thread-recovery", "terminal:operation-crashed"
    ) is not None
    assert len(
        [
            item
            for item in ledger.list_items("thread-recovery")
            if item.operation_key == "user:operation-crashed"
        ]
    ) == 1


def test_recovery_rejects_tampered_persisted_identity_before_runner() -> None:
    row = _row()
    row["run_id"] = "agent-run-tampered"
    row["payload"] = {**row["payload"], "run_id": "agent-run-tampered"}
    connection = _Connection([row])

    try:
        recover_general_agent_turns(
            store=type("Store", (), {"connection": connection})(),
            runner=lambda command: _result(command),
        )
    except ValueError as exc:
        assert str(exc) == "general_agent_recovery_run_identity_mismatch"
    else:
        raise AssertionError("tampered recovery identity was accepted")
