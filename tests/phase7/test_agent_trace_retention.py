from pathlib import Path

import pytest

from tools.runtime.prune_agent_traces import prune_agent_traces


ROOT = Path(__file__).resolve().parents[2]


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self) -> None:
        self.statement = ""
        self.params = {}
        self.commits = 0

    def execute(self, statement, params):
        self.statement = statement
        self.params = params
        return _Result(
            [
                {"event_type": "agents_sdk_trace_recorded"},
                {"event_type": "agents_sdk_trace_record_rejected"},
            ]
        )

    def commit(self):
        self.commits += 1


def test_trace_pruner_deletes_only_expired_or_orphaned_sdk_trace_records() -> None:
    connection = _Connection()

    result = prune_agent_traces(connection, retention_days=30)

    assert result == {"deleted": 2, "recorded": 1, "rejected": 1}
    assert connection.params == {"retention_days": 30}
    assert "trace.created_at < now() - make_interval" in connection.statement
    assert "NOT EXISTS" in connection.statement
    assert "waje_runtime.investigation_threads" in connection.statement
    assert "agents_sdk_trace_recorded" in connection.statement
    assert "agents_sdk_trace_record_rejected" in connection.statement
    assert connection.commits == 1


def test_trace_pruner_rejects_invalid_retention_without_deleting() -> None:
    connection = _Connection()
    with pytest.raises(ValueError, match="agent_trace_retention_days_invalid"):
        prune_agent_traces(connection, retention_days=0)
    assert connection.statement == ""
    assert connection.commits == 0


def test_schema_deletes_trace_on_thread_deletion_and_has_prune_index() -> None:
    schema = (ROOT / "tools/runtime/conversation-runtime.sql").read_text(
        encoding="utf-8"
    )
    assert "idx_audit_events_general_agent_trace" in schema
    assert "ADD CONSTRAINT audit_events_thread_id_fkey" in schema
    assert "REFERENCES waje_runtime.investigation_threads(thread_id)" in schema
    assert "ON DELETE CASCADE" in schema
    assert "ON DELETE CASCADE\n  NOT VALID" in schema
    assert "BEFORE DELETE ON waje_runtime.investigation_threads" not in schema
