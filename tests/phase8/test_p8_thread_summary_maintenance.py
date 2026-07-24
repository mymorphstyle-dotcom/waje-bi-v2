from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from bi_agent.runtime.thread_summary_maintenance import (
    PostgresThreadSummaryMaintenance,
    ThreadSummaryRefreshCandidate,
    process_stale_thread_summaries,
)


class _Rows:
    def __init__(self, values: list[Any]) -> None:
        self._values = values

    def fetchall(self) -> list[Any]:
        return list(self._values)

    def fetchone(self) -> Any | None:
        return self._values[0] if self._values else None


class _Connection:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.params: list[dict[str, Any]] = []
        self.commits = 0
        self.rollbacks = 0

    def execute(
        self,
        statement: str,
        params: Mapping[str, Any] | None = None,
    ) -> _Rows:
        self.statements.append(statement)
        self.params.append(dict(params or {}))
        if "FROM waje_runtime.investigation_threads" in statement:
            return _Rows(
                [
                    {
                        "thread_id": "thread-stale",
                        "owner_id": "owner-1",
                        "latest_item_sequence": 73,
                        "summary_covers_through_sequence": 12,
                        "compact_through_sequence": 61,
                    }
                ]
            )
        if "pg_try_advisory_lock" in statement:
            return _Rows([{"acquired": True}])
        if "pg_advisory_unlock" in statement:
            return _Rows([{"released": True}])
        raise AssertionError(statement)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_stale_summary_discovery_is_derived_from_durable_thread_state() -> None:
    connection = _Connection()
    maintenance = PostgresThreadSummaryMaintenance(connection)

    candidates = maintenance.list_stale(limit=7, thread_id="thread-stale")

    assert candidates == (
        ThreadSummaryRefreshCandidate(
            thread_id="thread-stale",
            owner_id="owner-1",
            latest_item_sequence=73,
            summary_covers_through_sequence=12,
            compact_through_sequence=61,
        ),
    )
    statement = connection.statements[0]
    assert "thread.customer_state <> 'working'" in statement
    assert "waje_runtime.agent_thread_summaries" in statement
    assert connection.params[0] == {
        "limit": 7,
        "thread_id": "thread-stale",
        "recent_item_limit": 40,
        "compaction_retention": 12,
        "compaction_byte_threshold": 65_536,
    }


class _Maintenance:
    def __init__(self) -> None:
        self.candidates = (
            ThreadSummaryRefreshCandidate(
                thread_id="thread-complete",
                owner_id="owner-1",
                latest_item_sequence=73,
                summary_covers_through_sequence=0,
                compact_through_sequence=61,
            ),
            ThreadSummaryRefreshCandidate(
                thread_id="thread-failed",
                owner_id="owner-2",
                latest_item_sequence=50,
                summary_covers_through_sequence=0,
                compact_through_sequence=38,
            ),
        )
        self.released: list[str] = []

    def list_stale(self, **_kwargs: Any):
        return self.candidates

    def try_acquire(self, _thread_id: str) -> bool:
        return True

    def release(self, thread_id: str) -> None:
        self.released.append(thread_id)


def test_background_summary_failure_is_observed_without_affecting_other_work() -> None:
    maintenance = _Maintenance()

    def refresh(thread_id: str, compact_through_sequence: int):
        if thread_id == "thread-failed":
            raise RuntimeError("provider unavailable")
        return {
            "summary_ref": "thread-summary:sha256:" + "a" * 64,
            "covers_through_sequence": compact_through_sequence,
        }

    result = process_stale_thread_summaries(
        maintenance=maintenance,  # type: ignore[arg-type]
        refresh_runner=refresh,
        limit=2,
    )

    assert result["completed"] == [
        {
            "thread_id": "thread-complete",
            "summary_ref": "thread-summary:sha256:" + "a" * 64,
            "covers_through_sequence": 61,
        }
    ]
    assert result["failed"] == [
        {"thread_id": "thread-failed", "error_code": "RuntimeError"}
    ]
    assert maintenance.released == ["thread-complete", "thread-failed"]
