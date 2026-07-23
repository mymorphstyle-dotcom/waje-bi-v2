from __future__ import annotations

import argparse
import json
from typing import Any

from bi_agent.conversation.postgres_store import PostgresConversationStore
from bi_agent.runtime.agents_sdk_trace import DEFAULT_AGENT_TRACE_STORAGE_POLICY


def prune_agent_traces(
    connection: Any,
    *,
    retention_days: int = DEFAULT_AGENT_TRACE_STORAGE_POLICY.retention_days,
) -> dict[str, int]:
    if isinstance(retention_days, bool) or retention_days < 1:
        raise ValueError("agent_trace_retention_days_invalid")
    rows = connection.execute(
        """
        DELETE FROM waje_runtime.audit_events trace
        WHERE trace.event_type IN (
            'agents_sdk_trace_recorded',
            'agents_sdk_trace_record_rejected'
          )
          AND (
            trace.created_at < now() - make_interval(days => %(retention_days)s)
            OR NOT EXISTS (
              SELECT 1
              FROM waje_runtime.investigation_threads thread
              WHERE thread.thread_id = trace.thread_id
            )
          )
        RETURNING trace.event_type
        """,
        {"retention_days": retention_days},
    ).fetchall()
    connection.commit()
    recorded = sum(
        1 for row in rows if _field(row, "event_type", 0) == "agents_sdk_trace_recorded"
    )
    rejected = len(rows) - recorded
    return {"deleted": len(rows), "recorded": recorded, "rejected": rejected}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--retention-days",
        type=int,
        default=DEFAULT_AGENT_TRACE_STORAGE_POLICY.retention_days,
    )
    args = parser.parse_args(argv)
    store = PostgresConversationStore.from_env()
    try:
        summary = prune_agent_traces(
            store.connection,
            retention_days=args.retention_days,
        )
    finally:
        store.connection.close()
    print(json.dumps(summary, sort_keys=True))
    return 0


def _field(row: Any, name: str, index: int) -> Any:
    if isinstance(row, dict):
        return row.get(name)
    try:
        return row[name]
    except (KeyError, TypeError, IndexError):
        return row[index]


if __name__ == "__main__":
    raise SystemExit(main())
