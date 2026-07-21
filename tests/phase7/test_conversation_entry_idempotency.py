from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import os
from threading import Event, Lock
from types import SimpleNamespace
from uuid import uuid4

import pytest

from bi_agent.conversation.agent_core import (
    ConversationAgentCore,
    _bind_single_authority_free_text,
)
from bi_agent.conversation.postgres_store import PostgresConversationStore
from bi_agent.conversation.runtime import ConversationRuntime
from bi_agent.conversation.store import InMemoryConversationStore
from bi_agent.runtime.durable_call_journal import (
    DurableCallJournalError,
    InMemoryDurableCallJournal,
)
from bi_agent.runtime.evidence_authority import (
    EvidenceIntegrityError,
    canonical_value,
)
from bi_agent.runtime.langgraph_workflow import WorkflowRunResult
from bi_agent.runtime.llm_client import LLMProviderError
from bi_agent.runtime.single_authority import DecisionLedger


def _new_topic_output(*, relation: str = "new_topic") -> dict[str, object]:
    return {
        "intent": "new_topic",
        "topic_relation": relation,
        "business_summary": "用户提出一个新的付费金额分析问题。",
        "confidence": 0.98,
        "display_summary": "已建立新的分析问题。",
        "selected_topic_id": None,
        "topic_options": [],
        "recommended_topic_id": None,
    }


class _CountingClient:
    def __init__(
        self,
        output: dict[str, object],
        *,
        entered: Event | None = None,
        release: Event | None = None,
    ) -> None:
        self.output = output
        self.entered = entered
        self.release = release
        self.calls: list[dict[str, object]] = []
        self._lock = Lock()

    def invoke_json(self, **kwargs):
        with self._lock:
            self.calls.append(dict(kwargs))
        if self.entered is not None:
            self.entered.set()
        if self.release is not None and not self.release.wait(timeout=5):
            raise AssertionError("provider_release_timeout")
        return SimpleNamespace(
            output=dict(self.output),
            audit={
                "task": kwargs["task"],
                "provider": "conversation-entry-test",
                "model": "conversation-entry-test",
            },
        )


def _runtime(client: _CountingClient, store=None):
    store = store or InMemoryConversationStore()
    store.create_thread("thread-entry", owner_id="owner-entry")
    return store, ConversationRuntime(store, llm_client=client)


def test_same_run_replays_accepted_entry_without_recalling_provider_or_duplicating_topic():
    client = _CountingClient(_new_topic_output())
    store, runtime = _runtime(client)

    first = runtime.handle_message(
        "thread-entry",
        "分析今天付费金额的变化。",
        run_id="run-entry-replay",
    )
    replay = runtime.handle_message(
        "thread-entry",
        "分析今天付费金额的变化。",
        run_id="run-entry-replay",
    )

    assert len(client.calls) == 1
    assert replay.to_dict() == first.to_dict()
    assert "runtime_budget" not in first.run_request.to_dict()
    assert len(store.topics_for_thread("thread-entry")) == 1
    assert len(store.get_thread("thread-entry").turns) == 1
    assert len(store.context_manifests) == 1
    transition_record = store.conversation_entry_transitions["run-entry-replay"]
    transition = transition_record["transition"]
    assert transition["node_name"] == "conversation_entry"
    assert transition_record["output_payload"]["turn"] == canonical_value(
        first.to_dict()
    )
    assert store.attempt_journal.load_stage_attempt_refs(
        run_attempt_id="run-entry-replay",
        transition_attempt_id=transition["attempt_id"],
        stage_name="conversation_entry",
    ) == (first.entry_command["accepted_attempt_ref"],)


def test_deterministic_topic_selection_has_an_independent_control_call_identity():
    client = _CountingClient(_new_topic_output())
    store, runtime = _runtime(client)
    selected = store.create_topic(
        "thread-entry",
        title="收入变化",
        summary="收入变化分析",
    )

    result = runtime.handle_message(
        "thread-entry",
        "继续收入变化分析。",
        run_id="run-entry-topic-selection",
        topic_selection_binding={
            "schema_version": "persisted-topic-selection.v1",
            "source_run_id": "run-topic-choice-source",
            "intent": "follow_up",
            "confidence": 1.0,
            "business_summary": "用户选择继续收入变化分析。",
            "selected_topic_id": selected.topic_id,
        },
    )

    accepted = store.attempt_journal._attempts[
        result.entry_command["accepted_attempt_ref"]
    ]
    assert accepted.spec.call_kind == "topic_selection"
    assert accepted.attempt_ref.startswith("control-call-attempt:sha256:")
    assert len(client.calls) == 0


def test_replay_rejects_tampered_accepted_terminal_event_without_provider_recall():
    client = _CountingClient(_new_topic_output())
    store, runtime = _runtime(client)
    result = runtime.handle_message(
        "thread-entry",
        "分析今天付费金额的变化。",
        run_id="run-entry-terminal-tamper",
    )
    attempt_ref = result.entry_command["accepted_attempt_ref"]
    terminal = store.attempt_journal._events[attempt_ref][-1]
    store.attempt_journal._events[attempt_ref][-1] = replace(
        terminal,
        output_payload={"tampered": True},
    )

    with pytest.raises(
        EvidenceIntegrityError,
        match="^conversation_entry_acceptance_invalid$",
    ):
        runtime.handle_message(
            "thread-entry",
            "分析今天付费金额的变化。",
            run_id="run-entry-terminal-tamper",
        )

    assert len(client.calls) == 1


class _TurnTamperingStore(InMemoryConversationStore):
    def accept_conversation_entry(self, **kwargs):
        turn = dict(kwargs["turn"])
        turn["run_request"] = {
            **dict(turn["run_request"]),
            "user_message": "tampered after provider acceptance",
        }
        return super().accept_conversation_entry(**{**kwargs, "turn": turn})


def test_store_rejects_route_turn_tamper_against_provider_transition():
    store = _TurnTamperingStore()
    client = _CountingClient(_new_topic_output())
    _, runtime = _runtime(client, store)

    with pytest.raises(
        EvidenceIntegrityError,
        match="^conversation_entry_transition_invalid$",
    ):
        runtime.handle_message(
            "thread-entry",
            "分析今天付费金额的变化。",
            run_id="run-entry-route-tamper",
        )

    assert len(client.calls) == 1
    assert store.get_thread("thread-entry").turns == []
    assert store.context_manifests == {}
    assert store.conversation_entry_transitions == {}


def test_replay_rejects_a_corrupt_run_binding_instead_of_repairing_it():
    client = _CountingClient(_new_topic_output())
    store, runtime = _runtime(client)
    store.upsert_run(
        "run-entry-corrupt-binding",
        thread_id="thread-entry",
        status="running",
    )
    runtime.handle_message(
        "thread-entry",
        "分析今天付费金额的变化。",
        run_id="run-entry-corrupt-binding",
    )
    store.runs["run-entry-corrupt-binding"]["turn_id"] = "turn-corrupt"

    with pytest.raises(
        EvidenceIntegrityError,
        match="^conversation_entry_binding_conflict$",
    ):
        runtime.handle_message(
            "thread-entry",
            "分析今天付费金额的变化。",
            run_id="run-entry-corrupt-binding",
        )

    assert len(client.calls) == 1


def test_same_run_rejects_a_different_exact_command_before_provider_call():
    client = _CountingClient(_new_topic_output())
    _, runtime = _runtime(client)
    runtime.handle_message(
        "thread-entry",
        "分析今天付费金额的变化。",
        run_id="run-entry-conflict",
    )

    with pytest.raises(
        EvidenceIntegrityError,
        match="^conversation_entry_command_conflict$",
    ):
        runtime.handle_message(
            "thread-entry",
            "改为分析昨天付费金额。",
            run_id="run-entry-conflict",
        )

    assert len(client.calls) == 1


def test_real_new_run_creates_a_new_provider_attempt_and_binding():
    client = _CountingClient(_new_topic_output())
    store, runtime = _runtime(client)

    first = runtime.handle_message(
        "thread-entry",
        "分析今天付费金额的变化。",
        run_id="run-entry-first",
    )
    second = runtime.handle_message(
        "thread-entry",
        "分析今天付费金额的变化。",
        run_id="run-entry-second",
    )

    assert len(client.calls) == 2
    assert first.turn_id != second.turn_id
    assert first.topic_id != second.topic_id
    assert len(store.topics_for_thread("thread-entry")) == 2


class _CrashAfterAcceptedCallStore(InMemoryConversationStore):
    def __init__(self) -> None:
        super().__init__()
        self.crash_once = True

    def _conversation_entry_failpoint(self, stage: str) -> None:
        if self.crash_once and stage == "after_turn":
            self.crash_once = False
            raise RuntimeError("injected_conversation_entry_crash")


def test_crash_after_journal_acceptance_replays_without_partial_binding_or_provider_recall():
    store = _CrashAfterAcceptedCallStore()
    client = _CountingClient(_new_topic_output())
    _, runtime = _runtime(client, store)

    with pytest.raises(RuntimeError, match="^injected_conversation_entry_crash$"):
        runtime.handle_message(
            "thread-entry",
            "分析今天付费金额的变化。",
            run_id="run-entry-crash",
        )

    assert len(client.calls) == 1
    assert store.topics_for_thread("thread-entry") == ()
    assert store.get_thread("thread-entry").turns == []
    assert store.context_manifests == {}

    recovered = runtime.handle_message(
        "thread-entry",
        "分析今天付费金额的变化。",
        run_id="run-entry-crash",
    )

    assert len(client.calls) == 1
    assert recovered.topic_id is not None
    assert len(store.topics_for_thread("thread-entry")) == 1
    assert len(store.get_thread("thread-entry").turns) == 1
    assert len(store.context_manifests) == 1


class _CrashBeforeConversationEntryCommitStore(InMemoryConversationStore):
    def __init__(self) -> None:
        super().__init__()
        self.crash_once = True

    def _conversation_entry_failpoint(self, stage: str) -> None:
        if self.crash_once and stage == "before_commit":
            self.crash_once = False
            raise RuntimeError("injected_conversation_entry_commit_failure")


def test_commit_failure_rolls_back_transition_seal_and_replays_accepted_output():
    store = _CrashBeforeConversationEntryCommitStore()
    client = _CountingClient(_new_topic_output())
    _, runtime = _runtime(client, store)

    with pytest.raises(
        RuntimeError,
        match="^injected_conversation_entry_commit_failure$",
    ):
        runtime.handle_message(
            "thread-entry",
            "分析今天付费金额的变化。",
            run_id="run-entry-commit-failure",
        )

    assert store.get_thread("thread-entry").turns == []
    assert store.context_manifests == {}
    assert store.conversation_entry_transitions == {}
    assert len(client.calls) == 1

    recovered = runtime.handle_message(
        "thread-entry",
        "分析今天付费金额的变化。",
        run_id="run-entry-commit-failure",
    )

    assert len(client.calls) == 1
    transition = store.conversation_entry_transitions["run-entry-commit-failure"][
        "transition"
    ]
    assert store.attempt_journal.load_stage_attempt_refs(
        run_attempt_id="run-entry-commit-failure",
        transition_attempt_id=transition["attempt_id"],
        stage_name="conversation_entry",
    ) == (recovered.entry_command["accepted_attempt_ref"],)


class _FreeTextCancelClient:
    supports_model_tier = True
    supports_output_validator = True
    supports_thinking_mode = True
    durable_max_attempts = 1

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def invoke_json(self, **kwargs):
        self.calls.append(dict(kwargs))
        output = {
            "binding_kind": "cancel",
            "slot_id": "",
            "value_ref": "",
            "target_refs": [],
            "affected_binding_fields": [],
            "replacement_user_text": "",
            "status_message": "已取消当前分析。",
        }
        kwargs["output_validator"](output)
        return SimpleNamespace(
            output=output,
            audit={
                "task": kwargs["task"],
                "provider": "free-text-test",
                "model": "free-text-test",
                "attempt_count": 1,
            },
        )


class _FreeTextCommitCrashStore:
    def __init__(self) -> None:
        self.attempt_journal = InMemoryDurableCallJournal()
        self.save_calls = 0
        self.saved_submission = None

    def load_accepted_free_text_submission(self, **_kwargs):
        return self.saved_submission

    def load_decision_ledger(self, _intent_revision_id):
        return DecisionLedger()

    def load_decision_options(self, _intent_revision_id):
        return ()

    def latest_accepted_transition_id(self, _run_attempt_id):
        return None

    def save_interaction_directive_transition(
        self,
        *,
        directive,
        transition,
        input_payload,
        output_payload,
        accepted_attempt_refs,
        material_revision_continuation=None,
    ):
        assert material_revision_continuation is None
        self.save_calls += 1
        if self.save_calls == 1:
            raise RuntimeError("injected_free_text_commit_failure")
        self.attempt_journal.bind_stage(
            run_attempt_id=transition.run_attempt_id,
            transition_attempt_id=transition.attempt_id,
            stage_name="bind_free_text_submission",
            attempt_refs=accepted_attempt_refs,
        )
        self.saved_submission = {
            "transition": transition,
            "input_payload": input_payload,
            "output_payload": output_payload,
        }
        return {
            "directive": directive.to_dict(),
            "durable_checkpoint": transition.to_dict(),
            "replayed": False,
        }


def test_free_text_provider_success_replays_after_transition_commit_failure():
    store = _FreeTextCommitCrashStore()
    client = _FreeTextCancelClient()
    active_revision = SimpleNamespace(
        intent_revision_id="intent-free-text-commit-crash",
        ambiguity_slots=(),
        to_dict=lambda: {
            "intent_revision_id": "intent-free-text-commit-crash",
        },
    )
    invocation = {
        "store": store,
        "llm_client": client,
        "thread_id": "thread-free-text-commit-crash",
        "run_id": "run-free-text-commit-crash",
        "active_revision": active_revision,
        "user_message": "取消这次分析",
    }

    with pytest.raises(
        RuntimeError,
        match="^injected_free_text_commit_failure$",
    ):
        _bind_single_authority_free_text(**invocation)

    assert len(client.calls) == 1
    recovered, raw_binding, audit = _bind_single_authority_free_text(**invocation)
    replayed, replayed_binding, replayed_audit = _bind_single_authority_free_text(
        **invocation
    )

    assert len(client.calls) == 1
    assert client.calls[0]["model_tier"] == "critical"
    assert client.calls[0]["thinking"] == "enabled"
    assert recovered["status"] == "run_cancelled"
    assert recovered["replayed"] is False
    assert raw_binding["binding_kind"] == "cancel"
    assert audit["provider"] == "free-text-test"
    assert replayed["status"] == "run_cancelled"
    assert replayed["replayed"] is True
    assert replayed_binding == raw_binding
    assert replayed_audit is None
    assert store.attempt_journal.load_stage_attempt_refs(
        run_attempt_id="run-free-text-commit-crash",
        transition_attempt_id=recovered["durable_checkpoint"]["attempt_id"],
        stage_name="bind_free_text_submission",
    )


def test_concurrent_same_run_serializes_provider_and_accepts_one_binding():
    entered = Event()
    release = Event()
    client = _CountingClient(
        _new_topic_output(),
        entered=entered,
        release=release,
    )
    store, runtime = _runtime(client)

    def invoke():
        return runtime.handle_message(
            "thread-entry",
            "分析今天付费金额的变化。",
            run_id="run-entry-concurrent",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(invoke)
        assert entered.wait(timeout=5)
        second = executor.submit(invoke)
        assert not second.done()
        release.set()
        first_result = first.result(timeout=5)
        second_result = second.result(timeout=5)

    assert len(client.calls) == 1
    assert first_result.to_dict() == second_result.to_dict()
    assert len(store.topics_for_thread("thread-entry")) == 1
    assert len(store.get_thread("thread-entry").turns) == 1


def test_agent_core_passes_other_running_run_status_to_queue_new_topic():
    store = InMemoryConversationStore()
    store.create_thread("thread-active-run", owner_id="owner-active")
    store.upsert_run(
        "run-already-active",
        thread_id="thread-active-run",
        status="running",
    )
    client = _CountingClient(_new_topic_output(relation="queued_new_topic"))

    result = ConversationAgentCore(
        store,
        conversation_llm_client=client,
        workflow_runner=lambda request: WorkflowRunResult(
            status="failed",
            run_id=request["run_id"],
            failure_reason="synthetic_failure",
        ),
    ).run_message(
        thread_id="thread-active-run",
        run_id="run-new-command",
        user_message="再分析今天付费金额的变化。",
        stop_after_phase="phase03",
    )

    assert result["status"] == "failed"
    assert result["topic_relation"] == "queued_new_topic"
    prompt_text = "\n".join(
        message["content"] for message in client.calls[0]["messages"]
    )
    assert '"active_run_status": "running"' in prompt_text


class _ProviderFailureClient:
    def invoke_json(self, **_kwargs):
        raise RuntimeError("provider_failed")


class _TypedProviderFailureClient:
    def invoke_json(self, **_kwargs):
        raise LLMProviderError(
            kind="provider_unavailable",
            retryability="retryable",
        )


class _ProgrammerErrorClient:
    def invoke_json(self, **_kwargs):
        raise AssertionError("conversation_client_contract_bug")


class _FailingFailureJournal(InMemoryDurableCallJournal):
    def fail(self, attempt, *, failure_code, failure_payload=None):
        del attempt, failure_code, failure_payload
        raise DurableCallJournalError("journal_write_failed")


def test_failure_journal_write_error_is_exposed_as_the_explicit_root_cause():
    store = InMemoryConversationStore()
    store.create_thread("thread-entry", owner_id="owner-entry")
    store.attempt_journal = _FailingFailureJournal()
    runtime = ConversationRuntime(store, llm_client=_ProviderFailureClient())

    with pytest.raises(
        RuntimeError,
        match="^conversation_orchestrator_failure_journal_failed$",
    ) as raised:
        runtime.handle_message(
            "thread-entry",
            "分析今天付费金额的变化。",
            run_id="run-entry-journal-failure",
        )

    assert isinstance(raised.value.__cause__, DurableCallJournalError)
    assert str(raised.value.__cause__) == "journal_write_failed"


def test_typed_provider_failure_maps_to_conversation_provider_failure():
    store = InMemoryConversationStore()
    store.create_thread("thread-entry", owner_id="owner-entry")
    runtime = ConversationRuntime(store, llm_client=_TypedProviderFailureClient())

    with pytest.raises(
        RuntimeError,
        match="^conversation_orchestrator_provider_failed$",
    ) as raised:
        runtime.handle_message(
            "thread-entry",
            "分析今天付费金额的变化。",
            run_id="run-entry-provider-failure",
        )

    assert isinstance(raised.value.__cause__, LLMProviderError)


def test_programmer_error_is_not_misreported_as_provider_failure():
    store = InMemoryConversationStore()
    store.create_thread("thread-entry", owner_id="owner-entry")
    runtime = ConversationRuntime(store, llm_client=_ProgrammerErrorClient())

    with pytest.raises(
        AssertionError,
        match="^conversation_client_contract_bug$",
    ):
        runtime.handle_message(
            "thread-entry",
            "分析今天付费金额的变化。",
            run_id="run-entry-programmer-error",
        )


class _AdvisoryCursor:
    def __init__(self, row=None) -> None:
        self.row = row

    def fetchone(self):
        return self.row


class _AdvisoryConnection:
    def __init__(self, *, unlock_result: bool = True) -> None:
        self.unlock_result = unlock_result
        self.calls: list[str] = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, sql, _params=None):
        normalized = " ".join(str(sql).split())
        self.calls.append(normalized)
        if "pg_advisory_unlock" in normalized:
            return _AdvisoryCursor((self.unlock_result,))
        return _AdvisoryCursor()

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_same_postgres_store_serializes_same_run_before_session_lock_reentry():
    connection = _AdvisoryConnection()
    store = PostgresConversationStore(connection)
    first_entered = Event()
    release_first = Event()
    second_entered = Event()

    def first_worker():
        with store.conversation_entry_lock("run-entry-local-pg-lock"):
            first_entered.set()
            assert release_first.wait(timeout=5)

    def second_worker():
        with store.conversation_entry_lock("run-entry-local-pg-lock"):
            second_entered.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(first_worker)
        assert first_entered.wait(timeout=5)
        second = executor.submit(second_worker)
        assert not second_entered.wait(timeout=0.1)
        release_first.set()
        first.result(timeout=5)
        second.result(timeout=5)

    assert second_entered.is_set()
    assert sum("pg_advisory_lock" in call for call in connection.calls) == 2
    assert sum("pg_advisory_unlock" in call for call in connection.calls) == 2


def test_postgres_conversation_entry_requires_confirmed_advisory_unlock():
    connection = _AdvisoryConnection(unlock_result=False)
    store = PostgresConversationStore(connection)

    with pytest.raises(
        RuntimeError,
        match="^conversation_entry_advisory_unlock_failed$",
    ):
        with store.conversation_entry_lock("run-entry-unlock-failure"):
            pass

    assert connection.rollbacks == 1


@pytest.mark.skipif(
    not (os.getenv("WAJE_RUNTIME_DATABASE_URL") or os.getenv("DATABASE_URL")),
    reason="runtime PostgreSQL is not configured",
)
def test_postgres_concurrent_workers_share_one_provider_call_and_atomic_binding():
    first_store = PostgresConversationStore.from_env()
    second_store = PostgresConversationStore.from_env()
    first_store.apply_schema()
    suffix = uuid4().hex
    thread_id = f"thread-entry-pg-{suffix}"
    run_id = f"run-entry-pg-{suffix}"
    first_store.create_thread(thread_id, owner_id="owner-entry-pg")
    first_store.upsert_run(run_id, thread_id=thread_id, status="running")
    entered = Event()
    release = Event()
    client = _CountingClient(
        _new_topic_output(),
        entered=entered,
        release=release,
    )
    first_runtime = ConversationRuntime(first_store, llm_client=client)
    second_runtime = ConversationRuntime(second_store, llm_client=client)

    def invoke(runtime):
        return runtime.handle_message(
            thread_id,
            "分析今天付费金额的变化。",
            run_id=run_id,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(invoke, first_runtime)
            assert entered.wait(timeout=5)
            second = executor.submit(invoke, second_runtime)
            assert not second.done()
            release.set()
            first_result = first.result(timeout=10)
            second_result = second.result(timeout=10)

        assert len(client.calls) == 1
        assert first_result.to_dict() == second_result.to_dict()
        assert len(first_store.topics_for_thread(thread_id)) == 1
        assert len(first_store.list_context_manifests(thread_id)) == 1
    finally:
        first_store.connection.close()
        second_store.connection.close()


@pytest.mark.skipif(
    not (os.getenv("WAJE_RUNTIME_DATABASE_URL") or os.getenv("DATABASE_URL")),
    reason="runtime PostgreSQL is not configured",
)
def test_postgres_persists_topic_selection_as_a_control_call():
    store = PostgresConversationStore.from_env()
    store.apply_schema()
    suffix = uuid4().hex
    thread_id = f"thread-topic-control-pg-{suffix}"
    run_id = f"run-topic-control-pg-{suffix}"
    store.create_thread(thread_id, owner_id="owner-topic-control-pg")
    selected = store.create_topic(
        thread_id,
        title="收入变化",
        summary="收入变化分析",
    )
    store.upsert_run(run_id, thread_id=thread_id, status="running")
    client = _CountingClient(_new_topic_output())

    try:
        result = ConversationRuntime(store, llm_client=client).handle_message(
            thread_id,
            "继续收入变化分析。",
            run_id=run_id,
            topic_selection_binding={
                "schema_version": "persisted-topic-selection.v1",
                "source_run_id": f"run-topic-choice-source-{suffix}",
                "intent": "follow_up",
                "confidence": 1.0,
                "business_summary": "用户选择继续收入变化分析。",
                "selected_topic_id": selected.topic_id,
            },
        )
        row = store.connection.execute(
            """
            SELECT call_kind, attempt_ref
            FROM waje_runtime.durable_call_attempts
            WHERE attempt_ref = %(attempt_ref)s
            """,
            {"attempt_ref": result.entry_command["accepted_attempt_ref"]},
        ).fetchone()

        assert row == (
            "topic_selection",
            result.entry_command["accepted_attempt_ref"],
        )
        assert row[1].startswith("control-call-attempt:sha256:")
        assert client.calls == []
        from psycopg import Error as PostgresError

        with pytest.raises(
            PostgresError,
            match="append_only_authority_record:conversation_turns",
        ):
            store.connection.execute(
                """
                UPDATE waje_runtime.conversation_turns
                SET payload = '{}'::jsonb
                WHERE turn_id = %(turn_id)s
                """,
                {"turn_id": result.turn_id},
            )
        store.connection.rollback()
    finally:
        store.connection.close()


class _PostgresCrashBeforeConversationEntryCommitStore(PostgresConversationStore):
    def __init__(self, connection) -> None:
        super().__init__(connection)
        self.crash_once = True

    def _conversation_entry_failpoint(self, stage: str) -> None:
        if self.crash_once and stage == "before_commit":
            self.crash_once = False
            raise RuntimeError("injected_postgres_entry_commit_failure")


@pytest.mark.skipif(
    not (os.getenv("WAJE_RUNTIME_DATABASE_URL") or os.getenv("DATABASE_URL")),
    reason="runtime PostgreSQL is not configured",
)
def test_postgres_commit_failure_rolls_back_route_transition_and_stage_seal():
    store = _PostgresCrashBeforeConversationEntryCommitStore.from_env()
    store.apply_schema()
    suffix = uuid4().hex
    thread_id = f"thread-entry-commit-pg-{suffix}"
    run_id = f"run-entry-commit-pg-{suffix}"
    store.create_thread(thread_id, owner_id="owner-entry-commit-pg")
    store.upsert_run(run_id, thread_id=thread_id, status="running")
    client = _CountingClient(_new_topic_output())
    runtime = ConversationRuntime(store, llm_client=client)

    try:
        with pytest.raises(
            RuntimeError,
            match="^injected_postgres_entry_commit_failure$",
        ):
            runtime.handle_message(
                thread_id,
                "分析今天付费金额的变化。",
                run_id=run_id,
            )

        row = store.connection.execute(
            """
            SELECT
              (SELECT count(*)
               FROM waje_runtime.workflow_transition_attempts
               WHERE run_attempt_id = %(run_attempt_id)s),
              (SELECT count(*)
               FROM waje_runtime.durable_stage_attempt_seals
               WHERE run_attempt_id = %(run_attempt_id)s),
              (SELECT count(*)
               FROM waje_runtime.conversation_turns
               WHERE thread_id = %(thread_id)s),
              (SELECT count(*)
               FROM waje_runtime.context_manifests
               WHERE run_id = %(run_attempt_id)s)
            """,
            {"run_attempt_id": run_id, "thread_id": thread_id},
        ).fetchone()
        assert tuple(row) == (0, 0, 0, 0)

        recovered = runtime.handle_message(
            thread_id,
            "分析今天付费金额的变化。",
            run_id=run_id,
        )
        assert len(client.calls) == 1
        run = store.get_run_state(run_id)
        transition_attempt_id = run["request"]["conversation_entry"][
            "transition_attempt_id"
        ]
        assert store.attempt_journal.load_stage_attempt_refs(
            run_attempt_id=run_id,
            transition_attempt_id=transition_attempt_id,
            stage_name="conversation_entry",
        ) == (recovered.entry_command["accepted_attempt_ref"],)
    finally:
        store.connection.close()


@pytest.mark.skipif(
    not (os.getenv("WAJE_RUNTIME_DATABASE_URL") or os.getenv("DATABASE_URL")),
    reason="runtime PostgreSQL is not configured",
)
def test_same_postgres_store_concurrent_runtime_calls_share_one_provider_call():
    store = PostgresConversationStore.from_env()
    store.apply_schema()
    suffix = uuid4().hex
    thread_id = f"thread-entry-same-pg-{suffix}"
    run_id = f"run-entry-same-pg-{suffix}"
    store.create_thread(thread_id, owner_id="owner-entry-same-pg")
    store.upsert_run(run_id, thread_id=thread_id, status="running")
    entered = Event()
    release = Event()
    client = _CountingClient(
        _new_topic_output(),
        entered=entered,
        release=release,
    )
    runtime = ConversationRuntime(store, llm_client=client)

    def invoke():
        return runtime.handle_message(
            thread_id,
            "分析今天付费金额的变化。",
            run_id=run_id,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(invoke)
            assert entered.wait(timeout=5)
            second = executor.submit(invoke)
            assert not second.done()
            release.set()
            first_result = first.result(timeout=10)
            second_result = second.result(timeout=10)

        assert len(client.calls) == 1
        assert first_result.to_dict() == second_result.to_dict()
        assert len(store.topics_for_thread(thread_id)) == 1
        assert len(store.list_context_manifests(thread_id)) == 1
    finally:
        store.connection.close()
