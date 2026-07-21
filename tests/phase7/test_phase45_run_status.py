from __future__ import annotations

import pytest
from types import SimpleNamespace
from unittest.mock import patch

from bi_agent.conversation.postgres_store import PostgresConversationStore
from bi_agent.conversation.run_status import (
    RUN_STATUS_TRANSITIONS,
    validate_run_status_transition,
    validate_run_status_value,
)
from bi_agent.runtime.evidence_authority import EvidenceIntegrityError
from tests.phase7.test_terminal_run_status import (
    _RunDispatchOwnershipConnection,
)
from tools.runtime.recover_run_dispatches import run_agent_core_dispatch


def _transition(next_status: str) -> str:
    return validate_run_status_transition(
        current_status="running_workflow",
        next_status=next_status,
        current_thread_id="thread-phase45",
        current_turn_id="turn-phase45",
        current_topic_id="topic-phase45",
        next_thread_id="thread-phase45",
        next_turn_id="turn-phase45",
        next_topic_id="topic-phase45",
        current_request={"stop_after_phase": next_status},
        next_request={"post_execution_refs": {"status": next_status}},
    )


@pytest.mark.parametrize("status", ("authority_sealed", "narrative_ready"))
def test_phase45_explicit_stops_are_terminal_run_statuses(status: str) -> None:
    assert status in RUN_STATUS_TRANSITIONS["running_workflow"]
    assert RUN_STATUS_TRANSITIONS[status] == frozenset()
    assert _transition(status) == "transition"


@pytest.mark.parametrize(
    "post_execution_status",
    (
        "delivery_retryable_failed",
        "delivery_permanently_failed",
    ),
)
def test_post_execution_outcomes_do_not_become_analysis_run_statuses(
    post_execution_status: str,
) -> None:
    assert post_execution_status not in RUN_STATUS_TRANSITIONS
    with pytest.raises(
        EvidenceIntegrityError,
        match="^analysis_run_status_invalid$",
    ):
        validate_run_status_value(post_execution_status)


def test_analysis_completion_remains_available_for_orthogonal_publication_states() -> (
    None
):
    assert _transition("completed") == "transition"
    assert RUN_STATUS_TRANSITIONS["completed"] == frozenset()


@pytest.mark.parametrize("status", ("authority_sealed", "narrative_ready"))
def test_postgres_dispatch_terminalizes_for_phase45_stop(status: str) -> None:
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
        status=status,
        request={"post_execution_refs": {"status": status}},
    )

    assert connection.run["status"] == status
    assert connection.dispatch["dispatch_state"] == "terminal"
    assert connection.dispatch["terminal_status"] == status
    assert "run-dispatch" not in store._active_run_dispatches


@pytest.mark.parametrize("status", ("authority_sealed", "narrative_ready"))
def test_recovery_runner_accepts_phase45_terminal_result(status: str) -> None:
    class Core:
        store = SimpleNamespace(connection=SimpleNamespace(close=lambda: None))

        def run_message(self, **kwargs):
            return {"status": status, "run_id": kwargs["run_id"]}

    lease = {
        "dispatch_id": f"dispatch-{status}",
        "run_id": f"run-{status}",
        "thread_id": "thread-phase45",
        "producer_kind": "thread_message",
        "scope_ref": "thread-phase45",
        "request_identity": f"request-{status}",
        "request_payload": {"message": "分析付费金额"},
        "dispatch_owner_id": "recovery-owner-phase45",
        "lease_epoch": 5,
    }
    with patch(
        "tools.runtime.recover_run_dispatches.ConversationAgentCore.from_environment",
        return_value=Core(),
    ):
        result = run_agent_core_dispatch(lease)

    assert result == {"status": status, "run_id": f"run-{status}"}
