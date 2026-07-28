from types import SimpleNamespace

from bi_agent.runtime.controlled_investigation_workflow import (
    _parallel_connection_dsn,
)


def test_parallel_child_connections_use_configured_runtime_dsn(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "WAJE_RUNTIME_DATABASE_URL",
        "postgresql://runtime-user:secret@db.example/waje",
    )
    connection = SimpleNamespace(
        info=SimpleNamespace(
            dsn="user=runtime-user dbname=waje host=db.example",
        ),
    )

    assert _parallel_connection_dsn(connection) == (
        "postgresql://runtime-user:secret@db.example/waje"
    )


def test_parallel_child_connections_fall_back_to_connection_dsn(
    monkeypatch,
) -> None:
    monkeypatch.delenv("WAJE_RUNTIME_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    connection = SimpleNamespace(
        info=SimpleNamespace(dsn="dbname=waje host=/tmp"),
    )

    assert _parallel_connection_dsn(connection) == "dbname=waje host=/tmp"
