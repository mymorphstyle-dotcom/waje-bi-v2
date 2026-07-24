from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from bi_agent.runtime.agent_context import (
    DEFAULT_CONTEXT_COMPACTION_BYTE_THRESHOLD,
    DEFAULT_CONTEXT_COMPACTION_RETENTION,
    DEFAULT_CONTEXT_RECENT_ITEM_LIMIT,
)
from bi_agent.runtime.general_agent_entry import (
    refresh_general_agent_thread_summary,
)


SummaryRefreshRunner = Callable[[str, int], Mapping[str, Any] | None]


@dataclass(frozen=True)
class ThreadSummaryRefreshCandidate:
    thread_id: str
    owner_id: str
    latest_item_sequence: int
    summary_covers_through_sequence: int
    compact_through_sequence: int

    def __post_init__(self) -> None:
        if (
            not self.thread_id
            or self.thread_id != self.thread_id.strip()
            or not self.owner_id
            or self.owner_id != self.owner_id.strip()
            or self.latest_item_sequence < 1
            or self.summary_covers_through_sequence < 0
            or self.compact_through_sequence
            <= self.summary_covers_through_sequence
            or self.compact_through_sequence >= self.latest_item_sequence
        ):
            raise ValueError("thread_summary_refresh_candidate_invalid")


class PostgresThreadSummaryMaintenance:
    """Discovers stale summaries from durable ledger state and serializes refreshes."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def list_stale(
        self,
        *,
        limit: int = 100,
        recent_item_limit: int = DEFAULT_CONTEXT_RECENT_ITEM_LIMIT,
        compaction_retention: int = DEFAULT_CONTEXT_COMPACTION_RETENTION,
        compaction_byte_threshold: int = (
            DEFAULT_CONTEXT_COMPACTION_BYTE_THRESHOLD
        ),
        thread_id: str | None = None,
    ) -> tuple[ThreadSummaryRefreshCandidate, ...]:
        _validate_positive_int(limit, "thread_summary_maintenance_limit_invalid")
        _validate_positive_int(
            recent_item_limit,
            "thread_summary_maintenance_recent_limit_invalid",
        )
        _validate_positive_int(
            compaction_retention,
            "thread_summary_maintenance_retention_invalid",
        )
        _validate_positive_int(
            compaction_byte_threshold,
            "thread_summary_maintenance_byte_threshold_invalid",
        )
        if compaction_retention >= recent_item_limit:
            raise ValueError("thread_summary_maintenance_window_invalid")
        _validate_optional_thread_id(thread_id)
        rows = self.connection.execute(
            """
            SELECT
              thread.thread_id,
              thread.owner_id,
              thread.latest_item_sequence,
              COALESCE(summary.covers_through_sequence, 0)
                AS summary_covers_through_sequence,
              CASE
                WHEN context_size.uncompacted_text_bytes
                     > %(compaction_byte_threshold)s
                  THEN thread.latest_item_sequence - 1
                ELSE thread.latest_item_sequence - %(compaction_retention)s
              END
                AS compact_through_sequence
            FROM waje_runtime.investigation_threads thread
            LEFT JOIN LATERAL (
              SELECT persisted.covers_through_sequence
              FROM waje_runtime.agent_thread_summaries persisted
              WHERE persisted.thread_id = thread.thread_id
              ORDER BY persisted.summary_version DESC
              LIMIT 1
            ) summary ON TRUE
            LEFT JOIN LATERAL (
              SELECT COALESCE(SUM(OCTET_LENGTH(message.text)), 0)
                AS uncompacted_text_bytes
              FROM waje_runtime.conversation_messages message
              WHERE message.thread_id = thread.thread_id
                AND message.item_sequence
                    > COALESCE(summary.covers_through_sequence, 0)
            ) context_size ON TRUE
            WHERE thread.customer_state <> 'working'
              AND (
                %(thread_id)s::text IS NULL
                OR thread.thread_id = %(thread_id)s
              )
              AND (
                (
                  thread.latest_item_sequence
                  - COALESCE(summary.covers_through_sequence, 0)
                ) > %(recent_item_limit)s
                OR context_size.uncompacted_text_bytes
                   > %(compaction_byte_threshold)s
              )
              AND (
                CASE
                  WHEN context_size.uncompacted_text_bytes
                       > %(compaction_byte_threshold)s
                    THEN thread.latest_item_sequence - 1
                  ELSE thread.latest_item_sequence - %(compaction_retention)s
                END
              ) > COALESCE(summary.covers_through_sequence, 0)
            ORDER BY thread.updated_at, thread.thread_id
            LIMIT %(limit)s
            """,
            {
                "limit": limit,
                "thread_id": thread_id,
                "recent_item_limit": recent_item_limit,
                "compaction_retention": compaction_retention,
                "compaction_byte_threshold": compaction_byte_threshold,
            },
        ).fetchall()
        self.connection.commit()
        return tuple(_candidate(row) for row in rows)

    def try_acquire(self, thread_id: str) -> bool:
        _validate_thread_id(thread_id)
        row = self.connection.execute(
            """
            SELECT pg_try_advisory_lock(
              hashtextextended(
                'thread-summary-refresh:' || %(thread_id)s::text,
                0
              )
            ) AS acquired
            """,
            {"thread_id": thread_id},
        ).fetchone()
        self.connection.commit()
        return bool(_field(row, "acquired", 0)) if row is not None else False

    def release(self, thread_id: str) -> None:
        _validate_thread_id(thread_id)
        row = self.connection.execute(
            """
            SELECT pg_advisory_unlock(
              hashtextextended(
                'thread-summary-refresh:' || %(thread_id)s::text,
                0
              )
            ) AS released
            """,
            {"thread_id": thread_id},
        ).fetchone()
        if row is None or not bool(_field(row, "released", 0)):
            self.connection.rollback()
            raise RuntimeError("thread_summary_maintenance_lock_not_owned")
        self.connection.commit()


def process_stale_thread_summaries(
    *,
    maintenance: PostgresThreadSummaryMaintenance,
    refresh_runner: SummaryRefreshRunner | None = None,
    limit: int = 100,
    thread_id: str | None = None,
) -> dict[str, list[Any]]:
    _validate_positive_int(limit, "thread_summary_maintenance_limit_invalid")
    _validate_optional_thread_id(thread_id)
    runner = refresh_runner or _refresh_thread_summary
    scope = {} if thread_id is None else {"thread_id": thread_id}
    candidates = maintenance.list_stale(limit=limit, **scope)
    completed: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    superseded: list[str] = []
    contended: list[str] = []
    for candidate in candidates:
        if not maintenance.try_acquire(candidate.thread_id):
            contended.append(candidate.thread_id)
            continue
        try:
            try:
                result = runner(
                    candidate.thread_id,
                    candidate.compact_through_sequence,
                )
                if result is None:
                    superseded.append(candidate.thread_id)
                    continue
                summary_ref = result.get("summary_ref")
                covers_through = result.get("covers_through_sequence")
                if (
                    not isinstance(summary_ref, str)
                    or not summary_ref
                    or isinstance(covers_through, bool)
                    or not isinstance(covers_through, int)
                    or covers_through < candidate.compact_through_sequence
                ):
                    raise RuntimeError("thread_summary_refresh_result_invalid")
                completed.append(
                    {
                        "thread_id": candidate.thread_id,
                        "summary_ref": summary_ref,
                        "covers_through_sequence": covers_through,
                    }
                )
            except Exception as exc:
                failed.append(
                    {
                        "thread_id": candidate.thread_id,
                        "error_code": str(
                            getattr(exc, "code", "") or type(exc).__name__
                        ),
                    }
                )
        finally:
            maintenance.release(candidate.thread_id)
    return {
        "eligible": [item.thread_id for item in candidates],
        "completed": completed,
        "failed": failed,
        "superseded": superseded,
        "contended": contended,
    }


def _refresh_thread_summary(
    thread_id: str,
    compact_through_sequence: int,
) -> Mapping[str, Any] | None:
    summary = asyncio.run(
        refresh_general_agent_thread_summary(
            thread_id=thread_id,
            compact_through_sequence=compact_through_sequence,
        )
    )
    if summary is None:
        return None
    return {
        "summary_ref": summary.summary_ref,
        "covers_through_sequence": summary.covers_through_sequence,
    }


def _candidate(row: Any) -> ThreadSummaryRefreshCandidate:
    return ThreadSummaryRefreshCandidate(
        thread_id=str(_field(row, "thread_id", 0)),
        owner_id=str(_field(row, "owner_id", 1)),
        latest_item_sequence=int(_field(row, "latest_item_sequence", 2)),
        summary_covers_through_sequence=int(
            _field(row, "summary_covers_through_sequence", 3)
        ),
        compact_through_sequence=int(
            _field(row, "compact_through_sequence", 4)
        ),
    )


def _field(row: Any, name: str, index: int) -> Any:
    if isinstance(row, Mapping):
        return row.get(name)
    return row[index]


def _validate_positive_int(value: int, code: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(code)


def _validate_thread_id(thread_id: str) -> None:
    if (
        not isinstance(thread_id, str)
        or not thread_id
        or thread_id != thread_id.strip()
    ):
        raise ValueError("thread_summary_maintenance_thread_invalid")


def _validate_optional_thread_id(thread_id: str | None) -> None:
    if thread_id is not None:
        _validate_thread_id(thread_id)


__all__ = (
    "PostgresThreadSummaryMaintenance",
    "ThreadSummaryRefreshCandidate",
    "process_stale_thread_summaries",
)
