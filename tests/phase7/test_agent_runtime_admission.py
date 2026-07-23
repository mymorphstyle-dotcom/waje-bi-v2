from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from bi_agent.runtime.agent_runtime_admission import (
    AgentRuntimeAdmissionError,
    PostgresAgentRuntimeAdmissionLease,
)


ROOT = Path(__file__).resolve().parents[2]


class Result:
    def __init__(self, row: Mapping[str, Any] | tuple[Any, ...] | None) -> None:
        self.row = row

    def fetchone(self):
        return self.row


class AdmissionConnection:
    def __init__(self, acquisitions: list[bool]) -> None:
        self.acquisitions = acquisitions
        self.statements: list[tuple[str, Mapping[str, Any]]] = []

    def execute(self, statement: str, params: Mapping[str, Any]):
        self.statements.append((statement, params))
        if "pg_try_advisory_lock" in statement:
            return Result({"acquired": self.acquisitions.pop(0)})
        if "pg_advisory_unlock" in statement:
            return Result((True,))
        raise AssertionError(statement)


def test_admission_acquires_global_and_actor_slots_and_releases_both() -> None:
    connection = AdmissionConnection([False, True, True])
    lease = PostgresAgentRuntimeAdmissionLease.acquire(
        connection=connection,
        actor_id="actor-1",
        environ={
            "WAJE_AGENT_MAX_PROCESSES": "2",
            "WAJE_AGENT_MAX_PROCESSES_PER_ACTOR": "1",
        },
    )

    assert lease.global_slot == 2
    assert lease.actor_slot == 1
    lease.release()
    lease.release()
    unlocks = [sql for sql, _ in connection.statements if "pg_advisory_unlock" in sql]
    assert len(unlocks) == 2


def test_actor_capacity_failure_releases_global_slot() -> None:
    connection = AdmissionConnection([True, False])
    with pytest.raises(
        AgentRuntimeAdmissionError,
        match="agent_runtime_actor_capacity_exceeded",
    ):
        PostgresAgentRuntimeAdmissionLease.acquire(
            connection=connection,
            actor_id="actor-1",
            environ={
                "WAJE_AGENT_MAX_PROCESSES": "1",
                "WAJE_AGENT_MAX_PROCESSES_PER_ACTOR": "1",
            },
        )
    assert any("pg_advisory_unlock" in sql for sql, _ in connection.statements)


def test_invalid_admission_configuration_fails_closed() -> None:
    with pytest.raises(
        AgentRuntimeAdmissionError,
        match="agent_runtime_admission_config_invalid",
    ):
        PostgresAgentRuntimeAdmissionLease.acquire(
            connection=AdmissionConnection([]),
            actor_id="actor-1",
            environ={"WAJE_AGENT_MAX_PROCESSES": "0"},
        )


def test_runtime_budget_contract_covers_process_tool_turn_token_and_io() -> None:
    entry = (ROOT / "bi_agent/runtime/general_agent_entry.py").read_text(
        encoding="utf-8"
    )
    runtime = (ROOT / "bi_agent/runtime/agent_turn_runtime.py").read_text(
        encoding="utf-8"
    )
    discovery = (ROOT / "bi_agent/runtime/agent_tool_discovery.py").read_text(
        encoding="utf-8"
    )
    gateway = (ROOT / "app/api/_generalAgent.ts").read_text(encoding="utf-8")
    assert "PostgresAgentRuntimeAdmissionLease.acquire" in entry
    assert "context_token_budget=_context_token_budget(provider)" in entry
    assert "max_turns: int = 10" in runtime
    assert "max_optional_tools: int = 4" in discovery
    assert "GENERAL_AGENT_OUTPUT_MAX_BYTES" in gateway
    assert "GENERAL_AGENT_STARTUP_MAX_BYTES" in gateway


def test_process_contracts_use_stdin_and_no_business_data_argv() -> None:
    general = (ROOT / "app/api/_generalAgent.ts").read_text(encoding="utf-8")
    core = (ROOT / "app/api/_agentCore.ts").read_text(encoding="utf-8")
    for source in (general, core):
        assert "commandInput.end(commandJson)" in source
        assert 'stdio: ["pipe", "ignore", "ignore", "pipe"]' in source
        assert '"--message"' not in source
        assert '"--user-id"' not in source
        assert '"--command-json"' not in source
