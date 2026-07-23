from __future__ import annotations

import io
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from bi_agent.conversation.postgres_store import _run_dispatch_lease_ms
from tools.runtime.recover_run_dispatches import (
    run_runtime_recovery_cycle,
    run_runtime_recovery_worker,
)


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("value", ["0", "-1", "1.5", " 30000", "invalid"])
def test_run_dispatch_lease_configuration_fails_closed(monkeypatch, value: str) -> None:
    monkeypatch.setenv("WAJE_RUN_DISPATCH_LEASE_MS", value)
    with pytest.raises(RuntimeError, match="run_dispatch_lease_configuration_invalid"):
        _run_dispatch_lease_ms()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1.0, 0.0])
def test_recovery_worker_rejects_non_finite_or_non_positive_poll_interval(
    value: float,
) -> None:
    with pytest.raises(ValueError, match="runtime_recovery_poll_interval_invalid"):
        run_runtime_recovery_worker(
            poll_interval_seconds=value,
            once=True,
            cycle_runner=lambda **_: {},
        )


def test_one_runtime_cycle_processes_all_durable_work_sources() -> None:
    connection = SimpleNamespace(close=lambda: None)
    store = SimpleNamespace(connection=connection)

    with (
        patch(
            "tools.runtime.recover_run_dispatches.PostgresConversationStore.from_env",
            return_value=store,
        ),
        patch(
            "tools.runtime.recover_run_dispatches.recover_pending_run_dispatches",
            return_value={"leased": ["bi-run-1"]},
        ) as run_dispatches,
        patch(
            "tools.runtime.recover_run_dispatches.recover_general_agent_turns",
            return_value={"discovered": ["operation-1"]},
        ) as general_turns,
        patch(
            "tools.runtime.recover_run_dispatches.process_agent_task_resume_outbox",
            return_value={"claimed": ["bi-run-2"]},
        ) as task_resumes,
    ):
        summary = run_runtime_recovery_cycle(
            limit=9,
            worker_id="worker-production-1",
        )

    assert summary == {
        "run_dispatches": {"leased": ["bi-run-1"]},
        "general_agent_turns": {"discovered": ["operation-1"]},
        "agent_task_resumes": {"claimed": ["bi-run-2"]},
    }
    run_dispatches.assert_called_once_with(store=store, limit=9)
    general_turns.assert_called_once_with(store=store, limit=9)
    assert task_resumes.call_args.kwargs["limit"] == 9
    assert task_resumes.call_args.kwargs["worker_id"] == "worker-production-1"


def test_scoped_runtime_cycle_never_leases_another_thread() -> None:
    connection = SimpleNamespace(close=lambda: None)
    store = SimpleNamespace(connection=connection)

    with (
        patch(
            "tools.runtime.recover_run_dispatches.PostgresConversationStore.from_env",
            return_value=store,
        ),
        patch(
            "tools.runtime.recover_run_dispatches.recover_pending_run_dispatches",
            return_value={"leased": ["bi-run-scoped"]},
        ) as run_dispatches,
        patch(
            "tools.runtime.recover_run_dispatches.recover_general_agent_turns",
            return_value={"discovered": []},
        ) as general_turns,
        patch(
            "tools.runtime.recover_run_dispatches.process_agent_task_resume_outbox",
            return_value={"claimed": ["bi-run-scoped"]},
        ) as task_resumes,
    ):
        run_runtime_recovery_cycle(
            limit=1,
            worker_id="worker-eval-scoped",
            thread_id="thread-eval-scoped",
        )

    expected_scope = {"thread_id": "thread-eval-scoped"}
    run_dispatches.assert_called_once_with(store=store, limit=1, **expected_scope)
    general_turns.assert_called_once_with(store=store, limit=1, **expected_scope)
    assert task_resumes.call_args.kwargs["thread_id"] == "thread-eval-scoped"


def test_continuous_worker_repeats_cycles_and_stops_gracefully() -> None:
    output = io.StringIO()
    stop = threading.Event()
    calls: list[tuple[int, str]] = []

    def cycle(*, limit: int, worker_id: str):
        calls.append((limit, worker_id))
        if len(calls) == 2:
            stop.set()
        return {"cycle": len(calls)}

    exit_code = run_runtime_recovery_worker(
        limit=4,
        poll_interval_seconds=0.001,
        worker_id="worker-continuous-1",
        stop_event=stop,
        cycle_runner=cycle,
        output=output,
    )

    assert exit_code == 0
    assert calls == [
        (4, "worker-continuous-1"),
        (4, "worker-continuous-1"),
    ]
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [record["summary"] for record in records] == [
        {"cycle": 1},
        {"cycle": 2},
    ]
    assert all(record["status"] == "completed" for record in records)


def test_continuous_worker_survives_a_transient_cycle_failure() -> None:
    output = io.StringIO()
    stop = threading.Event()
    attempts = 0

    def cycle(*, limit: int, worker_id: str):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("database unavailable with sensitive detail")
        stop.set()
        return {"recovered": True}

    exit_code = run_runtime_recovery_worker(
        poll_interval_seconds=0.001,
        worker_id="worker-resilient-1",
        stop_event=stop,
        cycle_runner=cycle,
        output=output,
    )

    assert exit_code == 0
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    assert records[0]["errorCode"] == "runtime_recovery_cycle_failed"
    assert records[0]["errorType"] == "ConnectionError"
    assert "sensitive detail" not in output.getvalue()
    assert records[1]["summary"] == {"recovered": True}


def test_repository_exposes_continuous_worker_as_a_deployable_process() -> None:
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    command = package["scripts"]["worker"]

    assert "tools.runtime.recover_run_dispatches" in command
    assert "--once" not in command
