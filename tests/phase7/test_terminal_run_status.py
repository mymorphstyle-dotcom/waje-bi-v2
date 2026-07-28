from __future__ import annotations

import json
import os
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

from bi_agent.conversation import run_status as run_status_policy
from bi_agent.conversation.agent_core import ConversationAgentCore
from bi_agent.conversation.postgres_store import PostgresConversationStore
from bi_agent.conversation.store import InMemoryConversationStore
from bi_agent.runtime.evidence_authority import EvidenceIntegrityError
from bi_agent.runtime.durable_call_journal import DurableCallSpec
from bi_agent.runtime.single_authority import LifecycleState


def test_run_status_value_validator_defines_closed_vocabulary() -> None:
    validator = getattr(run_status_policy, "validate_run_status_value", None)

    assert callable(validator)
    for status in run_status_policy.RUN_STATUS_TRANSITIONS:
        assert validator(status) == status
    with pytest.raises(
        EvidenceIntegrityError,
        match="^analysis_run_status_invalid$",
    ):
        validator("unknown_runtime_state")


@pytest.mark.parametrize(
    "attempted_status",
    ("failed", "running", "waiting_for_clarification"),
)
def test_inmemory_completed_run_authority_cannot_be_downgraded(
    attempted_status: str,
) -> None:
    store = InMemoryConversationStore()
    store.upsert_run(
        "run-source",
        thread_id="thread-1",
        topic_id="topic-1",
        status="completed",
        request={"question": "source question"},
    )
    completed_run = deepcopy(store.runs["run-source"])
    completed_events = store.audit_events

    with pytest.raises(
        EvidenceIntegrityError,
        match="^analysis_run_status_transition_conflict$",
    ):
        store.upsert_run(
            "run-source",
            thread_id="thread-1",
            topic_id="topic-1",
            status=attempted_status,
            request={"failure_reason": "late client error"},
        )

    assert store.runs["run-source"] == completed_run
    assert store.audit_events == completed_events


def test_inmemory_nonterminal_run_can_still_transition_to_failed() -> None:
    store = InMemoryConversationStore()
    store.upsert_run(
        "run-real-failure",
        thread_id="thread-1",
        topic_id="topic-1",
        status="running_workflow",
        request={"question": "source question"},
    )

    store.upsert_run(
        "run-real-failure",
        thread_id="thread-1",
        topic_id="topic-1",
        status="failed",
        request={"failure_reason": "workflow_failed"},
    )

    assert store.runs["run-real-failure"]["status"] == "failed"


class _Cursor:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class _RunStatusConnection:
    def __init__(
        self,
        *,
        status: str,
        request: dict,
        thread_id: str = "thread-1",
        turn_id: str = "",
        topic_id: str = "topic-1",
        exists: bool = True,
        fail_audit: bool = False,
        fail_secondary_audit: bool = False,
    ):
        self._committed_run = (
            {
                "run_id": "run-transition",
                "status": status,
                "request": deepcopy(request),
                "thread_id": thread_id,
                "turn_id": turn_id,
                "topic_id": topic_id,
            }
            if exists
            else None
        )
        self._pending_run: dict | None = None
        self._committed_lifecycle: dict | None = None
        self._pending_lifecycle: dict | None = None
        self._pending_audit_events: list[dict] = []
        self.audit_events: list[dict] = []
        self.lifecycle: dict | None = None
        self.statements: list[tuple[str, dict]] = []
        self.transaction_events: list[str] = []
        self.audit_attempts = 0
        self.fail_audit = fail_audit
        self.fail_secondary_audit = fail_secondary_audit
        self.commits = 0
        self.rollbacks = 0

    @property
    def exists(self):
        return self._committed_run is not None

    @property
    def status(self):
        return str((self._committed_run or {}).get("status") or "")

    @property
    def request(self):
        return deepcopy((self._committed_run or {}).get("request") or {})

    @property
    def thread_id(self):
        return str((self._committed_run or {}).get("thread_id") or "")

    @property
    def turn_id(self):
        return str((self._committed_run or {}).get("turn_id") or "")

    @property
    def topic_id(self):
        return str((self._committed_run or {}).get("topic_id") or "")

    def _visible_run(self):
        return self._pending_run or self._committed_run

    def _visible_lifecycle(self):
        return self._pending_lifecycle or self._committed_lifecycle

    def execute(self, statement, params=None):
        params = dict(params or {})
        self.statements.append((statement, params))
        if "INSERT INTO waje_runtime.run_lifecycle_state_revisions" in statement:
            payload = json.loads(params["payload"])
            existing = self._visible_lifecycle()
            if existing is not None and (
                existing["state_revision"] == payload["state_revision"]
            ):
                return _Cursor()
            self._pending_lifecycle = payload
            return _Cursor(({"state_revision": payload["state_revision"]},))
        if "FROM waje_runtime.run_lifecycle_state_revisions" in statement:
            lifecycle = self._visible_lifecycle()
            if lifecycle is None:
                return _Cursor()
            if "state_revision = %(state_revision)s" in statement and (
                lifecycle["state_revision"] != params["state_revision"]
            ):
                return _Cursor()
            return _Cursor(({"payload": json.dumps(lifecycle)},))
        if "analysis_run_status_insert" in statement:
            if self._visible_run() is not None:
                return _Cursor()
            self._pending_run = {
                "run_id": str(params["run_id"]),
                "status": str(params["status"]),
                "request": json.loads(params["request"]),
                "thread_id": str(params["thread_id"]),
                "turn_id": str(params.get("turn_id") or ""),
                "topic_id": str(params.get("topic_id") or ""),
            }
            return _Cursor(({"status": self._pending_run["status"]},))
        if "analysis_run_status_transition_lock" in statement:
            run = self._visible_run()
            if run is None:
                return _Cursor()
            return _Cursor(
                (
                    {
                        "status": run["status"],
                        "thread_id": run["thread_id"],
                        "turn_id": run["turn_id"] or None,
                        "topic_id": run["topic_id"] or None,
                        "request": json.dumps(run["request"]),
                    },
                )
            )
        if "analysis_run_status_transition_cas" in statement:
            run = self._visible_run()
            if run is None or run["status"] != str(params.get("current_status") or ""):
                return _Cursor()
            self._pending_run = {
                **run,
                "status": str(params["status"]),
                "request": json.loads(params["request"]),
                "turn_id": str(params.get("turn_id") or ""),
                "topic_id": str(params.get("topic_id") or ""),
            }
            return _Cursor(({"status": self._pending_run["status"]},))
        if "analysis_run_state" in statement:
            run = self._visible_run()
            return _Cursor((deepcopy(run),)) if run is not None else _Cursor()
        if "analysis_run_failure_primary_audit" in statement:
            return _Cursor(
                tuple(
                    deepcopy(event)
                    for event in self.audit_events
                    if event.get("run_id") == params.get("run_id")
                    and event.get("event_type") == params.get("failure_reason")
                )
            )
        if "INSERT INTO waje_runtime.audit_events" in statement:
            self.audit_attempts += 1
            self.transaction_events.append("audit")
            if self.fail_audit or (
                self.fail_secondary_audit
                and params.get("event_type") == "workflow_failure_llm_call_recorded"
            ):
                raise RuntimeError("run_status_audit_unavailable")
            self._pending_audit_events.append(deepcopy(params))
        return _Cursor()

    def commit(self):
        self.transaction_events.append("commit")
        if self._pending_run is not None:
            self._committed_run = deepcopy(self._pending_run)
        if self._pending_lifecycle is not None:
            self._committed_lifecycle = deepcopy(self._pending_lifecycle)
        self.audit_events.extend(deepcopy(self._pending_audit_events))
        self._pending_run = None
        self._pending_lifecycle = None
        self._pending_audit_events = []
        self.commits += 1

    def rollback(self):
        self.transaction_events.append("rollback")
        self._pending_run = None
        self._pending_lifecycle = None
        self._pending_audit_events = []
        self.rollbacks += 1


class _RunDispatchOwnershipConnection:
    def __init__(
        self,
        *,
        owner_id: str = "owner-current",
        lease_epoch: int = 4,
        dispatch_state: str = "leased",
        run_status: str = "queued",
        lease_active: bool = True,
        producer_kind: str = "thread_message",
    ) -> None:
        self.run = {
            "run_id": "run-dispatch",
            "thread_id": "thread-dispatch",
            "turn_id": None,
            "topic_id": None,
            "status": run_status,
            "request": {},
        }
        self.dispatch = {
            "dispatch_id": "dispatch-1",
            "producer_kind": producer_kind,
            "scope_ref": (
                "run-dispatch"
                if producer_kind == "clarification_resolution"
                else "thread-dispatch"
            ),
            "request_identity": "request-dispatch",
            "request_digest": "a" * 64,
            "request_payload": {
                "message": "检查昨天付费金额",
                **(
                    {
                        "resumeRequest": {
                            "schema_version": (
                                "single-authority-phase02-waiting.v1"
                            ),
                            "run_attempt_id": "run-dispatch",
                            "thread_id": "thread-dispatch",
                            "turn_id": "turn-dispatch",
                            "topic_id": "topic-dispatch",
                            "turn_intent": "new_analysis",
                            "topic_relation": "new_topic",
                            "intent_revision_id": "intent-dispatch",
                            "decision_ledger_position": 0,
                            "accepted_transition_id": "transition-waiting",
                            "clarification": {"status": "needs_input"},
                            "context_manifest_ref": "context-dispatch",
                            "runtime_descriptors": {},
                        },
                    }
                    if producer_kind == "clarification_resolution"
                    else {}
                ),
            },
            "run_id": "run-dispatch",
            "thread_id": "thread-dispatch",
            "dispatch_state": dispatch_state,
            "owner_id": owner_id,
            "lease_epoch": lease_epoch,
            "lease_active": lease_active,
            "terminal_status": None,
            "failure_reason": None,
        }
        self.audit_events: list[dict] = []
        self.lifecycle: dict | None = None
        self.statements: list[tuple[str, dict]] = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, statement, params=None):
        params = dict(params or {})
        self.statements.append((statement, params))
        if "INSERT INTO waje_runtime.run_lifecycle_state_revisions" in statement:
            payload = json.loads(params["payload"])
            if (
                self.lifecycle is not None
                and self.lifecycle["state_revision"] >= payload["state_revision"]
            ):
                return _Cursor()
            self.lifecycle = payload
            return _Cursor(({"state_revision": payload["state_revision"]},))
        if "FROM waje_runtime.run_lifecycle_state_revisions" in statement:
            if self.lifecycle is None:
                return _Cursor()
            return _Cursor(({"payload": json.dumps(self.lifecycle)},))
        if "pg_advisory_xact_lock" in statement:
            return _Cursor()
        if "recoverable_run_dispatch_scan" in statement:
            recoverable_status = (
                self.dispatch["producer_kind"] == "thread_message"
                and self.run["status"] == "queued"
            ) or (
                self.dispatch["producer_kind"] == "clarification_resolution"
                and self.run["status"] == "waiting_for_clarification"
            )
            if recoverable_status and (
                self.dispatch["dispatch_state"] == "pending"
                or (
                    self.dispatch["dispatch_state"] == "leased"
                    and not self.dispatch["lease_active"]
                )
            ):
                return _Cursor(
                    (
                        {
                            **deepcopy(self.dispatch),
                            "lease_expired": not self.dispatch["lease_active"],
                            "run_status": self.run["status"],
                        },
                    )
                )
            return _Cursor()
        if "recoverable_run_dispatch_lease_cas" in statement:
            recoverable_status = (
                self.dispatch["producer_kind"] == "thread_message"
                and self.run["status"] == "queued"
            ) or (
                self.dispatch["producer_kind"] == "clarification_resolution"
                and self.run["status"] == "waiting_for_clarification"
            )
            if (
                not recoverable_status
                or self.dispatch["lease_epoch"] != params["current_epoch"]
                or (
                    self.dispatch["dispatch_state"] != "pending"
                    and not (
                        self.dispatch["dispatch_state"] == "leased"
                        and not self.dispatch["lease_active"]
                    )
                )
            ):
                return _Cursor()
            self.dispatch.update(
                {
                    "dispatch_state": "leased",
                    "owner_id": params["owner_id"],
                    "lease_epoch": self.dispatch["lease_epoch"] + 1,
                    "lease_active": True,
                }
            )
            return _Cursor(({"lease_epoch": self.dispatch["lease_epoch"]},))
        if "recovery_run_dispatch_owner_lock" in statement:
            if (
                self.dispatch["dispatch_id"] != params["dispatch_id"]
                or self.dispatch["run_id"] != params["run_id"]
            ):
                return _Cursor()
            return _Cursor((deepcopy(self.dispatch),))
        if "recovery_run_dispatch_run_lock" in statement:
            if self.run["run_id"] != params["run_id"]:
                return _Cursor()
            return _Cursor((deepcopy(self.run),))
        if "recovery_run_dispatch_failure_cas" in statement:
            if self.run["status"] != params["current_status"]:
                return _Cursor()
            self.run["status"] = "failed"
            self.run["request"] = json.loads(params["request"])
            return _Cursor(({"status": "failed"},))
        if "recovery_run_dispatch_terminal_cas" in statement:
            if (
                self.dispatch["dispatch_state"] not in {"leased", "running"}
                or self.dispatch["owner_id"] != params["owner_id"]
                or self.dispatch["lease_epoch"] != params["lease_epoch"]
                or not self.dispatch["lease_active"]
            ):
                return _Cursor()
            self.dispatch.update(
                {
                    "dispatch_state": "terminal",
                    "terminal_status": "failed",
                    "failure_reason": params["failure_reason"],
                    "lease_active": False,
                }
            )
            return _Cursor(({"dispatch_state": "terminal"},))
        if "generic_run_dispatch_owner_lock" in statement:
            if (
                self.dispatch["dispatch_id"] != params["dispatch_id"]
                or self.dispatch["run_id"] != params["run_id"]
            ):
                return _Cursor()
            return _Cursor((deepcopy(self.dispatch),))
        if "generic_run_dispatch_run_lock" in statement:
            if self.run["run_id"] != params["run_id"]:
                return _Cursor()
            return _Cursor((deepcopy(self.run),))
        if "analysis_run_status_transition_lock" in statement:
            if self.run["run_id"] != params["run_id"]:
                return _Cursor()
            return _Cursor((deepcopy(self.run),))
        if "generic_run_dispatch_run_claim_cas" in statement:
            if self.run["status"] != "queued":
                return _Cursor()
            self.run["status"] = "running"
            return _Cursor(({"status": "running"},))
        if "generic_run_dispatch_owner_consume_cas" in statement:
            if (
                self.dispatch["dispatch_state"] != "leased"
                or self.dispatch["owner_id"] != params["owner_id"]
                or self.dispatch["lease_epoch"] != params["lease_epoch"]
            ):
                return _Cursor()
            self.dispatch["dispatch_state"] = "running"
            self.dispatch["lease_active"] = True
            return _Cursor(({"dispatch_state": "running"},))
        if "generic_run_dispatch_heartbeat_cas" in statement:
            if (
                self.dispatch["dispatch_state"] != "running"
                or self.dispatch["owner_id"] != params["owner_id"]
                or self.dispatch["lease_epoch"] != params["lease_epoch"]
                or not self.dispatch["lease_active"]
                or self.run["status"]
                not in {
                    "running",
                    "running_workflow",
                    "waiting_for_clarification",
                }
            ):
                return _Cursor()
            return _Cursor(({"run_id": self.run["run_id"]},))
        if "owned_analysis_run_status_transition_cas" in statement:
            if self.run["status"] != params["current_status"]:
                return _Cursor()
            self.run.update(
                {
                    "status": params["status"],
                    "request": json.loads(params["request"]),
                    "turn_id": params.get("turn_id"),
                    "topic_id": params.get("topic_id"),
                }
            )
            return _Cursor(({"status": self.run["status"]},))
        if "analysis_run_status_transition_cas" in statement:
            if self.run["status"] != params["current_status"]:
                return _Cursor()
            self.run.update(
                {
                    "status": params["status"],
                    "request": json.loads(params["request"]),
                    "turn_id": params.get("turn_id"),
                    "topic_id": params.get("topic_id"),
                }
            )
            return _Cursor(({"status": self.run["status"]},))
        if "analysis_run_failure_primary_audit" in statement:
            return _Cursor(
                tuple(
                    event
                    for event in self.audit_events
                    if event.get("event_type") == params["failure_reason"]
                    and event.get("run_id") == params["run_id"]
                )
            )
        if "owned_run_dispatch_terminal_cas" in statement:
            if (
                self.dispatch["dispatch_state"] != "running"
                or self.dispatch["owner_id"] != params["owner_id"]
                or self.dispatch["lease_epoch"] != params["lease_epoch"]
            ):
                return _Cursor()
            self.dispatch["dispatch_state"] = "terminal"
            self.dispatch["terminal_status"] = params["status"]
            self.dispatch["lease_active"] = False
            return _Cursor(({"dispatch_state": "terminal"},))
        if "expired_run_dispatch_scan" in statement:
            if (
                self.dispatch["dispatch_state"] in {"leased", "running"}
                and not self.dispatch["lease_active"]
            ):
                return _Cursor(
                    (
                        {
                            **deepcopy(self.dispatch),
                            "run_status": self.run["status"],
                        },
                    )
                )
            return _Cursor()
        if "expired_running_dispatch_run_fail_cas" in statement:
            if self.run["status"] not in {"running", "running_workflow"}:
                return _Cursor()
            self.run["status"] = "failed"
            self.run["request"] = {
                **self.run["request"],
                "failure_reason": "run_dispatch_heartbeat_expired",
            }
            return _Cursor(({"status": "failed"},))
        if "expired_running_clarification_run_restore_cas" in statement:
            if self.run["status"] != params["run_status"]:
                return _Cursor()
            self.run["status"] = "waiting_for_clarification"
            return _Cursor(({"status": "waiting_for_clarification"},))
        if "expired_controlled_investigation_lease_cascade" in statement:
            return _Cursor()
        if "expired_running_clarification_release_cas" in statement:
            if (
                self.dispatch["dispatch_state"] != "running"
                or self.dispatch["owner_id"] != params["owner_id"]
                or self.dispatch["lease_epoch"] != params["lease_epoch"]
            ):
                return _Cursor()
            self.dispatch.update(
                {
                    "dispatch_state": "pending",
                    "owner_id": None,
                    "lease_active": False,
                    "terminal_status": None,
                    "failure_reason": None,
                }
            )
            return _Cursor(({"dispatch_state": "pending"},))
        if "expired_running_dispatch_terminal_cas" in statement:
            if self.dispatch["dispatch_state"] != "running":
                return _Cursor()
            self.dispatch["dispatch_state"] = "terminal"
            self.dispatch["terminal_status"] = "failed"
            return _Cursor(({"dispatch_state": "terminal"},))
        if "expired_leased_dispatch_release_cas" in statement:
            if self.dispatch["dispatch_state"] != "leased":
                return _Cursor()
            self.dispatch.update(
                {
                    "dispatch_state": "pending",
                    "owner_id": None,
                    "lease_active": False,
                }
            )
            return _Cursor(({"dispatch_state": "pending"},))
        if "INSERT INTO waje_runtime.audit_events" in statement:
            self.audit_events.append(deepcopy(params))
            return _Cursor()
        raise AssertionError(statement)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_generic_dispatch_owner_claim_heartbeat_and_terminal_transition_are_fenced():
    connection = _RunDispatchOwnershipConnection()
    store = PostgresConversationStore(connection)

    claimed = store.claim_run_dispatch(
        dispatch_id="dispatch-1",
        run_id="run-dispatch",
        thread_id="thread-dispatch",
        dispatch_owner_id="owner-current",
        lease_epoch=4,
    )

    assert claimed["status"] == "running"
    assert connection.run["status"] == "running"
    assert connection.dispatch["dispatch_state"] == "running"
    assert connection.lifecycle is not None
    assert connection.lifecycle["run_attempt_id"] == "run-dispatch"
    assert connection.lifecycle["state_revision"] == 1
    assert connection.lifecycle["execution_state"] == "running"
    assert connection.lifecycle["cancellation_state"] == "active"
    assert connection.lifecycle["supersession_state"] == "active"
    assert (
        store.renew_run_dispatch_lease(
            dispatch_id="dispatch-1",
            run_id="run-dispatch",
            dispatch_owner_id="owner-current",
            lease_epoch=4,
        )
        is True
    )
    assert (
        store.renew_run_dispatch_lease(
            dispatch_id="dispatch-1",
            run_id="run-dispatch",
            dispatch_owner_id="owner-stale",
            lease_epoch=3,
        )
        is False
    )

    store.upsert_run(
        "run-dispatch",
        thread_id="thread-dispatch",
        status="waiting_for_clarification",
        request={"reason": "needs_clarification"},
    )

    assert connection.run["status"] == "waiting_for_clarification"
    assert connection.dispatch["dispatch_state"] == "terminal"
    assert connection.dispatch["terminal_status"] == "waiting_for_clarification"


def test_generic_dispatch_claim_rejects_stale_epoch_before_start():
    connection = _RunDispatchOwnershipConnection()

    with pytest.raises(
        EvidenceIntegrityError,
        match="^run_dispatch_claim_rejected$",
    ):
        PostgresConversationStore(connection).claim_run_dispatch(
            dispatch_id="dispatch-1",
            run_id="run-dispatch",
            thread_id="thread-dispatch",
            dispatch_owner_id="owner-current",
            lease_epoch=3,
        )

    assert connection.run["status"] == "queued"
    assert connection.dispatch["dispatch_state"] == "leased"


def test_clarification_dispatch_claim_preserves_waiting_run_until_decision_resume():
    connection = _RunDispatchOwnershipConnection(
        producer_kind="clarification_resolution",
        dispatch_state="leased",
        run_status="waiting_for_clarification",
    )
    connection.lifecycle = LifecycleState.create(
        run_attempt_id="run-dispatch",
        execution_state="waiting",
        interaction_state="waiting_for_user",
    ).to_dict()
    store = PostgresConversationStore(connection)

    claimed = store.claim_run_dispatch(
        dispatch_id="dispatch-1",
        run_id="run-dispatch",
        thread_id="thread-dispatch",
        dispatch_owner_id="owner-current",
        lease_epoch=4,
    )

    assert claimed["producer_kind"] == "clarification_resolution"
    assert claimed["dispatch_state"] == "running"
    assert claimed["status"] == "waiting_for_clarification"
    assert connection.run["status"] == "waiting_for_clarification"
    assert connection.dispatch["dispatch_state"] == "running"
    assert store._active_run_dispatches["run-dispatch"] == (
        "dispatch-1",
        "owner-current",
        4,
    )

    store.upsert_run(
        "run-dispatch",
        thread_id="thread-dispatch",
        status="running_workflow",
        request={},
    )

    assert connection.run["status"] == "running_workflow"
    assert connection.dispatch["dispatch_state"] == "running"


def test_clarification_dispatch_start_failure_preserves_waiting_business_run():
    connection = _RunDispatchOwnershipConnection(
        producer_kind="clarification_resolution",
        dispatch_state="leased",
        run_status="waiting_for_clarification",
    )

    durable = PostgresConversationStore(connection).fail_owned_run_dispatch(
        dispatch_id="dispatch-1",
        run_id="run-dispatch",
        thread_id="thread-dispatch",
        dispatch_owner_id="owner-current",
        lease_epoch=4,
        failure_reason="agent_core_startup_failed",
    )

    assert durable == {
        "dispatch_id": "dispatch-1",
        "dispatch_status": "failed",
        "failure_reason": "agent_core_startup_failed",
        "run_id": "run-dispatch",
        "status": "waiting_for_clarification",
        "thread_id": "thread-dispatch",
    }
    assert connection.run["status"] == "waiting_for_clarification"
    assert connection.run["request"] == {}
    assert connection.dispatch["dispatch_state"] == "terminal"
    assert connection.dispatch["terminal_status"] == "failed"


def _create_postgres_leased_dispatch(
    store: PostgresConversationStore,
    *,
    suffix: str,
) -> tuple[str, str, str, str]:
    thread_id = f"thread-dispatch-pg-{suffix}"
    run_id = f"run-dispatch-pg-{suffix}"
    owner_id = f"dispatch-owner-pg-{suffix}"
    dispatch_id = f"dispatch-pg-{suffix}"
    message_id = f"message-dispatch-pg-{suffix}"
    store.create_thread(thread_id, owner_id=f"thread-owner-pg-{suffix}")
    store.connection.execute(
        """
        INSERT INTO waje_runtime.conversation_messages(
          message_id, thread_id, role, text
        ) VALUES (
          %(message_id)s, %(thread_id)s, 'user', 'dispatch lifecycle test'
        )
        """,
        {"message_id": message_id, "thread_id": thread_id},
    )
    store.connection.execute(
        """
        INSERT INTO waje_runtime.analysis_runs(
          run_id, run_attempt_id, thread_id, status, request
        ) VALUES (
          %(run_id)s, %(run_id)s, %(thread_id)s, 'queued', '{}'::jsonb
        )
        """,
        {"run_id": run_id, "thread_id": thread_id},
    )
    store.connection.execute(
        """
        INSERT INTO waje_runtime.run_dispatches(
          dispatch_id, producer_kind, scope_ref, request_identity,
          request_digest, request_payload, thread_id, run_id,
          message_id, dispatch_state, owner_id, lease_epoch, lease_expires_at,
          heartbeat_at
        ) VALUES (
          %(dispatch_id)s, 'thread_message', %(thread_id)s,
          %(request_identity)s, %(request_digest)s, '{}'::jsonb,
          %(thread_id)s, %(run_id)s, %(message_id)s,
          'leased', %(owner_id)s, 1,
          now() + interval '10 minutes', now()
        )
        """,
        {
            "dispatch_id": dispatch_id,
            "thread_id": thread_id,
            "request_identity": f"request-pg-{suffix}",
            "request_digest": "a" * 64,
            "run_id": run_id,
            "message_id": message_id,
            "owner_id": owner_id,
        },
    )
    store.connection.commit()
    return thread_id, run_id, dispatch_id, owner_id


def _terminalize_postgres_dispatch_test_run(
    store: PostgresConversationStore,
    *,
    thread_id: str,
    run_id: str,
) -> None:
    lifecycle = store.latest_lifecycle_state(run_id)
    if lifecycle is not None and lifecycle.cancellation_state == "active":
        store.cancel_run_attempt(
            run_attempt_id=run_id,
            reason_ref=f"test-cleanup:{run_id}",
        )
    if run_id in store._active_run_dispatches:
        store.upsert_run(
            run_id,
            thread_id=thread_id,
            status="failed",
            request={"failure_reason": "test_cleanup"},
        )
        return
    store.connection.execute(
        """
        UPDATE waje_runtime.analysis_runs
        SET status = 'failed',
            request = jsonb_build_object('failure_reason', 'test_cleanup'),
            updated_at = now()
        WHERE run_id = %(run_id)s AND status = 'queued'
        """,
        {"run_id": run_id},
    )
    store.connection.execute(
        """
        UPDATE waje_runtime.run_dispatches
        SET dispatch_state = 'terminal', terminal_status = 'failed',
            failure_reason = 'test_cleanup', lease_expires_at = NULL,
            updated_at = now()
        WHERE run_id = %(run_id)s AND dispatch_state = 'leased'
        """,
        {"run_id": run_id},
    )
    store.connection.commit()


@pytest.mark.skipif(
    not (os.getenv("WAJE_RUNTIME_DATABASE_URL") or os.getenv("DATABASE_URL")),
    reason="runtime PostgreSQL is not configured",
)
def test_postgres_dispatch_claim_opens_conversation_provider_scope_atomically():
    store = PostgresConversationStore.from_env()
    store.apply_schema()
    suffix = uuid4().hex
    thread_id, run_id, dispatch_id, owner_id = _create_postgres_leased_dispatch(
        store,
        suffix=suffix,
    )
    attempt = None
    try:
        claimed = store.claim_run_dispatch(
            dispatch_id=dispatch_id,
            run_id=run_id,
            thread_id=thread_id,
            dispatch_owner_id=owner_id,
            lease_epoch=1,
        )
        lifecycle = store.latest_lifecycle_state(run_id)
        row = store.connection.execute(
            """
            SELECT run.status, dispatch.dispatch_state,
                   (SELECT count(*)
                    FROM waje_runtime.run_lifecycle_state_revisions state
                    WHERE state.run_attempt_id = run.run_id)
            FROM waje_runtime.analysis_runs run
            JOIN waje_runtime.run_dispatches dispatch
              ON dispatch.run_id = run.run_id
            WHERE run.run_id = %(run_id)s
            """,
            {"run_id": run_id},
        ).fetchone()

        assert claimed["status"] == "running"
        assert tuple(row) == ("running", "running", 1)
        assert lifecycle is not None
        assert lifecycle.state_revision == 1
        assert lifecycle.execution_state == "running"
        assert lifecycle.cancellation_state == "active"
        assert lifecycle.supersession_state == "active"

        duplicate_store = PostgresConversationStore.from_env()
        try:
            with pytest.raises(
                EvidenceIntegrityError,
                match="^run_dispatch_claim_rejected$",
            ):
                duplicate_store.claim_run_dispatch(
                    dispatch_id=dispatch_id,
                    run_id=run_id,
                    thread_id=thread_id,
                    dispatch_owner_id=owner_id,
                    lease_epoch=1,
                )
        finally:
            duplicate_store.connection.close()

        spec = DurableCallSpec.create(
            run_attempt_id=run_id,
            intent_revision_id=None,
            plan_revision_id=None,
            task_id=None,
            stage_name="conversation_entry",
            call_kind="conversation_provider",
            operation_name="dispatch_lifecycle_regression",
            input_ref=f"dispatch-lifecycle-input:{suffix}",
            input_payload={"question": "检查付费金额变化"},
        )
        claim = store.attempt_journal.claim(spec)
        attempt = claim.attempt
        assert claim.replayed is False
        event_rows = store.connection.execute(
            """
            SELECT status
            FROM waje_runtime.durable_call_attempt_events
            WHERE run_attempt_id = %(run_id)s
              AND attempt_ref = %(attempt_ref)s
            ORDER BY event_sequence
            """,
            {
                "run_id": run_id,
                "attempt_ref": attempt.attempt_ref,
            },
        ).fetchall()
        assert tuple(str(row[0]) for row in event_rows) == (
            "claimed",
            "started",
        )
    finally:
        if attempt is not None:
            store.attempt_journal.fail(
                attempt,
                failure_code="dispatch_lifecycle_regression_complete",
            )
        _terminalize_postgres_dispatch_test_run(
            store,
            thread_id=thread_id,
            run_id=run_id,
        )
        store.connection.close()


class _PostgresDispatchLifecycleFailureStore(PostgresConversationStore):
    def _append_lifecycle_state_locked(self, state: LifecycleState) -> str:
        del state
        raise RuntimeError("injected_dispatch_lifecycle_failure")


@pytest.mark.skipif(
    not (os.getenv("WAJE_RUNTIME_DATABASE_URL") or os.getenv("DATABASE_URL")),
    reason="runtime PostgreSQL is not configured",
)
def test_postgres_dispatch_lifecycle_failure_rolls_back_run_and_dispatch_claim():
    store = _PostgresDispatchLifecycleFailureStore.from_env()
    store.apply_schema()
    suffix = uuid4().hex
    thread_id, run_id, dispatch_id, owner_id = _create_postgres_leased_dispatch(
        store,
        suffix=suffix,
    )
    recovered_store = None
    try:
        with pytest.raises(
            RuntimeError,
            match="^injected_dispatch_lifecycle_failure$",
        ):
            store.claim_run_dispatch(
                dispatch_id=dispatch_id,
                run_id=run_id,
                thread_id=thread_id,
                dispatch_owner_id=owner_id,
                lease_epoch=1,
            )

        row = store.connection.execute(
            """
            SELECT run.status, dispatch.dispatch_state,
                   dispatch.owner_id, dispatch.lease_epoch,
                   (SELECT count(*)
                    FROM waje_runtime.run_lifecycle_state_revisions state
                    WHERE state.run_attempt_id = run.run_id),
                   (SELECT count(*)
                    FROM waje_runtime.audit_events audit
                    WHERE audit.run_id = run.run_id
                      AND audit.event_type = 'run_status_changed')
            FROM waje_runtime.analysis_runs run
            JOIN waje_runtime.run_dispatches dispatch
              ON dispatch.run_id = run.run_id
            WHERE run.run_id = %(run_id)s
            """,
            {"run_id": run_id},
        ).fetchone()
        assert tuple(row) == ("queued", "leased", owner_id, 1, 0, 0)

        recovered_store = PostgresConversationStore(store.connection)
        recovered_store.claim_run_dispatch(
            dispatch_id=dispatch_id,
            run_id=run_id,
            thread_id=thread_id,
            dispatch_owner_id=owner_id,
            lease_epoch=1,
        )
        lifecycle = recovered_store.latest_lifecycle_state(run_id)
        assert lifecycle is not None
        assert lifecycle.state_revision == 1
        assert lifecycle.execution_state == "running"
    finally:
        cleanup_store = recovered_store or store
        _terminalize_postgres_dispatch_test_run(
            cleanup_store,
            thread_id=thread_id,
            run_id=run_id,
        )
        store.connection.close()


@pytest.mark.skipif(
    not (os.getenv("WAJE_RUNTIME_DATABASE_URL") or os.getenv("DATABASE_URL")),
    reason="runtime PostgreSQL is not configured",
)
def test_postgres_dispatch_claim_rejects_preexisting_lifecycle_without_mutation():
    store = PostgresConversationStore.from_env()
    store.apply_schema()
    suffix = uuid4().hex
    thread_id, run_id, dispatch_id, owner_id = _create_postgres_leased_dispatch(
        store,
        suffix=suffix,
    )
    lifecycle = LifecycleState.create(run_attempt_id=run_id)
    store.append_lifecycle_state(lifecycle)
    try:
        with pytest.raises(
            EvidenceIntegrityError,
            match="^run_dispatch_lifecycle_conflict$",
        ):
            store.claim_run_dispatch(
                dispatch_id=dispatch_id,
                run_id=run_id,
                thread_id=thread_id,
                dispatch_owner_id=owner_id,
                lease_epoch=1,
            )

        row = store.connection.execute(
            """
            SELECT run.status, dispatch.dispatch_state,
                   dispatch.owner_id, dispatch.lease_epoch, state.content_digest
            FROM waje_runtime.analysis_runs run
            JOIN waje_runtime.run_dispatches dispatch
              ON dispatch.run_id = run.run_id
            JOIN waje_runtime.run_lifecycle_state_revisions state
              ON state.run_attempt_id = run.run_id
             AND state.state_revision = 1
            WHERE run.run_id = %(run_id)s
            """,
            {"run_id": run_id},
        ).fetchone()
        assert tuple(row) == (
            "queued",
            "leased",
            owner_id,
            1,
            lifecycle.content_digest,
        )
    finally:
        _terminalize_postgres_dispatch_test_run(
            store,
            thread_id=thread_id,
            run_id=run_id,
        )
        store.connection.close()


def test_owned_dispatch_failure_finalizer_terminalizes_same_owner_atomically():
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

    finalized = store.finalize_run_failure(
        run_id="run-dispatch",
        thread_id="thread-dispatch",
        turn_id="",
        topic_id="",
        request={"question": "source question"},
        failure_reason="workflow_failed",
        failure_stage="workflow",
        failure_payload={"reason": "provider failed"},
    )

    assert finalized["failure_reason"] == "workflow_failed"
    assert connection.run["status"] == "failed"
    assert connection.dispatch["dispatch_state"] == "terminal"
    assert connection.dispatch["terminal_status"] == "failed"
    assert "run-dispatch" not in store._active_run_dispatches
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_stale_dispatch_owner_cannot_finalize_failure():
    connection = _RunDispatchOwnershipConnection(
        dispatch_state="running",
        run_status="running_workflow",
    )
    store = PostgresConversationStore(connection)
    store._active_run_dispatches["run-dispatch"] = (
        "dispatch-1",
        "owner-stale",
        3,
    )

    with pytest.raises(EvidenceIntegrityError, match="^run_dispatch_owner_lost$"):
        store.finalize_run_failure(
            run_id="run-dispatch",
            thread_id="thread-dispatch",
            turn_id="",
            topic_id="",
            request={"question": "source question"},
            failure_reason="workflow_failed",
            failure_stage="workflow",
            failure_payload={"reason": "provider failed"},
        )

    assert connection.run["status"] == "running_workflow"
    assert connection.dispatch["dispatch_state"] == "running"
    assert connection.commits == 0
    assert connection.rollbacks == 1


@pytest.mark.parametrize(
    ("dispatch_state", "run_status", "expected_dispatch", "expected_run"),
    [
        ("leased", "queued", "pending", "queued"),
        ("running", "running_workflow", "terminal", "failed"),
        ("terminal", "completed", "terminal", "completed"),
    ],
)
def test_expired_dispatch_sweeper_recovers_only_nonterminal_current_owner(
    dispatch_state, run_status, expected_dispatch, expected_run
):
    connection = _RunDispatchOwnershipConnection(
        dispatch_state=dispatch_state,
        run_status=run_status,
        lease_active=False,
    )

    recovered = PostgresConversationStore(connection).sweep_expired_run_dispatches()

    assert connection.dispatch["dispatch_state"] == expected_dispatch
    assert connection.run["status"] == expected_run
    if dispatch_state == "terminal":
        assert recovered == ()
    else:
        assert recovered[0]["run_id"] == "run-dispatch"


def test_expired_running_clarification_dispatch_is_released_for_recovery():
    connection = _RunDispatchOwnershipConnection(
        dispatch_state="running",
        run_status="waiting_for_clarification",
        lease_active=False,
        producer_kind="clarification_resolution",
    )

    recovered = PostgresConversationStore(connection).sweep_expired_run_dispatches()

    assert recovered == (
        {
            "dispatch_id": "dispatch-1",
            "run_id": "run-dispatch",
            "action": "released_for_retry",
        },
    )
    assert connection.run["status"] == "waiting_for_clarification"
    assert connection.dispatch["dispatch_state"] == "pending"
    assert connection.dispatch["owner_id"] is None
    assert connection.dispatch["terminal_status"] is None
    assert connection.dispatch["failure_reason"] is None


def test_expired_running_clarification_workflow_restores_replayable_state():
    connection = _RunDispatchOwnershipConnection(
        dispatch_state="running",
        run_status="running_workflow",
        lease_active=False,
        producer_kind="clarification_resolution",
    )
    connection.lifecycle = LifecycleState.create(
        run_attempt_id="run-dispatch",
        execution_state="complete",
        evidence_state="complete",
    ).to_dict()
    connection.run["request"] = {
        "plan_result_refs": {"plan_revision_id": "plan-dispatch"},
        "execution_result_refs": {
            "authoritative_execution_result_ref": "execution-dispatch"
        },
        "claim_coverage_refs": {
            "claim_coverage_checkpoint_ref": "coverage-dispatch"
        },
    }
    progress_request = deepcopy(connection.run["request"])

    recovered = PostgresConversationStore(connection).sweep_expired_run_dispatches()

    assert recovered == (
        {
            "dispatch_id": "dispatch-1",
            "run_id": "run-dispatch",
            "action": "released_for_retry",
        },
    )
    assert connection.run["status"] == "waiting_for_clarification"
    assert connection.run["request"] == progress_request
    assert connection.dispatch["dispatch_state"] == "pending"
    assert connection.dispatch["owner_id"] is None
    assert connection.lifecycle["retry_state"] == "scheduled"


def test_recovered_clarification_claim_advances_retry_epoch_in_lifecycle():
    connection = _RunDispatchOwnershipConnection(
        producer_kind="clarification_resolution",
        dispatch_state="leased",
        run_status="waiting_for_clarification",
    )
    prepared = LifecycleState.create(
        run_attempt_id="run-dispatch",
        execution_state="complete",
        evidence_state="complete",
    ).transition(retry_state="scheduled")
    connection.lifecycle = prepared.to_dict()
    store = PostgresConversationStore(connection)

    claimed = store.claim_run_dispatch(
        dispatch_id="dispatch-1",
        run_id="run-dispatch",
        thread_id="thread-dispatch",
        dispatch_owner_id="owner-current",
        lease_epoch=4,
    )

    assert claimed["dispatch_state"] == "running"
    assert claimed["resume_request"] == connection.dispatch["request_payload"][
        "resumeRequest"
    ]
    assert connection.lifecycle["state_revision"] == prepared.state_revision + 1
    assert connection.lifecycle["retry_state"] == "running"


def test_recovery_driver_starts_committed_pending_dispatch_without_client_retry():
    from tools.runtime.recover_run_dispatches import recover_pending_run_dispatches

    lease = {
        "dispatch_id": "dispatch-crash-window",
        "run_id": "run-crash-window",
        "thread_id": "thread-crash-window",
        "producer_kind": "thread_message",
        "scope_ref": "thread-crash-window",
        "request_identity": "request-crash-window",
        "request_payload": {
            "message": "检查昨天付费金额",
        },
        "dispatch_owner_id": "recovery-owner-1",
        "lease_epoch": 1,
    }

    class CrashWindowStore:
        def __init__(self):
            self.leased = False
            self.failed = []

        def sweep_expired_run_dispatches(self, *, limit):
            return ()

        def lease_recoverable_run_dispatches(self, *, limit):
            self.leased = True
            return (lease,)

        def fail_owned_run_dispatch(self, **kwargs):
            self.failed.append(kwargs)

    store = CrashWindowStore()
    started = []

    summary = recover_pending_run_dispatches(
        store=store,
        dispatch_runner=lambda dispatch: (
            started.append(dispatch) or {"status": "completed"}
        ),
        limit=10,
    )

    assert store.leased is True
    assert started == [lease]
    assert store.failed == []
    assert summary == {
        "swept": [],
        "leased": ["run-crash-window"],
        "dispatched": [{"run_id": "run-crash-window", "status": "completed"}],
        "failed": [],
    }


def test_postgres_recovery_leases_pending_dispatch_with_db_epoch_fence():
    connection = _RunDispatchOwnershipConnection(
        owner_id="",
        dispatch_state="pending",
        run_status="queued",
        lease_active=False,
    )

    leases = PostgresConversationStore(connection).lease_recoverable_run_dispatches(
        limit=1
    )

    assert len(leases) == 1
    lease = leases[0]
    assert lease["run_id"] == "run-dispatch"
    assert lease["thread_id"] == "thread-dispatch"
    assert lease["producer_kind"] == "thread_message"
    assert lease["request_payload"] == {
        "message": "检查昨天付费金额",
    }
    assert lease["dispatch_owner_id"].startswith("recovery-dispatch-")
    assert lease["lease_epoch"] == 5
    assert connection.dispatch["dispatch_state"] == "leased"
    assert connection.dispatch["owner_id"] == lease["dispatch_owner_id"]
    assert connection.dispatch["lease_epoch"] == 5
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_postgres_recovery_failure_is_owner_cas_terminal():
    connection = _RunDispatchOwnershipConnection(
        owner_id="recovery-owner-1",
        lease_epoch=5,
        dispatch_state="leased",
        run_status="queued",
        lease_active=True,
    )

    failed = PostgresConversationStore(connection).fail_owned_run_dispatch(
        dispatch_id="dispatch-1",
        run_id="run-dispatch",
        thread_id="thread-dispatch",
        dispatch_owner_id="recovery-owner-1",
        lease_epoch=5,
        failure_reason="run_dispatch_recovery_worker_failed",
    )

    assert failed["status"] == "failed"
    assert failed["failure_reason"] == "run_dispatch_recovery_worker_failed"
    assert connection.run["status"] == "failed"
    assert connection.dispatch["dispatch_state"] == "terminal"
    assert connection.dispatch["terminal_status"] == "failed"
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_postgres_recovery_failure_replays_same_owner_terminal_authority():
    connection = _RunDispatchOwnershipConnection(
        owner_id="recovery-owner-terminal",
        lease_epoch=6,
        dispatch_state="terminal",
        run_status="completed",
        lease_active=False,
    )
    connection.dispatch["terminal_status"] = "completed"
    before_run = deepcopy(connection.run)
    before_dispatch = deepcopy(connection.dispatch)

    durable = PostgresConversationStore(connection).fail_owned_run_dispatch(
        dispatch_id="dispatch-1",
        run_id="run-dispatch",
        thread_id="thread-dispatch",
        dispatch_owner_id="recovery-owner-terminal",
        lease_epoch=6,
        failure_reason="run_dispatch_recovery_worker_failed",
    )

    assert durable["status"] == "completed"
    assert connection.run == before_run
    assert connection.dispatch == before_dispatch
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_recovery_driver_owner_terminalizes_worker_start_failure():
    from tools.runtime.recover_run_dispatches import recover_pending_run_dispatches

    lease = {
        "dispatch_id": "dispatch-recovery-failure",
        "run_id": "run-recovery-failure",
        "thread_id": "thread-recovery-failure",
        "producer_kind": "thread_message",
        "scope_ref": "thread-recovery-failure",
        "request_identity": "request-recovery-failure",
        "request_payload": {
            "message": "继续分析",
        },
        "dispatch_owner_id": "recovery-owner-2",
        "lease_epoch": 2,
    }

    class RecoveryFailureStore:
        def __init__(self):
            self.failed = []

        def sweep_expired_run_dispatches(self, *, limit):
            return ()

        def lease_recoverable_run_dispatches(self, *, limit):
            return (lease,)

        def fail_owned_run_dispatch(self, **kwargs):
            self.failed.append(kwargs)
            return {"status": "failed"}

    store = RecoveryFailureStore()

    def fail_to_start(_dispatch):
        raise RuntimeError("worker bootstrap unavailable")

    summary = recover_pending_run_dispatches(
        store=store,
        dispatch_runner=fail_to_start,
        limit=10,
    )

    assert store.failed == [
        {
            "dispatch_id": "dispatch-recovery-failure",
            "run_id": "run-recovery-failure",
            "thread_id": "thread-recovery-failure",
            "dispatch_owner_id": "recovery-owner-2",
            "lease_epoch": 2,
            "failure_reason": "run_dispatch_recovery_worker_failed",
        }
    ]
    assert summary["failed"] == [
        {
            "run_id": "run-recovery-failure",
            "failure_reason": "run_dispatch_recovery_worker_failed",
            "error_type": "RuntimeError",
        }
    ]


def test_recovery_driver_continues_batch_after_terminal_owner_then_throw():
    from tools.runtime.recover_run_dispatches import recover_pending_run_dispatches

    leases = tuple(
        {
            "dispatch_id": f"dispatch-{run_id}",
            "run_id": run_id,
            "thread_id": "thread-recovery-batch",
            "producer_kind": "thread_message",
            "scope_ref": "thread-recovery-batch",
            "request_identity": f"request-{run_id}",
            "request_payload": {
                "message": "检查昨天付费金额",
            },
            "dispatch_owner_id": f"owner-{run_id}",
            "lease_epoch": index,
        }
        for index, run_id in enumerate(
            ("run-terminal-then-throw", "run-next"),
            start=1,
        )
    )

    class TerminalThenThrowStore:
        def __init__(self):
            self.finalized = []

        def sweep_expired_run_dispatches(self, *, limit):
            return ()

        def lease_recoverable_run_dispatches(self, *, limit):
            return leases

        def fail_owned_run_dispatch(self, **kwargs):
            self.finalized.append(kwargs)
            return {"status": "completed"}

    store = TerminalThenThrowStore()

    def dispatch(lease):
        if lease["run_id"] == "run-terminal-then-throw":
            raise RuntimeError("response serialization failed after terminal commit")
        return {"status": "completed", "run_id": lease["run_id"]}

    summary = recover_pending_run_dispatches(
        store=store,
        dispatch_runner=dispatch,
        limit=10,
    )

    assert [item["run_id"] for item in summary["dispatched"]] == [
        "run-terminal-then-throw",
        "run-next",
    ]
    assert summary["failed"] == []
    assert len(store.finalized) == 1


@pytest.mark.parametrize(
    (
        "producer_kind",
        "payload",
        "expected_message",
    ),
    [
        (
            "thread_message",
            {
                "message": "检查昨天付费金额",
            },
            "检查昨天付费金额",
        ),
    ],
)
def test_recovery_runner_rehydrates_every_dispatch_producer(
    producer_kind,
    payload,
    expected_message,
):
    from tools.runtime.recover_run_dispatches import run_agent_core_dispatch

    calls = []

    class Core:
        store = SimpleNamespace(connection=SimpleNamespace(close=lambda: None))

        def run_message(self, **kwargs):
            calls.append(kwargs)
            return {"status": "completed", "run_id": kwargs["run_id"]}

    lease = {
        "dispatch_id": "dispatch-recovery",
        "run_id": "run-recovery",
        "thread_id": "thread-recovery",
        "producer_kind": producer_kind,
        "scope_ref": "thread-recovery",
        "request_identity": "request-recovery",
        "request_payload": payload,
        "dispatch_owner_id": "recovery-owner-3",
        "lease_epoch": 3,
    }
    with patch(
        "tools.runtime.recover_run_dispatches.ConversationAgentCore.from_environment",
        return_value=Core(),
    ):
        result = run_agent_core_dispatch(lease)

    assert result == {"status": "completed", "run_id": "run-recovery"}
    assert calls == [
        {
            "thread_id": "thread-recovery",
            "run_id": "run-recovery",
            "user_message": expected_message,
            "run_dispatch": {
                "dispatch_id": "dispatch-recovery",
                "dispatch_owner_id": "recovery-owner-3",
                "lease_epoch": 3,
            },
        }
    ]


@pytest.mark.parametrize(
    "attempted_status",
    ("failed", "running", "waiting_for_clarification"),
)
def test_postgres_completed_run_status_compare_and_swap_rejects_downgrade(
    attempted_status: str,
) -> None:
    completed_request = {"material_authority": {"signature": "persisted"}}
    connection = _RunStatusConnection(
        status="completed",
        request=completed_request,
    )

    with pytest.raises(
        EvidenceIntegrityError,
        match="^analysis_run_status_transition_conflict$",
    ):
        PostgresConversationStore(connection).upsert_run(
            "run-source",
            thread_id="thread-1",
            topic_id="topic-1",
            status=attempted_status,
            request={"failure_reason": "late client error"},
        )

    assert connection.status == "completed"
    assert connection.request == completed_request
    assert connection.audit_events == []


def test_postgres_nonterminal_run_can_still_transition_to_failed() -> None:
    connection = _RunStatusConnection(
        status="running_workflow",
        request={"question": "source question"},
    )

    PostgresConversationStore(connection).upsert_run(
        "run-real-failure",
        thread_id="thread-1",
        topic_id="topic-1",
        status="failed",
        request={"failure_reason": "workflow_failed"},
    )

    assert connection.status == "failed"
    assert connection.request == {"failure_reason": "workflow_failed"}
    assert len(connection.audit_events) == 1


@pytest.mark.parametrize("backend", ("inmemory", "postgres"))
def test_failure_finalization_rolls_back_when_primary_audit_cannot_commit(
    backend: str,
) -> None:
    if backend == "inmemory":

        class PrimaryAuditFailingStore(InMemoryConversationStore):
            def _append_staged_audit_event(self, events, event):
                if event.get("event_type") == "analysis_delivery_persistence_failed":
                    raise RuntimeError("primary failure audit unavailable")
                return super()._append_staged_audit_event(events, event)

        store = PrimaryAuditFailingStore()
        store.upsert_run(
            "run-transition",
            thread_id="thread-1",
            turn_id="turn-1",
            topic_id="topic-1",
            status="running_workflow",
            request={"question": "检查付费金额"},
        )
    else:
        connection = _RunStatusConnection(
            status="running_workflow",
            request={"question": "检查付费金额"},
            turn_id="turn-1",
            fail_audit=True,
        )
        store = PostgresConversationStore(connection)

    with pytest.raises(RuntimeError, match="audit.*unavailable"):
        store.finalize_run_failure(
            run_id="run-transition",
            thread_id="thread-1",
            turn_id="turn-1",
            topic_id="topic-1",
            request={"question": "检查付费金额"},
            failure_reason="analysis_delivery_persistence_failed",
            failure_stage="publication",
            failure_payload={"reason": "publication delivery unavailable"},
        )

    state = store.get_run_state("run-transition")
    assert state is not None
    assert state["status"] == "running_workflow"


@pytest.mark.parametrize("backend", ("inmemory", "postgres"))
def test_failure_finalization_fills_owner_once_and_replays_exactly(
    backend: str,
) -> None:
    if backend == "inmemory":
        store = InMemoryConversationStore()
        store.upsert_run(
            "run-transition",
            thread_id="thread-1",
            status="running_workflow",
            request={"question": "检查付费金额"},
        )
    else:
        connection = _RunStatusConnection(
            status="running_workflow",
            request={"question": "检查付费金额"},
            thread_id="thread-1",
            turn_id="",
            topic_id="",
        )
        store = PostgresConversationStore(connection)

    arguments = {
        "run_id": "run-transition",
        "thread_id": "thread-1",
        "turn_id": "turn-1",
        "topic_id": "topic-1",
        "request": {"question": "检查付费金额"},
        "failure_reason": "analysis_delivery_persistence_failed",
        "failure_stage": "publication",
        "failure_payload": {"reason": "delivery unavailable"},
    }
    store.finalize_run_failure(**arguments)
    first_state = store.get_run_state("run-transition")
    first_audits = (
        store.audit_events
        if backend == "inmemory"
        else deepcopy(connection.audit_events)
    )
    store.finalize_run_failure(**arguments)

    assert first_state is not None
    assert first_state["turn_id"] == "turn-1"
    assert first_state["topic_id"] == "topic-1"
    assert store.get_run_state("run-transition") == first_state
    assert (
        store.audit_events if backend == "inmemory" else connection.audit_events
    ) == first_audits


def test_postgres_gateway_created_queued_row_enters_agent_core_lifecycle() -> None:
    connection = _RunStatusConnection(
        status="queued",
        request={},
        thread_id="thread-gateway",
        turn_id="",
        topic_id="",
    )
    store = PostgresConversationStore(connection)
    store.get_thread = lambda _thread_id: SimpleNamespace(owner_id="user-gateway")

    with patch(
        "bi_agent.conversation.agent_core.ConversationRuntime.handle_message",
        side_effect=RuntimeError("conversation entry failed"),
    ):
        with pytest.raises(RuntimeError, match="conversation entry failed"):
            ConversationAgentCore(store).run_message(
                thread_id="thread-gateway",
                run_id="run-transition",
                user_message="检查昨天付费金额",
            )

    transitioned_statuses = [
        params["status"]
        for statement, params in connection.statements
        if "analysis_run_status_transition_cas" in statement
    ]
    assert transitioned_statuses == ["running", "failed"]
    assert connection.status == "failed"
    assert len(connection.audit_events) == 2


def test_agent_core_rejects_user_outside_thread_owner_before_run_start() -> None:
    store = InMemoryConversationStore()
    store.create_thread("thread-personal", owner_id="user-1")

    with pytest.raises(EvidenceIntegrityError, match="^thread_owner_mismatch$"):
        ConversationAgentCore(store).run_message(
            thread_id="thread-personal",
            run_id="run-owner-mismatch",
            user_message="检查昨天付费金额",
            user_id="user-2",
        )

    assert store.get_thread("thread-personal").owner_id == "user-1"
    assert store.get_run_state("run-owner-mismatch") is None


def test_python_startup_ack_failure_terminalizes_claimed_running_run() -> None:
    store = InMemoryConversationStore()
    store.create_thread("thread-startup-ack", owner_id="owner-startup")

    with patch.dict(
        "os.environ",
        {"WAJE_AGENT_CORE_STARTUP_ACK_FD": "999999"},
        clear=False,
    ):
        with pytest.raises(RuntimeError, match="agent_core_startup_ack_failed"):
            ConversationAgentCore(store).run_message(
                thread_id="thread-startup-ack",
                run_id="run-startup-ack",
                user_message="检查付费金额",
            )

    state = store.get_run_state("run-startup-ack")
    assert state is not None
    assert state["status"] == "failed"
    assert state["request"]["failure_reason"] == "conversation_orchestration_failed"


@pytest.mark.parametrize("next_status", ("running", "failed"))
def test_gateway_queued_status_allows_only_declared_entry_transitions(
    next_status: str,
) -> None:
    connection = _RunStatusConnection(
        status="queued",
        request={},
        topic_id="",
    )

    PostgresConversationStore(connection).upsert_run(
        "run-transition",
        thread_id="thread-1",
        status=next_status,
        request={},
    )

    assert connection.status == next_status


@pytest.mark.parametrize(
    "next_status",
    (
        "running_workflow",
        "waiting_for_clarification",
        "completed",
        "interaction_completed",
    ),
)
def test_gateway_queued_status_rejects_skipped_runtime_phases(
    next_status: str,
) -> None:
    connection = _RunStatusConnection(
        status="queued",
        request={},
        topic_id="",
    )

    with pytest.raises(
        EvidenceIntegrityError,
        match="^analysis_run_status_transition_conflict$",
    ):
        PostgresConversationStore(connection).upsert_run(
            "run-transition",
            thread_id="thread-1",
            status=next_status,
            request={},
        )

    assert connection.status == "queued"


def _run_store(
    backend: str,
    *,
    status: str,
    request: dict,
    thread_id: str = "thread-1",
    turn_id: str = "turn-1",
    topic_id: str = "topic-1",
):
    if backend == "inmemory":
        store = InMemoryConversationStore()
        store.upsert_run(
            "run-transition",
            thread_id=thread_id,
            turn_id=turn_id,
            topic_id=topic_id,
            status=status,
            request=request,
        )
        return store, store
    connection = _RunStatusConnection(
        status=status,
        request=request,
        thread_id=thread_id,
        turn_id=turn_id,
        topic_id=topic_id,
    )
    return PostgresConversationStore(connection), connection


def _run_state(backend: str, authority) -> dict | None:
    if backend == "inmemory":
        run = authority.runs.get("run-transition")
        if run is None:
            return None
        return {
            "status": str(run.get("status") or ""),
            "thread_id": str(run.get("thread_id") or ""),
            "turn_id": str(run.get("turn_id") or ""),
            "topic_id": str(run.get("topic_id") or ""),
            "request": deepcopy(run.get("request") or {}),
        }
    if not authority.exists:
        return None
    return {
        "status": authority.status,
        "thread_id": authority.thread_id,
        "turn_id": authority.turn_id,
        "topic_id": authority.topic_id,
        "request": deepcopy(authority.request),
    }


def _run_audit_count(backend: str, authority) -> int:
    if backend == "inmemory":
        return len(authority.audit_events)
    return len(authority.audit_events)


@pytest.mark.parametrize("backend", ("inmemory", "postgres"))
@pytest.mark.parametrize(
    ("current_status", "next_status"),
    (
        (None, "unknown_runtime_state"),
        ("unknown_runtime_state", "unknown_runtime_state"),
        ("unknown_runtime_state", "failed"),
        ("running", "unknown_runtime_state"),
    ),
    ids=("fresh", "replay", "transition-from", "transition-to"),
)
def test_unknown_run_status_values_fail_closed(
    backend: str,
    current_status: str | None,
    next_status: str,
) -> None:
    request = {"question": "persisted"}
    if backend == "inmemory":
        store = InMemoryConversationStore()
        authority = store
        if current_status is not None:
            store.runs["run-transition"] = {
                "run_id": "run-transition",
                "thread_id": "thread-1",
                "turn_id": "turn-1",
                "topic_id": "topic-1",
                "status": current_status,
                "request": deepcopy(request),
                "checkpoint_events": [],
            }
    else:
        authority = _RunStatusConnection(
            status=current_status or "",
            request=request,
            exists=current_status is not None,
            turn_id="turn-1",
        )
        store = PostgresConversationStore(authority)
    before = _run_state(backend, authority)
    before_audits = _run_audit_count(backend, authority)

    with pytest.raises(
        EvidenceIntegrityError,
        match="^analysis_run_status_invalid$",
    ):
        store.upsert_run(
            "run-transition",
            thread_id="thread-1",
            turn_id="turn-1",
            topic_id="topic-1",
            status=next_status,
            request=request,
        )

    assert _run_state(backend, authority) == before
    assert _run_audit_count(backend, authority) == before_audits


@pytest.mark.parametrize("backend", ("inmemory", "postgres"))
@pytest.mark.parametrize(
    ("current_status", "attempted_status"),
    (
        ("interaction_completed", "failed"),
        ("waiting_for_clarification", "running"),
        ("failed", "running"),
        ("completed", "failed"),
    ),
)
def test_terminal_run_statuses_reject_cross_status_overwrite(
    backend: str,
    current_status: str,
    attempted_status: str,
) -> None:
    store, authority = _run_store(
        backend,
        status=current_status,
        request={"question": "persisted"},
    )
    before = _run_state(backend, authority)
    before_audits = _run_audit_count(backend, authority)

    with pytest.raises(
        EvidenceIntegrityError,
        match="^analysis_run_status_transition_conflict$",
    ):
        store.upsert_run(
            "run-transition",
            thread_id="thread-1",
            turn_id="turn-1",
            topic_id="topic-1",
            status=attempted_status,
            request={"failure_reason": "late overwrite"},
        )

    assert _run_state(backend, authority) == before
    assert _run_audit_count(backend, authority) == before_audits


@pytest.mark.parametrize("backend", ("inmemory", "postgres"))
@pytest.mark.parametrize(
    ("current_status", "next_status"),
    (
        ("running", "running_workflow"),
        ("running", "waiting_for_clarification"),
        ("running", "interaction_completed"),
        ("running", "failed"),
        ("running_workflow", "waiting_for_clarification"),
        ("running_workflow", "completed"),
        ("running_workflow", "failed"),
        ("waiting_for_clarification", "running_workflow"),
    ),
)
def test_run_status_table_allows_only_declared_forward_transitions(
    backend: str,
    current_status: str,
    next_status: str,
) -> None:
    initial_turn_id = "" if current_status == "running" else "turn-1"
    initial_topic_id = "" if current_status == "running" else "topic-1"
    store, authority = _run_store(
        backend,
        status=current_status,
        request={},
        turn_id=initial_turn_id,
        topic_id=initial_topic_id,
    )
    before_audits = _run_audit_count(backend, authority)

    store.upsert_run(
        "run-transition",
        thread_id="thread-1",
        turn_id="turn-1",
        topic_id="topic-1",
        status=next_status,
        request={"question": "persisted business request"},
    )

    assert _run_state(backend, authority) == {
        "status": next_status,
        "thread_id": "thread-1",
        "turn_id": "turn-1",
        "topic_id": "topic-1",
        "request": {"question": "persisted business request"},
    }
    assert _run_audit_count(backend, authority) == before_audits + 1


class _AuditFailingInMemoryStore(InMemoryConversationStore):
    fail_run_status_audit = False

    def _append_staged_audit_event(self, events, event):
        if (
            self.fail_run_status_audit
            and event.get("event_type") == "run_status_changed"
        ):
            raise RuntimeError("run_status_audit_unavailable")
        return super()._append_staged_audit_event(events, event)


@pytest.mark.parametrize(
    ("current_status", "next_status"),
    ((None, "running"), ("running_workflow", "failed")),
    ids=("fresh", "transition"),
)
def test_inmemory_run_and_audit_write_are_atomic_when_audit_append_fails(
    current_status: str | None,
    next_status: str,
) -> None:
    store = _AuditFailingInMemoryStore()
    if current_status is not None:
        store.upsert_run(
            "run-atomic",
            thread_id="thread-1",
            turn_id="turn-1",
            topic_id="topic-1",
            status=current_status,
            request={"question": "persisted"},
        )
    before_runs = deepcopy(store.runs)
    before_audits = store.audit_events
    store.fail_run_status_audit = True

    with pytest.raises(RuntimeError, match="^run_status_audit_unavailable$"):
        store.upsert_run(
            "run-atomic",
            thread_id="thread-1",
            turn_id="turn-1",
            topic_id="topic-1",
            status=next_status,
            request={"question": "next"},
        )

    assert store.runs == before_runs
    assert store.audit_events == before_audits


def test_inmemory_run_request_does_not_retain_nested_caller_references() -> None:
    store = InMemoryConversationStore()
    request = {
        "question": "persisted",
        "context": {
            "target": "2026-06-02",
            "baselines": ["previous_day"],
        },
    }
    store.upsert_run(
        "run-request-copy",
        thread_id="thread-1",
        turn_id="turn-1",
        topic_id="topic-1",
        status="running_workflow",
        request=request,
    )
    before_audits = store.audit_events

    request["context"]["target"] = "tampered"
    request["context"]["baselines"].append("same_weekday")

    assert store.runs["run-request-copy"]["request"] == {
        "question": "persisted",
        "context": {
            "target": "2026-06-02",
            "baselines": ["previous_day"],
        },
    }
    assert store.audit_events == before_audits


@pytest.mark.parametrize(
    ("current_status", "next_status"),
    ((None, "running"), ("running_workflow", "failed")),
    ids=("fresh", "transition"),
)
def test_postgres_run_and_audit_write_rollback_together_on_audit_failure(
    current_status: str | None,
    next_status: str,
) -> None:
    connection = _RunStatusConnection(
        status=current_status or "",
        request={"question": "persisted"},
        exists=current_status is not None,
        turn_id="turn-1",
        fail_audit=True,
    )
    store = PostgresConversationStore(connection)
    before = _run_state("postgres", connection)

    with pytest.raises(RuntimeError, match="^run_status_audit_unavailable$"):
        store.upsert_run(
            "run-transition",
            thread_id="thread-1",
            turn_id="turn-1",
            topic_id="topic-1",
            status=next_status,
            request={"question": "next"},
        )

    assert _run_state("postgres", connection) == before
    assert connection.audit_events == []
    assert connection.commits == 0
    assert connection.rollbacks == 1


@pytest.mark.parametrize(
    ("current_status", "next_status"),
    ((None, "running"), ("running_workflow", "failed")),
    ids=("fresh", "transition"),
)
def test_postgres_run_and_audit_write_commit_once_after_audit(
    current_status: str | None,
    next_status: str,
) -> None:
    connection = _RunStatusConnection(
        status=current_status or "",
        request={"question": "persisted"},
        exists=current_status is not None,
        turn_id="turn-1",
    )

    PostgresConversationStore(connection).upsert_run(
        "run-transition",
        thread_id="thread-1",
        turn_id="turn-1",
        topic_id="topic-1",
        status=next_status,
        request={"question": "next"},
    )

    assert connection.status == next_status
    assert connection.request == {"question": "next"}
    assert len(connection.audit_events) == 1
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.transaction_events == ["audit", "commit"]


def test_postgres_exact_replay_does_not_attempt_audit() -> None:
    request = {
        "question": "persisted",
        "context": {"target": "2026-06-02"},
    }
    connection = _RunStatusConnection(
        status="running_workflow",
        request=request,
        turn_id="turn-1",
    )
    before = _run_state("postgres", connection)

    PostgresConversationStore(connection).upsert_run(
        "run-transition",
        thread_id="thread-1",
        turn_id="turn-1",
        topic_id="topic-1",
        status="running_workflow",
        request={
            "context": {"target": "2026-06-02"},
            "question": "persisted",
        },
    )

    assert _run_state("postgres", connection) == before
    assert connection.audit_attempts == 0
    assert connection.audit_events == []


@pytest.mark.parametrize("backend", ("inmemory", "postgres"))
@pytest.mark.parametrize(
    "status",
    (
        "running",
        "running_workflow",
        "waiting_for_clarification",
        "completed",
        "interaction_completed",
        "failed",
    ),
)
def test_exact_same_status_replay_is_idempotent_without_duplicate_audit(
    backend: str,
    status: str,
) -> None:
    request = {
        "question": "persisted",
        "context": {"target": "2026-06-02", "baselines": ["previous_day"]},
    }
    store, authority = _run_store(
        backend,
        status=status,
        request=request,
    )
    before = _run_state(backend, authority)
    before_audits = _run_audit_count(backend, authority)

    store.upsert_run(
        "run-transition",
        thread_id="thread-1",
        turn_id="turn-1",
        topic_id="topic-1",
        status=status,
        request={
            "context": {
                "baselines": ["previous_day"],
                "target": "2026-06-02",
            },
            "question": "persisted",
        },
    )

    assert _run_state(backend, authority) == before
    assert _run_audit_count(backend, authority) == before_audits


@pytest.mark.parametrize("backend", ("inmemory", "postgres"))
def test_same_status_replay_rejects_request_drift(backend: str) -> None:
    store, authority = _run_store(
        backend,
        status="running_workflow",
        request={"question": "persisted"},
    )
    before = _run_state(backend, authority)
    before_audits = _run_audit_count(backend, authority)

    with pytest.raises(
        EvidenceIntegrityError,
        match="^analysis_run_status_transition_conflict$",
    ):
        store.upsert_run(
            "run-transition",
            thread_id="thread-1",
            turn_id="turn-1",
            topic_id="topic-1",
            status="running_workflow",
            request={"question": "drifted"},
        )

    assert _run_state(backend, authority) == before
    assert _run_audit_count(backend, authority) == before_audits


@pytest.mark.parametrize("backend", ("inmemory", "postgres"))
@pytest.mark.parametrize("owner_axis", ("thread_id", "turn_id", "topic_id"))
def test_same_status_replay_rejects_owner_drift(
    backend: str,
    owner_axis: str,
) -> None:
    store, authority = _run_store(
        backend,
        status="running_workflow",
        request={"question": "persisted"},
    )
    before = _run_state(backend, authority)
    before_audits = _run_audit_count(backend, authority)
    owner = {
        "thread_id": "thread-1",
        "turn_id": "turn-1",
        "topic_id": "topic-1",
    }
    owner[owner_axis] = f"{owner_axis}-drifted"

    with pytest.raises(
        EvidenceIntegrityError,
        match="^analysis_run_status_transition_conflict$",
    ):
        store.upsert_run(
            "run-transition",
            **owner,
            status="running_workflow",
            request={"question": "persisted"},
        )

    assert _run_state(backend, authority) == before
    assert _run_audit_count(backend, authority) == before_audits


@pytest.mark.parametrize("backend", ("inmemory", "postgres"))
@pytest.mark.parametrize("owner_axis", ("thread_id", "turn_id", "topic_id"))
def test_legal_status_transition_rejects_existing_owner_drift(
    backend: str,
    owner_axis: str,
) -> None:
    store, authority = _run_store(
        backend,
        status="running_workflow",
        request={"question": "persisted"},
    )
    before = _run_state(backend, authority)
    before_audits = _run_audit_count(backend, authority)
    owner = {
        "thread_id": "thread-1",
        "turn_id": "turn-1",
        "topic_id": "topic-1",
    }
    owner[owner_axis] = f"{owner_axis}-drifted"

    with pytest.raises(
        EvidenceIntegrityError,
        match="^analysis_run_status_transition_conflict$",
    ):
        store.upsert_run(
            "run-transition",
            **owner,
            status="failed",
            request={"failure_reason": "real failure"},
        )

    assert _run_state(backend, authority) == before
    assert _run_audit_count(backend, authority) == before_audits
