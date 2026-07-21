from __future__ import annotations

from bi_agent.conversation.run_status import (
    RUN_STATUS_TRANSITIONS,
    validate_run_status_transition,
)


def _transition(current_status: str, next_status: str):
    return validate_run_status_transition(
        current_status=current_status,
        next_status=next_status,
        current_thread_id="thread-phase03",
        current_turn_id="turn-phase03",
        current_topic_id="topic-phase03",
        next_thread_id="thread-phase03",
        next_turn_id="turn-phase03",
        next_topic_id="topic-phase03",
        current_request={"question": "paid amount"},
        next_request={"question": "paid amount"},
    )


def test_evidence_ready_is_a_terminal_phase03_status_after_running_workflow():
    assert "evidence_ready" in RUN_STATUS_TRANSITIONS["running_workflow"]
    assert RUN_STATUS_TRANSITIONS["evidence_ready"] == frozenset()
    assert _transition("running_workflow", "evidence_ready") == "transition"


def test_planned_remains_an_explicit_phase02_stop_status():
    assert "planned" in RUN_STATUS_TRANSITIONS["running_workflow"]
    assert RUN_STATUS_TRANSITIONS["planned"] == frozenset()
