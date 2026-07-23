from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from bi_agent.runtime.agent_turn_runtime import AgentTurnError, AgentTurnResult
from bi_agent.runtime.general_agent_entry import (
    GeneralAgentTurnCommand,
    run_general_agent_turn,
)


GeneralAgentRecoveryRunner = Callable[[GeneralAgentTurnCommand], AgentTurnResult]


@dataclass(frozen=True)
class RecoverableGeneralAgentTurn:
    command: GeneralAgentTurnCommand
    persisted_run_id: str
    persisted_user_item_id: str

    def __post_init__(self) -> None:
        if self.persisted_run_id != self.command.agent_run_id:
            raise ValueError("general_agent_recovery_run_identity_mismatch")
        if self.persisted_user_item_id != self.command.user_item_id:
            raise ValueError("general_agent_recovery_item_identity_mismatch")


def discover_recoverable_general_agent_turns(
    *,
    store: Any,
    limit: int = 100,
    thread_id: str | None = None,
) -> tuple[RecoverableGeneralAgentTurn, ...]:
    _validate_limit(limit)
    _validate_optional_thread_id(thread_id)
    try:
        rows = store.connection.execute(
            """
            SELECT thread.thread_id,
                   thread.owner_id,
                   thread.active_task_id AS run_id,
                   input.message_id,
                   input.text,
                   input.operation_key,
                   input.payload
            FROM waje_runtime.investigation_threads thread
            JOIN LATERAL (
              SELECT message_id, text, operation_key, payload, item_sequence
              FROM waje_runtime.conversation_messages candidate
              WHERE candidate.thread_id = thread.thread_id
                AND candidate.item_type = 'user_message'
                AND candidate.operation_key LIKE 'user:%%'
                AND candidate.payload->>'run_id' = thread.active_task_id
              ORDER BY candidate.item_sequence DESC
              LIMIT 1
            ) input ON TRUE
            WHERE thread.customer_state = 'working'
              AND (
                %(thread_id)s::text IS NULL
                OR thread.thread_id = %(thread_id)s
              )
              AND thread.active_task_id LIKE 'agent-run-%%'
              AND NOT EXISTS (
                SELECT 1
                FROM waje_runtime.conversation_messages terminal
                WHERE terminal.thread_id = thread.thread_id
                  AND terminal.operation_key =
                    'terminal:' || substring(input.operation_key FROM 6)
              )
              AND NOT EXISTS (
                SELECT 1
                FROM waje_runtime.conversation_messages checkpoint
                WHERE checkpoint.thread_id = thread.thread_id
                  AND checkpoint.operation_key =
                    'checkpoint:' || substring(input.operation_key FROM 6)
              )
            ORDER BY input.item_sequence, thread.thread_id
            LIMIT %(limit)s
            """,
            {"limit": limit, "thread_id": thread_id},
        ).fetchall()
        return tuple(_recoverable_turn(row) for row in rows)
    finally:
        store.connection.rollback()


def recover_general_agent_turns(
    *,
    store: Any,
    runner: GeneralAgentRecoveryRunner | None = None,
    limit: int = 100,
    thread_id: str | None = None,
) -> dict[str, list[Any]]:
    execute = runner or _run_recoverable_turn
    discovered = discover_recoverable_general_agent_turns(
        store=store,
        limit=limit,
        thread_id=thread_id,
    )
    completed: list[dict[str, str]] = []
    in_progress: list[str] = []
    failed: list[dict[str, str]] = []
    for item in discovered:
        command = item.command
        try:
            result = execute(command)
            if result.thread_id != command.thread_id:
                raise ValueError("general_agent_recovery_thread_identity_mismatch")
            if result.run_id != command.agent_run_id:
                raise ValueError("general_agent_recovery_result_identity_mismatch")
            if result.operation_id != command.operation_id:
                raise ValueError("general_agent_recovery_operation_identity_mismatch")
            if result.status not in {
                "completed",
                "completed_with_limits",
                "needs_input",
                "working",
                "failed",
            }:
                raise ValueError("general_agent_recovery_result_status_invalid")
            completed.append(
                {
                    "run_id": result.run_id,
                    "operation_id": result.operation_id,
                    "status": result.status,
                }
            )
        except AgentTurnError as exc:
            if exc.code == "agent_turn_operation_in_progress":
                in_progress.append(command.operation_id)
                continue
            failed.append(
                {
                    "run_id": command.agent_run_id,
                    "operation_id": command.operation_id,
                    "error_code": exc.code,
                }
            )
        except Exception as exc:
            failed.append(
                {
                    "run_id": command.agent_run_id,
                    "operation_id": command.operation_id,
                    "error_code": _stable_error_code(exc),
                }
            )
    return {
        "discovered": [item.command.operation_id for item in discovered],
        "completed": completed,
        "in_progress": in_progress,
        "failed": failed,
    }


def _run_recoverable_turn(command: GeneralAgentTurnCommand) -> AgentTurnResult:
    return asyncio.run(run_general_agent_turn(command))


def _recoverable_turn(row: Any) -> RecoverableGeneralAgentTurn:
    thread_id = _required_text(_field(row, "thread_id", 0))
    owner_id = _required_text(_field(row, "owner_id", 1))
    run_id = _required_text(_field(row, "run_id", 2))
    user_item_id = _required_text(_field(row, "message_id", 3))
    message = _required_text(_field(row, "text", 4), exact=True)
    operation_key = _required_text(_field(row, "operation_key", 5))
    if not operation_key.startswith("user:") or len(operation_key) <= len("user:"):
        raise ValueError("general_agent_recovery_operation_key_invalid")
    operation_id = operation_key[len("user:") :]
    payload = _field(row, "payload", 6)
    if not isinstance(payload, Mapping) or payload.get("run_id") != run_id:
        raise ValueError("general_agent_recovery_payload_invalid")
    command_payload: dict[str, Any] = {
        "threadId": thread_id,
        "actorId": owner_id,
        "operationId": operation_id,
        "message": message,
    }
    pending_resolution = payload.get("pending_action_resolution")
    if pending_resolution is not None:
        if not isinstance(pending_resolution, Mapping):
            raise ValueError("general_agent_recovery_payload_invalid")
        command_payload["pendingActionResolution"] = dict(pending_resolution)
    command = GeneralAgentTurnCommand.model_validate(command_payload)
    return RecoverableGeneralAgentTurn(
        command=command,
        persisted_run_id=run_id,
        persisted_user_item_id=user_item_id,
    )


def _stable_error_code(error: Exception) -> str:
    candidate = getattr(error, "code", None)
    if isinstance(candidate, str) and candidate and candidate == candidate.strip():
        return candidate
    return type(error).__name__


def _field(row: Any, name: str, index: int) -> Any:
    if isinstance(row, Mapping):
        return row.get(name)
    return row[index]


def _required_text(value: Any, *, exact: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("general_agent_recovery_payload_invalid")
    if exact and value != value.strip():
        raise ValueError("general_agent_recovery_payload_invalid")
    return value if exact else value.strip()


def _validate_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("general_agent_recovery_limit_invalid")


def _validate_optional_thread_id(thread_id: str | None) -> None:
    if thread_id is not None and (
        not isinstance(thread_id, str)
        or not thread_id
        or thread_id != thread_id.strip()
    ):
        raise ValueError("general_agent_recovery_thread_invalid")


__all__ = (
    "RecoverableGeneralAgentTurn",
    "discover_recoverable_general_agent_turns",
    "recover_general_agent_turns",
)
