from __future__ import annotations

import json
from threading import Event
from types import SimpleNamespace

import pytest

import bi_agent.conversation.agent_core as agent_core
from bi_agent.conversation.postgres_store import PostgresConversationStore
from bi_agent.runtime.evidence_authority import (
    EvidenceIntegrityError,
    canonical_digest,
)
from bi_agent.runtime.single_authority import (
    DurableTransition,
    InteractionDirective,
    LifecycleState,
)


class _Cursor:
    def __init__(self, rows=()):
        self.rows = tuple(rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class _AttemptJournal:
    def __init__(self) -> None:
        self.bindings: list[dict] = []

    def bind_stage(self, **kwargs) -> None:
        self.bindings.append(kwargs)


class _DirectiveTerminalConnection:
    def __init__(self, *, producer_kind: str = "clarification_resolution"):
        self.source = {
            "run_id": "run-source",
            "run_attempt_id": "run-source",
            "thread_id": "thread-source",
            "turn_id": "turn-source",
            "topic_id": "topic-source",
            "status": "waiting_for_clarification",
            "request": {
                "runtime_descriptors": {
                    "context_manifest": {"manifest_id": "manifest-source"}
                }
            },
        }
        self.pending = {
            "pending_clarification_topic_id": "topic-source",
            "pending_clarification_id": "run-source",
        }
        self.dispatch = {
            "dispatch_id": "dispatch-source",
            "run_id": "run-source",
            "thread_id": "thread-source",
            "dispatch_state": "running",
            "owner_id": "owner-source",
            "lease_epoch": 3,
            "lease_active": True,
            "producer_kind": producer_kind,
            "scope_ref": "run-source",
            "terminal_status": None,
        }
        self.lifecycle = LifecycleState.create(
            run_attempt_id="run-source",
            execution_state="waiting",
            interaction_state="waiting_for_user",
        ).to_dict()
        self.directive_payload: dict | None = None
        self.audit_events: list[dict] = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, statement, params=None):
        params = dict(params or {})
        if "pg_advisory_xact_lock" in statement:
            return _Cursor(({"locked": True},))
        if "generic_run_dispatch_owner_lock" in statement:
            return _Cursor((dict(self.dispatch),))
        if (
            "SELECT run_id, run_attempt_id" in statement
            and "FROM waje_runtime.analysis_runs" in statement
        ):
            return _Cursor((dict(self.source),))
        if "INSERT INTO waje_runtime.interaction_directives" in statement:
            self.directive_payload = json.loads(params["payload"])
            return _Cursor(({"directive_id": params["directive_id"]},))
        if (
            "SELECT payload" in statement
            and "FROM waje_runtime.interaction_directives" in statement
        ):
            return _Cursor(({"payload": self.directive_payload},))
        if "control_directive_source_lock" in statement:
            return _Cursor((dict(self.source),))
        if "control_directive_pending_clarification_lock" in statement:
            return _Cursor((dict(self.pending),))
        if "FROM waje_runtime.run_lifecycle_state_revisions" in statement:
            if (
                "state_revision = %(state_revision)s" in statement
                and self.lifecycle["state_revision"] != params["state_revision"]
            ):
                return _Cursor()
            return _Cursor(({"payload": self.lifecycle},))
        if "INSERT INTO waje_runtime.run_lifecycle_state_revisions" in statement:
            self.lifecycle = json.loads(params["payload"])
            return _Cursor(({"state_revision": self.lifecycle["state_revision"]},))
        if "cancellation_source_complete_cas" in statement:
            if self.source["status"] != "waiting_for_clarification":
                return _Cursor()
            self.source["status"] = "interaction_completed"
            self.source["request"] = json.loads(params["request"])
            return _Cursor(({"run_id": self.source["run_id"]},))
        if "cancellation_pending_clarification_clear_cas" in statement:
            if self.pending["pending_clarification_id"] != params["run_id"]:
                return _Cursor()
            self.pending = {
                "pending_clarification_topic_id": None,
                "pending_clarification_id": "",
            }
            return _Cursor(({"thread_id": params["thread_id"]},))
        if "owned_run_dispatch_terminal_cas" in statement:
            if (
                self.dispatch["dispatch_id"] != params["dispatch_id"]
                or self.dispatch["dispatch_state"] != "running"
                or self.dispatch["owner_id"] != params["owner_id"]
                or self.dispatch["lease_epoch"] != params["lease_epoch"]
            ):
                return _Cursor()
            self.dispatch["dispatch_state"] = "terminal"
            self.dispatch["terminal_status"] = params["status"]
            return _Cursor(({"dispatch_state": "terminal"},))
        if "INSERT INTO waje_runtime.audit_events" in statement:
            self.audit_events.append(
                {**params, "payload": json.loads(params["payload"])}
            )
            return _Cursor()
        raise AssertionError(statement)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _directive(kind: str) -> InteractionDirective:
    return InteractionDirective.create(
        run_attempt_id="run-source",
        intent_revision_id="intent-source",
        kind=kind,
        target_refs=("intent-source",) if kind == "challenge" else (),
        original_user_text=(
            "这个基线选择有问题" if kind == "challenge" else "取消当前分析"
        ),
    )


def _transition(directive: InteractionDirective) -> DurableTransition:
    return DurableTransition.create(
        node_name="bind_free_text_directive",
        parent_transition_id="transition-waiting",
        run_attempt_id=directive.run_attempt_id,
        intent_revision_id=directive.intent_revision_id,
        decision_ledger_position=0,
        input_digest=canonical_digest({"directive": directive.directive_id}),
        output_digest=canonical_digest({"kind": directive.kind}),
        execution_attempt=1,
        provider_ref="provider-test",
        model_ref="model-test",
        status="succeeded",
        acceptance_state="accepted",
        next_transition=(
            "cancelled" if directive.kind == "cancel" else "repair_scope_pending"
        ),
    )


def _save_directive(kind: str, *, producer_kind="clarification_resolution"):
    connection = _DirectiveTerminalConnection(producer_kind=producer_kind)
    store = PostgresConversationStore(connection)
    store.attempt_journal = _AttemptJournal()
    store._active_run_dispatches["run-source"] = (
        "dispatch-source",
        "owner-source",
        3,
    )
    heartbeat_stop = Event()
    store._run_dispatch_heartbeat_stops["dispatch-source"] = heartbeat_stop
    store.resolve_active_intent_revision = lambda _run_id: SimpleNamespace(
        intent_revision_id="intent-source"
    )
    store._save_transition_attempt_locked = lambda **_kwargs: "inserted"
    directive = _directive(kind)
    transition = _transition(directive)
    result = store.save_interaction_directive_transition(
        directive=directive,
        transition=transition,
        input_payload={"directive": directive.directive_id},
        output_payload={"kind": directive.kind},
        accepted_attempt_refs=("provider-attempt-1",),
    )
    return connection, store, heartbeat_stop, directive, result


def test_cancel_atomically_closes_waiting_run_and_exact_resolution_dispatch():
    connection, store, heartbeat_stop, directive, result = _save_directive("cancel")

    assert connection.source["status"] == "interaction_completed"
    assert connection.source["request"]["runtime_descriptors"] == {
        "context_manifest": {"manifest_id": "manifest-source"}
    }
    assert connection.source["request"]["interaction_result"] == {
        "schema_version": "typed-interaction.v1",
        "intent": "analysis_cancellation",
        "response_text": "已取消当前分析。",
    }
    assert connection.pending == {
        "pending_clarification_topic_id": None,
        "pending_clarification_id": "",
    }
    assert connection.dispatch["dispatch_state"] == "terminal"
    assert connection.dispatch["terminal_status"] == "interaction_completed"
    assert connection.lifecycle["execution_state"] == "cancelled"
    assert connection.lifecycle["interaction_state"] == "closed"
    assert connection.lifecycle["publication_state"] == "not_ready"
    assert connection.lifecycle["cancellation_state"] == "cancelled"
    assert heartbeat_stop.is_set()
    assert "run-source" not in store._active_run_dispatches
    assert result["directive"] == directive.to_dict()
    assert result["source_terminal"] == {
        "status": "interaction_completed",
        "run_id": "run-source",
        "turn_id": "turn-source",
        "topic_id": "topic-source",
        "intent": "analysis_cancellation",
        "topic_relation": "analysis_cancellation",
        "context_manifest": {"manifest_id": "manifest-source"},
        "interaction_result": {
            "schema_version": "typed-interaction.v1",
            "intent": "analysis_cancellation",
            "response_text": "已取消当前分析。",
        },
    }
    assert {event["event_type"] for event in connection.audit_events} == {
        "single_authority_run_cancelled",
        "run_status_changed",
        "clarification_cleared",
        "run_dispatch_completed",
    }


def test_challenge_terminals_only_the_resolution_command_and_keeps_waiting():
    connection, store, heartbeat_stop, directive, result = _save_directive("challenge")

    assert connection.source["status"] == "waiting_for_clarification"
    assert connection.pending == {
        "pending_clarification_topic_id": "topic-source",
        "pending_clarification_id": "run-source",
    }
    assert connection.dispatch["dispatch_state"] == "terminal"
    assert connection.dispatch["terminal_status"] == ("waiting_for_clarification")
    assert connection.lifecycle["execution_state"] == "waiting"
    assert connection.lifecycle["interaction_state"] == "waiting_for_user"
    assert connection.lifecycle["cancellation_state"] == "active"
    assert heartbeat_stop.is_set()
    assert "run-source" not in store._active_run_dispatches
    assert result["directive"] == directive.to_dict()
    assert result["source_waiting"] == {
        "status": "waiting_for_clarification",
        "run_id": "run-source",
        "turn_id": "turn-source",
        "topic_id": "topic-source",
    }
    assert {event["event_type"] for event in connection.audit_events} == {
        "single_authority_challenge_recorded",
        "run_dispatch_completed",
    }


def test_control_directive_rejects_a_non_clarification_dispatch():
    with pytest.raises(
        EvidenceIntegrityError,
        match="^run_dispatch_owner_lost$",
    ):
        _save_directive("cancel", producer_kind="thread_message")


def test_cancel_submission_returns_the_typed_terminal_to_agent_core(monkeypatch):
    directive = _directive("cancel")
    source_terminal = {
        "status": "interaction_completed",
        "run_id": "run-source",
        "turn_id": "turn-source",
        "topic_id": "topic-source",
        "intent": "analysis_cancellation",
        "topic_relation": "analysis_cancellation",
        "context_manifest": {"manifest_id": "manifest-source"},
        "interaction_result": {
            "schema_version": "typed-interaction.v1",
            "intent": "analysis_cancellation",
            "response_text": "已取消当前分析。",
        },
    }
    accepted = {
        "status": "run_cancelled",
        "directive": directive.to_dict(),
        "durable_checkpoint": {"transition_id": "transition-cancel"},
        "replayed": False,
        "source_terminal": source_terminal,
    }
    monkeypatch.setattr(
        agent_core,
        "_bind_single_authority_free_text",
        lambda **_kwargs: (
            accepted,
            {"binding_kind": "cancel"},
            {"provider": "provider-test"},
        ),
    )

    class _Store:
        def __init__(self):
            self.audit_events = []

        def get_run_state(self, _run_id):
            return {
                "thread_id": "thread-source",
                "turn_id": "turn-source",
                "topic_id": "topic-source",
                "status": "waiting_for_clarification",
            }

        def resolve_active_intent_revision(self, _run_id):
            return SimpleNamespace(intent_revision_id="intent-source")

        def add_audit_event(self, event_type, **kwargs):
            self.audit_events.append((event_type, kwargs))

    store = _Store()
    result = agent_core._record_single_authority_clarification_submission(
        store=store,
        llm_client=object(),
        thread_id="thread-source",
        run_id="run-source",
        user_message="取消当前分析",
        clarification={"selectedOptionId": None},
    )

    assert result["status"] == "interaction_completed"
    assert result["intent"] == "analysis_cancellation"
    assert result["interaction_result"] == source_terminal["interaction_result"]
    assert "source_terminal" not in result
    assert result["directive"] == directive.to_dict()
    assert store.audit_events[0][0] == "single_authority_directive_recorded"
