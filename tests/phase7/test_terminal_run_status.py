from __future__ import annotations

import json
from copy import deepcopy

import pytest

from bi_agent.conversation import run_status as run_status_policy
from bi_agent.conversation.agent_core import ConversationAgentCore
from bi_agent.conversation.postgres_store import PostgresConversationStore
from bi_agent.conversation.store import InMemoryConversationStore
from bi_agent.runtime.evidence_authority import EvidenceIntegrityError
from tests.phase7.test_agent_core_bridge import (
    _completed_runtime_workflow_result,
    _queryless_runtime_records_for_request,
    fake_workflow,
)
from tests.phase7.test_clarification_resume_authority import _seed_memory_store


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
    store, _ = _seed_memory_store()
    material_authority = deepcopy(
        store.runs["run-source"]["request"]["material_authority"]
    )
    store.runs["run-source"]["status"] = "running_workflow"
    store.finalize_completed_material_authority(
        run_id="run-source",
        thread_id="thread-1",
        topic_id="topic-1",
        request={"question": "source question"},
        material_authority=material_authority,
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
        self._pending_audit_events: list[dict] = []
        self.audit_events: list[dict] = []
        self.statements: list[tuple[str, dict]] = []
        self.transaction_events: list[str] = []
        self.audit_attempts = 0
        self.fail_audit = fail_audit
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

    def execute(self, statement, params=None):
        params = dict(params or {})
        self.statements.append((statement, params))
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
            if run is None or run["status"] != str(
                params.get("current_status") or ""
            ):
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
        if "INSERT INTO waje_runtime.audit_events" in statement:
            self.audit_attempts += 1
            self.transaction_events.append("audit")
            if self.fail_audit:
                raise RuntimeError("run_status_audit_unavailable")
            self._pending_audit_events.append(deepcopy(params))
        return _Cursor()

    def commit(self):
        self.transaction_events.append("commit")
        if self._pending_run is not None:
            self._committed_run = deepcopy(self._pending_run)
        self.audit_events.extend(deepcopy(self._pending_audit_events))
        self._pending_run = None
        self._pending_audit_events = []
        self.commits += 1

    def rollback(self):
        self.transaction_events.append("rollback")
        self._pending_run = None
        self._pending_audit_events = []
        self.rollbacks += 1


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
                "answer_package": None,
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
        ("completed_without_workflow", "failed"),
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
        ("running", "completed_without_workflow"),
        ("running", "failed"),
        ("running_workflow", "waiting_for_clarification"),
        ("running_workflow", "completed"),
        ("running_workflow", "failed"),
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
        "completed_without_workflow",
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


class _CommitThenRaiseStore(InMemoryConversationStore):
    def __init__(self, finalizer_error: Exception):
        super().__init__()
        self.finalizer_error = finalizer_error

    def finalize_completed_material_authority(self, **kwargs):
        super().finalize_completed_material_authority(**kwargs)
        raise self.finalizer_error


@pytest.mark.parametrize(
    "finalizer_error",
    (
        RuntimeError("client failed after committed completion"),
        EvidenceIntegrityError("completed_followup_authority_record_conflict"),
    ),
)
def test_agent_core_recovers_committed_completion_before_classifying_finalizer_error(
    finalizer_error: Exception,
) -> None:
    def workflow(request):
        result = fake_workflow(request)
        records = _queryless_runtime_records_for_request(request)
        return _completed_runtime_workflow_result(
            request,
            answer_package=result.answer_package,
            records=records,
            artifact_path="",
        )

    store = _CommitThenRaiseStore(finalizer_error)
    result = ConversationAgentCore(store, workflow_runner=workflow).run_message(
        thread_id="thread-commit-then-error",
        run_id="run-commit-then-error",
        user_message="当前付费金额的数据边界是什么？",
    )

    assert result["status"] == "completed"
    assert store.runs["run-commit-then-error"]["status"] == "completed"
    authority = store.resolve_completed_material_authority(
        source_run_id="run-commit-then-error",
        thread_id="thread-commit-then-error",
        topic_id=result["topic_id"],
    )
    assert authority["source_run_id"] == "run-commit-then-error"
    assert not any(
        event["event_type"]
        == "completed_material_authority_finalization_failed"
        for event in store.audit_events
    )


def test_agent_core_keeps_real_nonterminal_finalizer_failure_failed() -> None:
    class FailingBeforeCommitStore(InMemoryConversationStore):
        def finalize_completed_material_authority(self, **_kwargs):
            raise EvidenceIntegrityError(
                "completed_followup_authority_anchor_unavailable"
            )

    def workflow(request):
        result = fake_workflow(request)
        return type(result)(
            status=result.status,
            run_id=result.run_id,
            answer_package=result.answer_package,
            artifact_path=result.artifact_path,
            completed_material_authority={"invalid": "store owns validation"},
        )

    store = FailingBeforeCommitStore()
    result = ConversationAgentCore(store, workflow_runner=workflow).run_message(
        thread_id="thread-real-finalizer-failure",
        run_id="run-real-finalizer-failure",
        user_message="昨天付费金额为什么变化？",
    )

    assert result["status"] == "failed"
    assert store.runs["run-real-finalizer-failure"]["status"] == "failed"
