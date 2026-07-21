from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from bi_agent.conversation.postgres_store import PostgresConversationStore
from bi_agent.conversation.run_status import (
    RUN_STATUS_TRANSITIONS,
    validate_run_status_transition,
)
from bi_agent.runtime.evidence_authority import EvidenceIntegrityError
from tests.phase7.test_terminal_run_status import (
    _RunDispatchOwnershipConnection,
)
from tools.runtime.recover_run_dispatches import (
    recover_pending_run_dispatches,
    run_agent_core_dispatch,
)


def _validate_transition(current_status: str, next_status: str) -> str:
    return validate_run_status_transition(
        current_status=current_status,
        next_status=next_status,
        current_thread_id="thread-plan",
        current_turn_id="turn-plan",
        current_topic_id="topic-plan",
        next_thread_id="thread-plan",
        next_turn_id="turn-plan",
        next_topic_id="topic-plan",
        current_request={"phase": "workflow"},
        next_request={"phase": "plan-bound"},
    )


def test_planned_is_a_declared_terminal_after_running_workflow() -> None:
    assert "planned" in RUN_STATUS_TRANSITIONS["running_workflow"]
    assert RUN_STATUS_TRANSITIONS["planned"] == frozenset()
    assert _validate_transition("running_workflow", "planned") == "transition"

    with pytest.raises(
        EvidenceIntegrityError,
        match="^analysis_run_status_transition_conflict$",
    ):
        _validate_transition("planned", "failed")


def test_waiting_clarification_resumes_same_run_workflow() -> None:
    assert RUN_STATUS_TRANSITIONS["waiting_for_clarification"] == frozenset(
        {"running_workflow", "interaction_completed"}
    )
    assert (
        _validate_transition("waiting_for_clarification", "running_workflow")
        == "transition"
    )
    assert (
        _validate_transition("waiting_for_clarification", "interaction_completed")
        == "transition"
    )


def test_owned_postgres_dispatch_terminalizes_when_phase02_is_planned() -> None:
    connection = _RunDispatchOwnershipConnection(
        dispatch_state="running",
        run_status="running_workflow",
    )
    store = PostgresConversationStore(connection)
    store._active_run_dispatches["run-dispatch"] = (
        "dispatch-1",
        "owner-current",
        4,
    )

    store.upsert_run(
        "run-dispatch",
        thread_id="thread-dispatch",
        status="planned",
        request={"plan_result_refs": {"plan_revision_id": "plan-revision-1"}},
    )

    assert connection.run["status"] == "planned"
    assert connection.dispatch["dispatch_state"] == "terminal"
    assert connection.dispatch["terminal_status"] == "planned"
    assert "run-dispatch" not in store._active_run_dispatches


def test_recovery_failure_replays_durable_planned_authority() -> None:
    connection = _RunDispatchOwnershipConnection(
        owner_id="recovery-owner-planned",
        lease_epoch=7,
        dispatch_state="terminal",
        run_status="planned",
        lease_active=False,
    )
    connection.dispatch["terminal_status"] = "planned"
    before_run = deepcopy(connection.run)
    before_dispatch = deepcopy(connection.dispatch)

    durable = PostgresConversationStore(connection).fail_owned_run_dispatch(
        dispatch_id="dispatch-1",
        run_id="run-dispatch",
        thread_id="thread-dispatch",
        dispatch_owner_id="recovery-owner-planned",
        lease_epoch=7,
        failure_reason="run_dispatch_recovery_worker_failed",
    )

    assert durable["status"] == "planned"
    assert connection.run == before_run
    assert connection.dispatch == before_dispatch


def test_recovery_driver_preserves_planned_after_response_failure() -> None:
    lease = {
        "dispatch_id": "dispatch-plan-response-failure",
        "run_id": "run-planned-response-failure",
        "thread_id": "thread-plan",
        "producer_kind": "thread_message",
        "scope_ref": "thread-plan",
        "request_identity": "request-plan",
        "request_payload": {"message": "规划付费金额变化分析"},
        "dispatch_owner_id": "recovery-owner-plan",
        "lease_epoch": 3,
    }

    class Store:
        def sweep_expired_run_dispatches(self, *, limit):
            return ()

        def lease_recoverable_run_dispatches(self, *, limit):
            return (lease,)

        def fail_owned_run_dispatch(self, **_kwargs):
            return {"status": "planned"}

    def fail_after_commit(_dispatch):
        raise RuntimeError("response serialization failed after plan commit")

    summary = recover_pending_run_dispatches(
        store=Store(),
        dispatch_runner=fail_after_commit,
        limit=1,
    )

    assert summary["dispatched"] == [
        {
            "run_id": "run-planned-response-failure",
            "status": "planned",
        }
    ]
    assert summary["failed"] == []


def test_recovery_runner_accepts_agent_core_planned_result() -> None:
    class Core:
        store = SimpleNamespace(connection=SimpleNamespace(close=lambda: None))

        def run_message(self, **kwargs):
            return {"status": "planned", "run_id": kwargs["run_id"]}

    lease = {
        "dispatch_id": "dispatch-planned",
        "run_id": "run-planned",
        "thread_id": "thread-planned",
        "producer_kind": "thread_message",
        "scope_ref": "thread-planned",
        "request_identity": "request-planned",
        "request_payload": {"message": "规划付费金额变化分析"},
        "dispatch_owner_id": "recovery-owner-planned",
        "lease_epoch": 5,
    }
    with patch(
        "tools.runtime.recover_run_dispatches.ConversationAgentCore.from_environment",
        return_value=Core(),
    ):
        result = run_agent_core_dispatch(lease)

    assert result == {"status": "planned", "run_id": "run-planned"}
