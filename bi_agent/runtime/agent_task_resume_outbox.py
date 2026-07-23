from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Any, Optional

from bi_agent.runtime.general_agent_entry import resume_general_agent_task


ResumeRunner = Callable[[str, str], Optional[Mapping[str, Any]]]


@dataclass(frozen=True)
class AgentTaskResumeEnvelope:
    resume_ref: str
    thread_id: str
    task_ref: str
    attempt_count: int
    lease_owner_id: str
    lease_epoch: int


class AgentTaskResumeLeaseLost(RuntimeError):
    pass


class PostgresAgentTaskResumeOutbox:
    """Durable handoff from BI task terminal state to AgentTurnRuntime resume."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def enqueue_ready(
        self,
        *,
        limit: int = 100,
        thread_id: str | None = None,
    ) -> tuple[str, ...]:
        _validate_limit(limit)
        _validate_optional_thread_id(thread_id)
        rows = self.connection.execute(
            """
            INSERT INTO waje_runtime.agent_task_resume_outbox(
              resume_ref, thread_id, task_ref
            )
            SELECT
              'agent-task-resume:' || run.run_id,
              run.thread_id,
              run.run_id
            FROM waje_runtime.analysis_runs run
            JOIN waje_runtime.investigation_threads thread
              ON thread.thread_id = run.thread_id
             AND thread.active_task_id = run.run_id
             AND thread.customer_state = 'working'
            WHERE run.status IN ('completed', 'failed')
              AND (
                %(thread_id)s::text IS NULL
                OR run.thread_id = %(thread_id)s
              )
              AND EXISTS (
                SELECT 1
                FROM waje_runtime.conversation_messages checkpoint
                WHERE checkpoint.thread_id = run.thread_id
                  AND checkpoint.operation_key LIKE 'checkpoint:%%'
                  AND checkpoint.payload->'checkpoint'->>'checkpointKind' =
                      'waiting_for_task'
                  AND checkpoint.payload->'checkpoint'->>'awaitedTaskRef' =
                      run.run_id
              )
            ORDER BY run.updated_at, run.run_id
            LIMIT %(limit)s
            ON CONFLICT (thread_id, task_ref) DO NOTHING
            RETURNING resume_ref
            """,
            {"limit": limit, "thread_id": thread_id},
        ).fetchall()
        self.connection.commit()
        return tuple(str(_field(row, "resume_ref", 0)) for row in rows)

    def claim_ready(
        self,
        *,
        lease_owner_id: str,
        limit: int = 100,
        max_attempts: int = 5,
        lease_seconds: int = 900,
        thread_id: str | None = None,
    ) -> tuple[AgentTaskResumeEnvelope, ...]:
        _validate_exact_text(lease_owner_id, "agent_task_resume_lease_owner_invalid")
        _validate_limit(limit)
        _validate_limit(max_attempts)
        _validate_limit(lease_seconds)
        _validate_optional_thread_id(thread_id)
        try:
            self.connection.execute("BEGIN")
            rows = self.connection.execute(
                """
                SELECT resume_ref
                FROM waje_runtime.agent_task_resume_outbox
                WHERE attempt_count < %(max_attempts)s
                  AND (
                    %(thread_id)s::text IS NULL
                    OR thread_id = %(thread_id)s
                  )
                  AND (
                    outbox_state IN ('pending', 'failed')
                    OR (
                      outbox_state = 'processing'
                      AND lease_expires_at <= now()
                    )
                  )
                ORDER BY updated_at, resume_ref
                LIMIT %(limit)s
                FOR UPDATE SKIP LOCKED
                """,
                {
                    "limit": limit,
                    "max_attempts": max_attempts,
                    "thread_id": thread_id,
                },
            ).fetchall()
            envelopes: list[AgentTaskResumeEnvelope] = []
            for row in rows:
                resume_ref = str(_field(row, "resume_ref", 0))
                updated = self.connection.execute(
                    """
                    UPDATE waje_runtime.agent_task_resume_outbox
                    SET outbox_state = 'processing',
                        attempt_count = attempt_count + 1,
                        lease_owner_id = %(lease_owner_id)s,
                        lease_epoch = lease_epoch + 1,
                        lease_expires_at = now()
                          + (%(lease_seconds)s * interval '1 second'),
                        last_error_code = NULL,
                        updated_at = now()
                    WHERE resume_ref = %(resume_ref)s
                      AND attempt_count < %(max_attempts)s
                      AND (
                        outbox_state IN ('pending', 'failed')
                        OR (
                          outbox_state = 'processing'
                          AND lease_expires_at <= now()
                        )
                      )
                    RETURNING resume_ref, thread_id, task_ref, attempt_count,
                              lease_owner_id, lease_epoch
                    """,
                    {
                        "resume_ref": resume_ref,
                        "lease_owner_id": lease_owner_id,
                        "lease_seconds": lease_seconds,
                        "max_attempts": max_attempts,
                    },
                ).fetchone()
                if updated is None:
                    continue
                envelopes.append(_envelope(updated))
            self.connection.commit()
            return tuple(envelopes)
        except Exception:
            self.connection.rollback()
            raise

    def sweep_exhausted(
        self,
        *,
        max_attempts: int = 5,
        limit: int = 100,
        thread_id: str | None = None,
    ) -> tuple[str, ...]:
        _validate_limit(max_attempts)
        _validate_limit(limit)
        _validate_optional_thread_id(thread_id)
        rows = self.connection.execute(
            """
            WITH exhausted AS (
              SELECT resume_ref
              FROM waje_runtime.agent_task_resume_outbox
              WHERE attempt_count >= %(max_attempts)s
                AND (
                  %(thread_id)s::text IS NULL
                  OR thread_id = %(thread_id)s
                )
                AND (
                  outbox_state = 'failed'
                  OR (
                    outbox_state = 'processing'
                    AND lease_expires_at <= now()
                  )
                )
              ORDER BY updated_at, resume_ref
              LIMIT %(limit)s
              FOR UPDATE SKIP LOCKED
            )
            UPDATE waje_runtime.agent_task_resume_outbox outbox
            SET outbox_state = 'exhausted',
                exhausted_at = COALESCE(outbox.exhausted_at, now()),
                lease_owner_id = NULL,
                lease_expires_at = NULL,
                updated_at = now()
            FROM exhausted
            WHERE outbox.resume_ref = exhausted.resume_ref
            RETURNING outbox.resume_ref
            """,
            {
                "max_attempts": max_attempts,
                "limit": limit,
                "thread_id": thread_id,
            },
        ).fetchall()
        self.connection.commit()
        return tuple(str(_field(row, "resume_ref", 0)) for row in rows)

    def heartbeat(
        self,
        envelope: AgentTaskResumeEnvelope,
        *,
        lease_seconds: int,
    ) -> None:
        _validate_limit(lease_seconds)
        updated = self.connection.execute(
            """
            UPDATE waje_runtime.agent_task_resume_outbox
            SET lease_expires_at = now()
                  + (%(lease_seconds)s * interval '1 second'),
                updated_at = now()
            WHERE resume_ref = %(resume_ref)s
              AND outbox_state = 'processing'
              AND attempt_count = %(attempt_count)s
              AND lease_owner_id = %(lease_owner_id)s
              AND lease_epoch = %(lease_epoch)s
            RETURNING resume_ref
            """,
            {
                "resume_ref": envelope.resume_ref,
                "attempt_count": envelope.attempt_count,
                "lease_owner_id": envelope.lease_owner_id,
                "lease_epoch": envelope.lease_epoch,
                "lease_seconds": lease_seconds,
            },
        ).fetchone()
        if updated is None:
            self.connection.rollback()
            raise AgentTaskResumeLeaseLost(
                "agent_task_resume_outbox_heartbeat_lease_lost"
            )
        self.connection.commit()

    def complete(self, envelope: AgentTaskResumeEnvelope) -> None:
        updated = self.connection.execute(
            """
            UPDATE waje_runtime.agent_task_resume_outbox
            SET outbox_state = 'completed',
                lease_owner_id = NULL,
                lease_expires_at = NULL,
                updated_at = now()
            WHERE resume_ref = %(resume_ref)s
              AND outbox_state = 'processing'
              AND attempt_count = %(attempt_count)s
              AND lease_owner_id = %(lease_owner_id)s
              AND lease_epoch = %(lease_epoch)s
            RETURNING resume_ref
            """,
            {
                "resume_ref": envelope.resume_ref,
                "attempt_count": envelope.attempt_count,
                "lease_owner_id": envelope.lease_owner_id,
                "lease_epoch": envelope.lease_epoch,
            },
        ).fetchone()
        if updated is None:
            self.connection.rollback()
            raise AgentTaskResumeLeaseLost(
                "agent_task_resume_outbox_completion_lease_lost"
            )
        self.connection.commit()

    def fail(
        self,
        envelope: AgentTaskResumeEnvelope,
        *,
        error_code: str,
        max_attempts: int = 5,
    ) -> str:
        if not error_code or error_code != error_code.strip():
            raise ValueError("agent_task_resume_error_code_invalid")
        _validate_limit(max_attempts)
        updated = self.connection.execute(
            """
            UPDATE waje_runtime.agent_task_resume_outbox
            SET outbox_state = CASE
                  WHEN attempt_count >= %(max_attempts)s THEN 'exhausted'
                  ELSE 'failed'
                END,
                last_error_code = %(error_code)s,
                exhausted_at = CASE
                  WHEN attempt_count >= %(max_attempts)s
                    THEN COALESCE(exhausted_at, now())
                  ELSE NULL
                END,
                lease_owner_id = NULL,
                lease_expires_at = NULL,
                updated_at = now()
            WHERE resume_ref = %(resume_ref)s
              AND outbox_state = 'processing'
              AND attempt_count = %(attempt_count)s
              AND lease_owner_id = %(lease_owner_id)s
              AND lease_epoch = %(lease_epoch)s
            RETURNING outbox_state
            """,
            {
                "resume_ref": envelope.resume_ref,
                "attempt_count": envelope.attempt_count,
                "error_code": error_code,
                "max_attempts": max_attempts,
                "lease_owner_id": envelope.lease_owner_id,
                "lease_epoch": envelope.lease_epoch,
            },
        ).fetchone()
        if updated is None:
            self.connection.rollback()
            raise AgentTaskResumeLeaseLost(
                "agent_task_resume_outbox_failure_lease_lost"
            )
        self.connection.commit()
        return str(_field(updated, "outbox_state", 0))


def process_agent_task_resume_outbox(
    *,
    outbox: PostgresAgentTaskResumeOutbox,
    resume_runner: ResumeRunner | None = None,
    limit: int = 100,
    worker_id: str | None = None,
    max_attempts: int = 5,
    lease_seconds: int = 900,
    heartbeat_interval_seconds: float | None = None,
    thread_id: str | None = None,
) -> dict[str, list[Any]]:
    runner = resume_runner or _resume_task
    lease_owner_id = worker_id or f"agent-resume:{os.getpid()}:{uuid.uuid4().hex}"
    _validate_exact_text(lease_owner_id, "agent_task_resume_worker_id_invalid")
    _validate_limit(max_attempts)
    _validate_limit(lease_seconds)
    _validate_optional_thread_id(thread_id)
    heartbeat_interval = (
        min(30.0, lease_seconds / 3)
        if heartbeat_interval_seconds is None
        else heartbeat_interval_seconds
    )
    if (
        isinstance(heartbeat_interval, bool)
        or not isinstance(heartbeat_interval, (int, float))
        or heartbeat_interval <= 0
        or heartbeat_interval >= lease_seconds
    ):
        raise ValueError("agent_task_resume_heartbeat_interval_invalid")
    scope_kwargs = {} if thread_id is None else {"thread_id": thread_id}
    enqueued = list(outbox.enqueue_ready(limit=limit, **scope_kwargs))
    exhausted = list(
        outbox.sweep_exhausted(
            limit=limit,
            max_attempts=max_attempts,
            **scope_kwargs,
        )
    )
    claimed = outbox.claim_ready(
        limit=limit,
        lease_owner_id=lease_owner_id,
        max_attempts=max_attempts,
        lease_seconds=lease_seconds,
        **scope_kwargs,
    )
    completed: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    superseded: list[str] = []
    with ThreadPoolExecutor(max_workers=1) as executor:
        for envelope in claimed:
            try:
                future = executor.submit(
                    runner,
                    envelope.thread_id,
                    envelope.task_ref,
                )
                while True:
                    try:
                        result = future.result(timeout=heartbeat_interval)
                        break
                    except FutureTimeout:
                        outbox.heartbeat(envelope, lease_seconds=lease_seconds)
                if result is None:
                    raise RuntimeError("agent_task_resume_completion_not_ready")
                status = str(result.get("status") or "")
                if status not in {"completed", "completed_with_limits", "failed"}:
                    raise RuntimeError("agent_task_resume_result_invalid")
                outbox.complete(envelope)
                completed.append({"task_ref": envelope.task_ref, "status": status})
            except AgentTaskResumeLeaseLost:
                superseded.append(envelope.task_ref)
            except Exception as exc:
                code = str(getattr(exc, "code", "") or type(exc).__name__)
                try:
                    state = outbox.fail(
                        envelope,
                        error_code=code,
                        max_attempts=max_attempts,
                    )
                    failed.append(
                        {"task_ref": envelope.task_ref, "error_code": code}
                    )
                    if state == "exhausted":
                        exhausted.append(envelope.resume_ref)
                except AgentTaskResumeLeaseLost:
                    superseded.append(envelope.task_ref)
    return {
        "enqueued": enqueued,
        "claimed": [item.task_ref for item in claimed],
        "completed": completed,
        "failed": failed,
        "exhausted": exhausted,
        "superseded": superseded,
    }


def _resume_task(thread_id: str, task_ref: str) -> Mapping[str, Any] | None:
    result = asyncio.run(
        resume_general_agent_task(thread_id=thread_id, task_ref=task_ref)
    )
    if result is None:
        return None
    return {
        "thread_id": result.thread_id,
        "run_id": result.run_id,
        "status": result.status,
        "replayed": result.replayed,
    }


def _envelope(row: Any) -> AgentTaskResumeEnvelope:
    return AgentTaskResumeEnvelope(
        resume_ref=str(_field(row, "resume_ref", 0)),
        thread_id=str(_field(row, "thread_id", 1)),
        task_ref=str(_field(row, "task_ref", 2)),
        attempt_count=int(_field(row, "attempt_count", 3)),
        lease_owner_id=str(_field(row, "lease_owner_id", 4)),
        lease_epoch=int(_field(row, "lease_epoch", 5)),
    )


def _field(row: Any, name: str, index: int) -> Any:
    if isinstance(row, Mapping):
        return row.get(name)
    return row[index]


def _validate_limit(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("agent_task_resume_limit_invalid")


def _validate_exact_text(value: str, code: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(code)


def _validate_optional_thread_id(thread_id: str | None) -> None:
    if thread_id is not None and (
        not isinstance(thread_id, str)
        or not thread_id
        or thread_id != thread_id.strip()
    ):
        raise ValueError("agent_task_resume_thread_invalid")


__all__ = (
    "AgentTaskResumeEnvelope",
    "AgentTaskResumeLeaseLost",
    "PostgresAgentTaskResumeOutbox",
    "process_agent_task_resume_outbox",
)
