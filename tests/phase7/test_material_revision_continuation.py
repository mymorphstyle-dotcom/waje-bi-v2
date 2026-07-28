from __future__ import annotations

from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from bi_agent.conversation.material_revision_continuation import (
    MaterialRevisionContinuation,
    MaterialRevisionContinuationError,
)
from bi_agent.conversation.agent_core import ConversationAgentCore
from bi_agent.conversation.postgres_store import PostgresConversationStore
from bi_agent.runtime.evidence_authority import canonical_digest
from bi_agent.runtime.single_authority import (
    DurableTransition,
    InteractionDirective,
    LifecycleState,
)
from tools.runtime.recover_run_dispatches import _validated_agent_core_command


def _directive(
    *,
    original_user_text: str = "改为比较最近七个完整自然日的均值",
) -> InteractionDirective:
    return InteractionDirective.create(
        run_attempt_id="run-source",
        intent_revision_id="intent-source",
        kind="material_intent_change",
        target_refs=("intent-source",),
        original_user_text=original_user_text,
    )


def _continuation(
    *,
    directive: InteractionDirective | None = None,
) -> MaterialRevisionContinuation:
    return MaterialRevisionContinuation.create(
        directive=directive or _directive(),
        thread_id="thread-one",
        successor_user_text="比较最近七个完整自然日的日均付费金额",
        superseded_plan_fields=(
            "time_spec",
            "baseline_refs",
            "resolved_window_refs",
        ),
        parent_transition_id="transition-directive",
    )


def test_material_revision_continuation_has_one_stable_dispatch_identity() -> None:
    first = _continuation()
    replay = _continuation()
    changed = _continuation(
        directive=_directive(original_user_text="改为比较上周同日"),
    )

    assert replay == first
    assert first.request_identity == f"material-revision:{first.directive_id}"
    assert first.successor_run_id.startswith("run-")
    assert first.successor_message_id.startswith("message-")
    assert first.successor_dispatch_id.startswith("dispatch-")
    assert {
        first.successor_run_id,
        first.successor_message_id,
        first.successor_dispatch_id,
    }.isdisjoint(
        {
            changed.successor_run_id,
            changed.successor_message_id,
            changed.successor_dispatch_id,
        }
    )


def test_material_revision_continuation_is_an_exact_recoverable_command() -> None:
    continuation = _continuation()

    assert continuation.producer_kind == "thread_message"
    assert continuation.scope_ref == continuation.thread_id
    assert continuation.request_payload == {
        "message": continuation.successor_user_text,
        "intentRevisionContext": {
            "supersedes_intent_revision_id": "intent-source",
            "superseded_plan_fields": [
                "time_spec",
                "baseline_refs",
                "resolved_window_refs",
            ],
            "intent_revision_reason_ref": continuation.directive_id,
            "parent_transition_id": "transition-directive",
        },
    }
    assert continuation.request_digest == canonical_digest(
        {
            "producer_kind": "thread_message",
            "scope_ref": "thread-one",
            "thread_id": "thread-one",
            "request_payload": continuation.request_payload,
        }
    )
    assert (
        _validated_agent_core_command(
            continuation.request_payload,
            producer_kind=continuation.producer_kind,
            run_id=continuation.successor_run_id,
        )
        == continuation.request_payload
    )
    assert continuation.source_link_payload == {
        "schema_version": "material-revision-continuation.v1",
        "directive_id": continuation.directive_id,
        "source_run_id": "run-source",
        "successor_run_id": continuation.successor_run_id,
        "successor_dispatch_id": continuation.successor_dispatch_id,
        "continuation_ref": continuation.continuation_ref,
    }


def test_material_revision_continuation_round_trips_and_rejects_tampering() -> None:
    continuation = _continuation()

    assert (
        MaterialRevisionContinuation.from_dict(continuation.to_dict()) == continuation
    )

    tampered = continuation.to_dict()
    tampered["successor_dispatch_id"] = "dispatch-tampered"
    with pytest.raises(
        MaterialRevisionContinuationError,
        match="^material_revision_continuation_identity_invalid$",
    ):
        MaterialRevisionContinuation.from_dict(tampered)

    with pytest.raises(
        MaterialRevisionContinuationError,
        match="^material_revision_continuation_digest_invalid$",
    ):
        MaterialRevisionContinuation.from_dict(
            {
                **continuation.to_dict(),
                "content_digest": "0" * 64,
            }
        )


def test_material_revision_continuation_accepts_only_typed_material_changes() -> None:
    non_material = InteractionDirective.create(
        run_attempt_id="run-source",
        intent_revision_id="intent-source",
        kind="challenge",
        target_refs=("intent-source",),
        original_user_text="请重新检查这个结论",
    )

    with pytest.raises(
        MaterialRevisionContinuationError,
        match="^material_revision_directive_kind_invalid$",
    ):
        _continuation(directive=non_material)

    wrong_target = InteractionDirective.create(
        run_attempt_id="run-source",
        intent_revision_id="intent-source",
        kind="material_intent_change",
        target_refs=("intent-other",),
        original_user_text="改为比较最近七个完整自然日的均值",
    )
    with pytest.raises(
        MaterialRevisionContinuationError,
        match="^material_revision_directive_target_invalid$",
    ):
        _continuation(directive=wrong_target)

    with pytest.raises(
        MaterialRevisionContinuationError,
        match="^material_revision_plan_fields_invalid$",
    ):
        MaterialRevisionContinuation.create(
            directive=_directive(),
            thread_id="thread-one",
            successor_user_text="比较最近七个完整自然日的日均付费金额",
            superseded_plan_fields=("time_spec", "time_spec"),
            parent_transition_id="transition-directive",
        )

    with pytest.raises(
        MaterialRevisionContinuationError,
        match="^material_revision_plan_fields_invalid$",
    ):
        MaterialRevisionContinuation.create(
            directive=_directive(),
            thread_id="thread-one",
            successor_user_text="比较最近七个完整自然日的日均付费金额",
            superseded_plan_fields=("provider_guess",),
            parent_transition_id="transition-directive",
        )


def test_material_revision_continuation_is_immutable() -> None:
    continuation = _continuation()
    mutable_copy = continuation.request_payload
    mutable_copy["message"] = "mutated"

    assert continuation.request_payload["message"] == (continuation.successor_user_text)
    with pytest.raises(
        MaterialRevisionContinuationError,
        match="^material_revision_continuation_identity_invalid$",
    ):
        replace(continuation, successor_run_id="run-mutated").validate()


def test_agent_core_runs_the_committed_successor_dispatch_directly() -> None:
    continuation = _continuation()
    successor_calls: list[dict] = []

    def run_message(**kwargs):
        successor_calls.append(kwargs)
        return {
            "status": "planned",
            "run_id": continuation.successor_run_id,
        }

    core = SimpleNamespace(run_message=run_message)
    source_terminal = {
        "status": "interaction_completed",
        "run_id": continuation.source_run_id,
        "turn_id": "turn-source",
        "topic_id": "topic-source",
        "intent": "material_revision",
        "topic_relation": "material_revision",
        "context_manifest": {"manifest_id": "manifest-source"},
        "interaction_result": {
            "schema_version": "typed-interaction.v1",
            "intent": "material_revision",
            "response_text": "已接受业务问题修订，后续分析已创建并继续执行。",
        },
    }
    result = ConversationAgentCore._run_material_revision_successor(
        core,
        thread_id=continuation.thread_id,
        source_run_id=continuation.source_run_id,
        artifact_root="artifacts/material-revision",
        user_id="user-one",
        decision_result={
            "material_revision_continuation": continuation.to_dict(),
            "successor_run_dispatch": {
                "dispatch_id": continuation.successor_dispatch_id,
                "dispatch_owner_id": "material-owner",
                "lease_epoch": 1,
            },
            "source_terminal": source_terminal,
        },
        stop_after_phase="phase02",
    )

    assert successor_calls == [
        {
            "thread_id": continuation.thread_id,
            "run_id": continuation.successor_run_id,
            "user_message": continuation.successor_user_text,
            "user_id": "user-one",
            "artifact_root": "artifacts/material-revision",
            "run_dispatch": {
                "dispatch_id": continuation.successor_dispatch_id,
                "dispatch_owner_id": "material-owner",
                "lease_epoch": 1,
            },
            "intent_revision_context": continuation.request_payload[
                "intentRevisionContext"
            ],
            "stop_after_phase": "phase02",
        }
    ]
    assert result == {
        **source_terminal,
        "material_revision_continuation": continuation.to_dict(),
        "successor_run_id": continuation.successor_run_id,
        "successor_status": "planned",
    }


class _Cursor:
    def __init__(self, rows=()):
        self.rows = tuple(rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class _MaterialContinuationConnection:
    def __init__(self) -> None:
        self.source = {
            "run_id": "run-source",
            "thread_id": "thread-one",
            "turn_id": "turn-source",
            "topic_id": "topic-source",
            "status": "waiting_for_clarification",
            "request": {
                "runtime_descriptors": {
                    "context_manifest": {
                        "manifest_id": "manifest-source",
                    }
                }
            },
        }
        self.pending_clarification_id = "run-source"
        self.active_task_id = "run-source"
        self.customer_state = "working"
        self.state_version = 7
        self.source_dispatch = {
            "dispatch_id": "dispatch-source",
            "dispatch_state": "running",
            "owner_id": "owner-source",
            "lease_epoch": 4,
            "terminal_status": None,
        }
        self.successor_run = None
        self.successor_message = None
        self.successor_dispatch = None
        self.lifecycle = LifecycleState.create(
            run_attempt_id="run-source",
            execution_state="waiting",
            interaction_state="waiting_for_user",
        ).to_dict()
        self.audit_events: list[dict] = []

    def execute(self, statement, params=None):
        params = dict(params or {})
        if "material_revision_successor_run_insert" in statement:
            self.successor_run = {
                "run_id": params["run_id"],
                "thread_id": params["thread_id"],
                "status": "queued",
                "request": {},
            }
            return _Cursor(({"run_id": params["run_id"]},))
        if "material_revision_successor_message_insert" in statement:
            self.successor_message = {
                "message_id": params["message_id"],
                "thread_id": params["thread_id"],
                "role": "user",
                "text": params["text"],
                "customer_visible": not (
                    "customer_visible" in statement and "false" in statement
                ),
            }
            return _Cursor(({"message_id": params["message_id"]},))
        if "material_revision_successor_dispatch_insert" in statement:
            self.successor_dispatch = {
                **params,
                "request_payload": json.loads(params["request_payload"]),
                "dispatch_state": "leased",
                "lease_epoch": 1,
            }
            return _Cursor(
                (
                    {
                        "dispatch_id": params["dispatch_id"],
                        "lease_epoch": 1,
                    },
                )
            )
        if "material_revision_source_complete_cas" in statement:
            if self.source["status"] != "waiting_for_clarification":
                return _Cursor()
            self.source["status"] = "interaction_completed"
            self.source["request"] = json.loads(params["request"])
            return _Cursor(({"run_id": self.source["run_id"]},))
        if "material_revision_pending_clarification_clear_cas" in statement:
            if (
                self.pending_clarification_id != params["source_run_id"]
                or self.active_task_id != params["source_run_id"]
            ):
                return _Cursor()
            self.pending_clarification_id = ""
            self.active_task_id = params["successor_run_id"]
            self.customer_state = "working"
            self.state_version += 1
            return _Cursor(({"thread_id": params["thread_id"]},))
        if "INSERT INTO waje_runtime.run_lifecycle_state_revisions" in statement:
            self.lifecycle = json.loads(params["payload"])
            return _Cursor(({"state_revision": self.lifecycle["state_revision"]},))
        if "FROM waje_runtime.run_lifecycle_state_revisions" in statement:
            if (
                "state_revision = %(state_revision)s" in statement
                and self.lifecycle["state_revision"] != params["state_revision"]
            ):
                return _Cursor()
            return _Cursor(({"payload": json.dumps(self.lifecycle)},))
        if "owned_run_dispatch_terminal_cas" in statement:
            if (
                self.source_dispatch["dispatch_id"] != params["dispatch_id"]
                or self.source_dispatch["owner_id"] != params["owner_id"]
                or self.source_dispatch["lease_epoch"] != params["lease_epoch"]
            ):
                return _Cursor()
            self.source_dispatch["dispatch_state"] = "terminal"
            self.source_dispatch["terminal_status"] = params["status"]
            return _Cursor(({"dispatch_state": "terminal"},))
        if "INSERT INTO waje_runtime.audit_events" in statement:
            self.audit_events.append(
                {
                    **params,
                    "payload": json.loads(params["payload"]),
                }
            )
            return _Cursor()
        if (
            "SELECT thread_id, turn_id, topic_id, status, request" in statement
            and "FROM waje_runtime.analysis_runs" in statement
        ):
            return _Cursor((dict(self.source),))
        raise AssertionError(statement)

    def commit(self):
        return None

    def rollback(self):
        return None


def test_store_material_revision_commit_closes_source_and_leases_successor() -> None:
    directive = _directive()
    transition = DurableTransition.create(
        node_name="bind_free_text_submission",
        parent_transition_id="transition-waiting",
        run_attempt_id=directive.run_attempt_id,
        intent_revision_id=directive.intent_revision_id,
        decision_ledger_position=0,
        input_digest=canonical_digest({"input": "material-revision"}),
        output_digest=canonical_digest({"output": "material-revision"}),
        execution_attempt=1,
        provider_ref="provider-test",
        model_ref="model-test",
        status="succeeded",
        acceptance_state="accepted",
        next_transition="create_intent_revision",
    )
    continuation = MaterialRevisionContinuation.create(
        directive=directive,
        thread_id="thread-one",
        successor_user_text="比较最近七个完整自然日的日均付费金额",
        superseded_plan_fields=(
            "time_spec",
            "baseline_refs",
            "resolved_window_refs",
        ),
        parent_transition_id=transition.transition_id,
    )
    connection = _MaterialContinuationConnection()
    store = PostgresConversationStore(connection)

    result = store._persist_material_revision_continuation_locked(
        directive=directive,
        transition=transition,
        continuation=continuation,
        source_dispatch=("dispatch-source", "owner-source", 4),
    )

    assert connection.source["status"] == "interaction_completed"
    assert connection.pending_clarification_id == ""
    assert connection.active_task_id == continuation.successor_run_id
    assert connection.customer_state == "working"
    assert connection.state_version == 8
    assert connection.source_dispatch["dispatch_state"] == "terminal"
    assert connection.source_dispatch["terminal_status"] == ("interaction_completed")
    assert connection.lifecycle["execution_state"] == "superseded"
    assert connection.lifecycle["interaction_state"] == "superseded"
    assert connection.lifecycle["publication_state"] == "not_ready"
    assert connection.lifecycle["supersession_state"] == "superseded"
    assert connection.successor_run == {
        "run_id": continuation.successor_run_id,
        "thread_id": continuation.thread_id,
        "status": "queued",
        "request": {},
    }
    assert connection.successor_message["message_id"] == (
        continuation.successor_message_id
    )
    assert connection.successor_message["customer_visible"] is False
    assert connection.successor_dispatch["dispatch_id"] == (
        continuation.successor_dispatch_id
    )
    assert connection.successor_dispatch["request_digest"] == (
        continuation.request_digest
    )
    assert connection.successor_dispatch["request_payload"] == (
        continuation.request_payload
    )
    assert connection.successor_dispatch["dispatch_state"] == "leased"
    assert result["material_revision_continuation"] == continuation.to_dict()
    assert result["successor_run_dispatch"]["dispatch_id"] == (
        continuation.successor_dispatch_id
    )
    assert result["source_terminal"]["status"] == "interaction_completed"
    assert {event["event_type"] for event in connection.audit_events} == {
        "material_revision_continuation_created",
        "run_status_changed",
        "clarification_cleared",
        "run_dispatch_completed",
        "message_recorded",
        "run_queued",
        "run_dispatch_leased",
    }
