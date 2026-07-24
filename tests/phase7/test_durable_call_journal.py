from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from threading import Event, Lock

import pytest

from bi_agent.runtime.durable_call_journal import (
    DurableCallJournalError,
    DurableCallSpec,
    DurableProviderClient,
    InMemoryDurableCallJournal,
)
from bi_agent.runtime.evidence_authority import canonical_digest
from bi_agent.runtime.llm_client import LLMOutputError, LLMProviderError

ROOT = Path(__file__).resolve().parents[2]


def _spec(*, input_value: str = "input") -> DurableCallSpec:
    input_payload = {"value": input_value}
    return DurableCallSpec.create(
        run_attempt_id="run-durable-call",
        intent_revision_id="intent-durable-call",
        plan_revision_id="plan-durable-call",
        task_id=None,
        stage_name="settle_claim_authority",
        call_kind="semantic_provider",
        operation_name="candidate_claim_proposal",
        input_ref=f"input:{input_value}",
        input_payload=input_payload,
    )


def test_claim_is_durable_before_call_and_success_replays_without_new_attempt():
    journal = InMemoryDurableCallJournal()
    spec = _spec()

    claim = journal.claim(spec)

    assert claim.replayed is False
    assert claim.attempt.attempt_number == 1
    assert [event.status for event in journal.events_for_attempt(claim.attempt)] == [
        "claimed",
        "started",
    ]

    completed = journal.succeed(claim.attempt, {"output": {"decision": "accept"}})
    replay = journal.claim(spec)

    assert completed.acceptance.accepted_attempt_ref == claim.attempt.attempt_ref
    assert replay.replayed is True
    assert replay.attempt == claim.attempt
    assert replay.output_payload == {"output": {"decision": "accept"}}
    assert len(journal.attempts_for_idempotency(spec.idempotency_key)) == 1


def test_crashed_started_attempt_gets_monotonic_retry_and_only_success_is_accepted():
    journal = InMemoryDurableCallJournal()
    spec = _spec()

    abandoned = journal.claim(spec)
    journal.abandon(abandoned.attempt)
    retry = journal.claim(spec)

    assert abandoned.attempt.attempt_number == 1
    assert retry.attempt.attempt_number == 2
    assert retry.attempt.retry_reason == "previous_attempt_incomplete"

    completed = journal.succeed(retry.attempt, {"output": {"value": 2}})
    replay = journal.claim(spec)

    assert completed.acceptance.accepted_attempt_ref == retry.attempt.attempt_ref
    assert replay.attempt == retry.attempt
    assert replay.output_payload == {"output": {"value": 2}}


def test_failed_attempt_is_terminal_and_next_attempt_records_retry_reason():
    journal = InMemoryDurableCallJournal()
    spec = _spec()
    first = journal.claim(spec)

    failed = journal.fail(first.attempt, failure_code="provider_timeout")
    retry = journal.claim(spec)

    assert failed.status == "failed"
    assert retry.attempt.attempt_number == 2
    assert retry.attempt.retry_reason == "previous_attempt_failed"
    with pytest.raises(DurableCallJournalError, match="attempt_terminal_conflict"):
        journal.succeed(first.attempt, {"output": {"late": True}})


def test_concurrent_logical_calls_wait_for_owner_and_invoke_provider_once():
    journal = InMemoryDurableCallJournal()
    spec = _spec()
    first_entered = Event()
    release_first = Event()
    second_started = Event()
    call_lock = Lock()
    provider_calls = 0

    def invoke(value: int):
        nonlocal provider_calls
        if value == 2:
            second_started.set()
        claim = journal.claim(spec)
        if claim.replayed:
            return claim.output_payload
        with call_lock:
            provider_calls += 1
        first_entered.set()
        assert release_first.wait(2)
        completion = journal.succeed(claim.attempt, {"output": {"value": value}})
        return completion.output_payload

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(invoke, 1)
        assert first_entered.wait(2)
        second = executor.submit(invoke, 2)
        assert second_started.wait(2)
        release_first.set()
        results = (first.result(), second.result())

    assert provider_calls == 1
    assert results == ({"output": {"value": 1}},) * 2
    assert len(journal.attempts_for_idempotency(spec.idempotency_key)) == 1


@pytest.mark.parametrize(
    "provider_output",
    (
        {"output": {"message": "completed normally"}},
        {"output": {"message": "provider reported an error-like sentence"}},
    ),
)
def test_in_flight_success_after_scope_supersession_is_auditable_orphan(
    provider_output,
):
    scope_active = Event()
    scope_active.set()
    provider_started = Event()
    release_provider = Event()
    journal = InMemoryDurableCallJournal(
        active_scope_validator=lambda _spec: scope_active.is_set()
    )
    spec = _spec()

    def invoke():
        claim = journal.claim(spec)
        provider_started.set()
        assert release_provider.wait(2)
        return journal.succeed(claim.attempt, provider_output)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(invoke)
        assert provider_started.wait(2)
        scope_active.clear()
        release_provider.set()
        completion = future.result()

    assert completion.disposition == "orphaned"
    assert completion.acceptance is None
    assert completion.accepted_attempt is None
    assert completion.output_payload == provider_output
    assert completion.terminal_event.status == "succeeded"
    assert completion.terminal_event.success_disposition == "orphaned"
    assert journal.events_for_attempt(completion.attempt)[-1] == (
        completion.terminal_event
    )
    with pytest.raises(DurableCallJournalError, match="call_scope_not_active"):
        journal.claim(spec)
    with pytest.raises(DurableCallJournalError, match="stage_attempt_not_accepted"):
        journal.bind_stage(
            run_attempt_id=spec.run_attempt_id,
            transition_attempt_id="transition-after-supersession",
            stage_name=spec.stage_name,
            attempt_refs=(completion.attempt.attempt_ref,),
        )


class _ProviderResult:
    def __init__(self, output, audit):
        self.output = output
        self.audit = audit


class _Provider:
    def __init__(self) -> None:
        self.calls = 0
        self._lock = Lock()

    def invoke_json(self, **kwargs):
        with self._lock:
            self.calls += 1
            call_number = self.calls
        output = {"decision": f"accepted-{call_number}"}
        return _ProviderResult(
            output,
            {
                "task": kwargs["task"],
                "provider": "provider-test",
                "model": "model-test",
                "attempt_count": 1,
                "structured_output": output,
            },
        )


def _accept_output(_: object) -> None:
    return None


def _invoke_provider(client: DurableProviderClient):
    return client.invoke_json(
        task="single_authority_candidate_claim_proposal",
        prompt_version="test.v1",
        messages=(
            {"role": "system", "content": "system"},
            {"role": "user", "content": "payload"},
        ),
        required_keys=("decision",),
        output_validator=_accept_output,
        model_tier="critical",
    )


def _provider_call_spec() -> DurableCallSpec:
    input_payload = {
        "task": "single_authority_candidate_claim_proposal",
        "prompt_version": "test.v1",
        "messages": (
            {"role": "system", "content": "system"},
            {"role": "user", "content": "payload"},
        ),
        "required_keys": ("decision",),
        "output_validator_ref": f"{__name__}:_accept_output",
        "model_tier": "critical",
        "thinking": None,
    }
    return DurableCallSpec.create(
        run_attempt_id="run-provider",
        intent_revision_id="intent-provider",
        plan_revision_id="plan-provider",
        task_id=None,
        stage_name="settle_claim_authority",
        call_kind="semantic_provider",
        operation_name="single_authority_candidate_claim_proposal",
        input_ref="provider-call-input:sha256:" + canonical_digest(input_payload),
        input_payload=input_payload,
    )


class _RetryingProvider(_Provider):
    durable_max_attempts = 3

    def invoke_json(self, **kwargs):
        with self._lock:
            self.calls += 1
            call_number = self.calls
        if call_number == 1:
            raise LLMProviderError(
                kind="provider_rate_limited",
                retryability="retryable",
                status_code=429,
                error_code="rate_limit_exceeded",
                error_type="rate_limit_error",
                error_param="messages",
                audit={
                    "task": kwargs["task"],
                    "provider": "provider-test",
                    "model": "model-test",
                    "prompt_version": kwargs["prompt_version"],
                    "messages": kwargs["messages"],
                    "raw_response_content": "retry-private-response" * 4096,
                    "started_at": "2026-07-18T00:00:00+00:00",
                    "finished_at": "2026-07-18T00:00:00.250000+00:00",
                    "duration_ms": 250.0,
                    "usage": {"prompt_tokens": 20, "total_tokens": 20},
                    "attempt_failures": (
                        {
                            "attempt": 1,
                            "failure_code": "provider_rate_limited",
                            "response_id": "response-failed-1",
                            "raw_response_content": ("retry-private-response" * 4096),
                            "provider_error": {
                                "status_code": 429,
                                "code": "rate_limit_exceeded",
                                "type": "rate_limit_error",
                                "param": "messages",
                                "message": "private provider diagnostic",
                                "body": {"private": "provider body"},
                            },
                        },
                    ),
                },
            )
        output = {"decision": "accepted-2"}
        return _ProviderResult(
            output,
            {
                "task": kwargs["task"],
                "provider": "provider-test",
                "model": "model-test",
                "attempt_count": 1,
                "structured_output": output,
            },
        )


class _UnknownFailureProvider(_Provider):
    durable_max_attempts = 3

    def invoke_json(self, **_kwargs):
        with self._lock:
            self.calls += 1
        raise AssertionError("provider_programming_error")


class _ProviderWithoutValidatorCapability(_Provider):
    durable_max_attempts = 2

    def __init__(self) -> None:
        super().__init__()
        self.received_validator = False

    def invoke_json(self, **kwargs):
        self.received_validator = self.received_validator or (
            "output_validator" in kwargs
        )
        with self._lock:
            self.calls += 1
            call_number = self.calls
        output = {"decision": "poison" if call_number == 1 else "accepted-2"}
        audit = {
            "task": kwargs["task"],
            "provider": "provider-test",
            "model": "model-test",
            "attempt_count": 1,
            "structured_output": output,
        }
        if call_number == 1:
            audit.update(
                {
                    "prompt_version": kwargs["prompt_version"],
                    "messages": kwargs["messages"],
                    "raw_response_content": "private-provider-response" * 4096,
                    "started_at": "2026-07-18T00:00:00+00:00",
                    "finished_at": "2026-07-18T00:00:01+00:00",
                    "duration_ms": 1000.0,
                    "usage": {
                        "prompt_tokens": 20,
                        "completion_tokens": 5,
                        "total_tokens": 25,
                    },
                }
            )
        return _ProviderResult(
            output,
            audit,
        )


def _reject_poison_output(output) -> None:
    if output.get("decision") == "poison":
        raise LLMOutputError("planner_contract_rejected")


def _reject_poison_output_without_retry(output) -> None:
    if output.get("decision") == "poison":
        raise LLMOutputError(
            "narrative_contract_rejected",
            retryable=False,
        )


def test_provider_client_replays_persisted_result_and_exposes_stage_attempt_refs():
    journal = InMemoryDurableCallJournal()
    provider = _Provider()
    first = DurableProviderClient(
        provider,
        journal=journal,
        run_attempt_id="run-provider",
        intent_revision_id="intent-provider",
        plan_revision_id="plan-provider",
        call_kind="semantic_provider",
        task_id=None,
        stage_name="settle_claim_authority",
    )

    first_result = _invoke_provider(first)
    replay = DurableProviderClient(
        provider,
        journal=journal,
        run_attempt_id="run-provider",
        intent_revision_id="intent-provider",
        plan_revision_id="plan-provider",
        call_kind="semantic_provider",
        task_id=None,
        stage_name="settle_claim_authority",
    )
    replay_result = _invoke_provider(replay)

    assert provider.calls == 1
    assert replay_result.output == first_result.output
    assert replay_result.audit == first_result.audit
    assert replay_result.audit["structured_output"] == first_result.output
    assert len(first.accepted_attempt_refs) == 1
    assert replay.accepted_attempt_refs == first.accepted_attempt_refs


def test_provider_retry_records_failed_attempt_then_accepts_second_attempt():
    journal = InMemoryDurableCallJournal()
    provider = _RetryingProvider()
    client = DurableProviderClient(
        provider,
        journal=journal,
        run_attempt_id="run-provider",
        intent_revision_id="intent-provider",
        plan_revision_id="plan-provider",
        call_kind="semantic_provider",
        task_id=None,
        stage_name="settle_claim_authority",
    )

    result = _invoke_provider(client)
    attempts = journal.attempts_for_idempotency(_provider_call_spec().idempotency_key)

    assert provider.calls == 2
    assert [item.attempt_number for item in attempts] == [1, 2]
    assert [journal.events_for_attempt(item)[-1].status for item in attempts] == [
        "failed",
        "succeeded",
    ]
    assert result.audit["attempt_count"] == 2
    prior_failure = result.audit["attempt_failures"][0]
    assert prior_failure["attempt"] == 1
    assert prior_failure["task"] == "single_authority_candidate_claim_proposal"
    assert prior_failure["provider"] == "provider-test"
    assert prior_failure["model"] == "model-test"
    assert prior_failure["prompt_version"] == "test.v1"
    assert prior_failure["duration_ms"] == 250.0
    assert prior_failure["usage"] == {
        "prompt_tokens": 20,
        "total_tokens": 20,
    }
    assert prior_failure["provider_error"] == {
        "status_code": 429,
        "code": "rate_limit_exceeded",
        "type": "rate_limit_error",
        "param": "messages",
    }
    failed_audit = journal.events_for_attempt(attempts[0])[-1].failure_payload["audit"]
    assert failed_audit["provider_error"] == prior_failure["provider_error"]
    assert failed_audit["call_input_ref"] == attempts[0].spec.input_ref
    assert failed_audit["call_input_digest"] == attempts[0].spec.input_digest
    serialized_failure = json.dumps(failed_audit)
    serialized_prior_failure = json.dumps(prior_failure)
    for forbidden in (
        "retry-private-response",
        "private provider diagnostic",
        "provider body",
    ):
        assert forbidden not in serialized_failure
        assert forbidden not in serialized_prior_failure
    assert "messages" not in failed_audit
    assert "raw_response_content" not in failed_audit
    assert client.accepted_attempt_refs == (attempts[1].attempt_ref,)


def test_wrapper_rejects_poison_before_acceptance_without_provider_validator_support():
    journal = InMemoryDurableCallJournal()
    provider = _ProviderWithoutValidatorCapability()
    client = DurableProviderClient(
        provider,
        journal=journal,
        run_attempt_id="run-provider",
        intent_revision_id="intent-provider",
        plan_revision_id="plan-provider",
        call_kind="semantic_provider",
        task_id=None,
        stage_name="settle_claim_authority",
    )

    result = client.invoke_json(
        task="single_authority_candidate_claim_proposal",
        prompt_version="test.v1",
        messages=(
            {"role": "system", "content": "system"},
            {"role": "user", "content": "payload"},
        ),
        required_keys=("decision",),
        output_validator=_reject_poison_output,
        model_tier="critical",
    )
    attempts = journal.attempts_for_idempotency(
        client.accepted_call_specs[0].idempotency_key
    )

    assert provider.calls == 2
    assert provider.received_validator is False
    assert result.output == {"decision": "accepted-2"}
    assert [item.attempt_number for item in attempts] == [1, 2]
    assert [journal.events_for_attempt(item)[-1].status for item in attempts] == [
        "failed",
        "succeeded",
    ]
    failed_audit = journal.events_for_attempt(attempts[0])[-1].failure_payload["audit"]
    serialized_failure = json.dumps(failed_audit)
    assert failed_audit["task"] == ("single_authority_candidate_claim_proposal")
    assert failed_audit["provider"] == "provider-test"
    assert failed_audit["model"] == "model-test"
    assert failed_audit["prompt_version"] == "test.v1"
    assert failed_audit["status"] == "failed"
    assert failed_audit["failure_code"] == "planner_contract_rejected"
    assert failed_audit["attempt_number"] == 1
    assert failed_audit["duration_ms"] == 1000.0
    assert failed_audit["usage"] == {
        "prompt_tokens": 20,
        "completion_tokens": 5,
        "total_tokens": 25,
    }
    assert failed_audit["call_input_ref"] == attempts[0].spec.input_ref
    assert failed_audit["call_input_digest"] == attempts[0].spec.input_digest
    assert failed_audit["call_input_bytes"] > 0
    assert failed_audit["raw_response_bytes"] > 64_000
    assert len(failed_audit["raw_response_digest"]) == 64
    assert failed_audit["structured_output_bytes"] > 0
    assert len(failed_audit["structured_output_digest"]) == 64
    assert "messages" not in failed_audit
    assert "raw_response_content" not in failed_audit
    assert "structured_output" not in failed_audit
    assert "private-provider-response" not in serialized_failure
    assert "poison" not in serialized_failure
    assert len(serialized_failure) < 4096
    assert client.accepted_attempt_refs == (attempts[1].attempt_ref,)


def test_wrapper_does_not_retry_non_retryable_output_contract_failure() -> None:
    journal = InMemoryDurableCallJournal()
    provider = _ProviderWithoutValidatorCapability()
    messages = (
        {"role": "system", "content": "system"},
        {"role": "user", "content": "payload"},
    )
    input_payload = {
        "task": "single_authority_narrative_writer",
        "prompt_version": "test.v1",
        "messages": messages,
        "required_keys": ("decision",),
        "output_validator_ref": (
            f"{__name__}:_reject_poison_output_without_retry"
        ),
        "model_tier": "critical",
        "thinking": None,
    }
    expected_spec = DurableCallSpec.create(
        run_attempt_id="run-provider",
        intent_revision_id="intent-provider",
        plan_revision_id="plan-provider",
        task_id=None,
        stage_name="compose_claim_aware_narrative",
        call_kind="narrative_provider",
        operation_name="single_authority_narrative_writer",
        input_ref="provider-call-input:sha256:" + canonical_digest(input_payload),
        input_payload=input_payload,
    )
    client = DurableProviderClient(
        provider,
        journal=journal,
        run_attempt_id="run-provider",
        intent_revision_id="intent-provider",
        plan_revision_id="plan-provider",
        call_kind="narrative_provider",
        task_id=None,
        stage_name="compose_claim_aware_narrative",
    )

    with pytest.raises(
        LLMOutputError,
        match="^narrative_contract_rejected$",
    ) as captured:
        client.invoke_json(
            task="single_authority_narrative_writer",
            prompt_version="test.v1",
            messages=messages,
            required_keys=("decision",),
            output_validator=_reject_poison_output_without_retry,
            model_tier="critical",
        )

    attempts = journal.attempts_for_idempotency(
        expected_spec.idempotency_key
    )
    assert captured.value.retryable is False
    assert provider.calls == 1
    assert len(attempts) == 1
    assert journal.events_for_attempt(attempts[0])[-1].status == "failed"


def test_unknown_provider_failure_terminalizes_once_without_retry():
    journal = InMemoryDurableCallJournal()
    provider = _UnknownFailureProvider()
    client = DurableProviderClient(
        provider,
        journal=journal,
        run_attempt_id="run-provider",
        intent_revision_id="intent-provider",
        plan_revision_id="plan-provider",
        call_kind="semantic_provider",
        task_id=None,
        stage_name="settle_claim_authority",
    )

    with pytest.raises(AssertionError, match="provider_programming_error"):
        _invoke_provider(client)

    attempts = journal.attempts_for_idempotency(_provider_call_spec().idempotency_key)
    assert provider.calls == 1
    assert len(attempts) == 1
    assert journal.events_for_attempt(attempts[0])[-1].status == "failed"


def test_stage_binding_accepts_only_persisted_cas_winners_and_is_immutable():
    journal = InMemoryDurableCallJournal()
    pending = journal.claim(_spec(input_value="pending"))
    accepted_claim = journal.claim(_spec(input_value="accepted"))
    accepted = journal.succeed(accepted_claim.attempt, {"output": {"ok": True}})

    with pytest.raises(DurableCallJournalError, match="stage_attempt_not_accepted"):
        journal.bind_stage(
            run_attempt_id="run-durable-call",
            transition_attempt_id="transition-attempt-1",
            stage_name="settle_claim_authority",
            attempt_refs=(pending.attempt.attempt_ref,),
        )

    bound = journal.bind_stage(
        run_attempt_id="run-durable-call",
        transition_attempt_id="transition-attempt-1",
        stage_name="settle_claim_authority",
        attempt_refs=(accepted.acceptance.accepted_attempt_ref,),
    )
    replayed = journal.bind_stage(
        run_attempt_id="run-durable-call",
        transition_attempt_id="transition-attempt-1",
        stage_name="settle_claim_authority",
        attempt_refs=(accepted.acceptance.accepted_attempt_ref,),
    )

    assert bound == replayed == (accepted.acceptance.accepted_attempt_ref,)
    with pytest.raises(DurableCallJournalError, match="stage_binding_conflict"):
        journal.bind_stage(
            run_attempt_id="run-durable-call",
            transition_attempt_id="transition-attempt-1",
            stage_name="settle_claim_authority",
            attempt_refs=(),
        )


def test_historical_acceptance_cannot_bind_after_scope_becomes_inactive():
    scope_active = Event()
    scope_active.set()
    journal = InMemoryDurableCallJournal(
        active_scope_validator=lambda _spec: scope_active.is_set()
    )
    claim = journal.claim(_spec())
    accepted = journal.succeed(claim.attempt, {"output": {"ok": True}})

    scope_active.clear()

    with pytest.raises(DurableCallJournalError, match="call_scope_not_active"):
        journal.bind_stage(
            run_attempt_id="run-durable-call",
            transition_attempt_id="transition-after-cancellation",
            stage_name="settle_claim_authority",
            attempt_refs=(accepted.acceptance.accepted_attempt_ref,),
        )
    with pytest.raises(DurableCallJournalError, match="stage_seal_missing"):
        journal.load_stage_attempt_refs(
            run_attempt_id="run-durable-call",
            transition_attempt_id="transition-after-cancellation",
            stage_name="settle_claim_authority",
        )


def test_exact_stage_binding_replay_survives_later_scope_supersession():
    scope_active = Event()
    scope_active.set()
    journal = InMemoryDurableCallJournal(
        active_scope_validator=lambda _spec: scope_active.is_set()
    )
    claim = journal.claim(_spec())
    accepted = journal.succeed(claim.attempt, {"output": {"ok": True}})
    attempt_ref = accepted.acceptance.accepted_attempt_ref

    bound = journal.bind_stage(
        run_attempt_id="run-durable-call",
        transition_attempt_id="transition-before-supersession",
        stage_name="settle_claim_authority",
        attempt_refs=(attempt_ref,),
    )
    scope_active.clear()

    replayed = journal.bind_stage(
        run_attempt_id="run-durable-call",
        transition_attempt_id="transition-before-supersession",
        stage_name="settle_claim_authority",
        attempt_refs=(attempt_ref,),
    )
    assert replayed == bound == (attempt_ref,)

    with pytest.raises(DurableCallJournalError, match="call_scope_not_active"):
        journal.bind_stage(
            run_attempt_id="run-durable-call",
            transition_attempt_id="new-transition-after-supersession",
            stage_name="settle_claim_authority",
            attempt_refs=(attempt_ref,),
        )


def test_runtime_schema_declares_append_only_attempt_events_acceptance_and_stage_refs():
    schema = (ROOT / "tools/runtime/conversation-runtime.sql").read_text()

    assert "CREATE TABLE IF NOT EXISTS waje_runtime.durable_call_attempts" in schema
    assert (
        "CREATE TABLE IF NOT EXISTS waje_runtime.durable_call_attempt_events" in schema
    )
    assert "status text NOT NULL CHECK (status IN (" in schema
    for status in ("'claimed'", "'started'", "'succeeded'", "'failed'"):
        assert status in schema
    assert "CREATE TABLE IF NOT EXISTS waje_runtime.durable_call_acceptances" in schema
    assert (
        "CREATE TABLE IF NOT EXISTS waje_runtime.durable_stage_attempt_seals" in schema
    )
    assert (
        "CREATE TABLE IF NOT EXISTS waje_runtime.durable_stage_attempt_bindings"
        in schema
    )
    assert "accepted_attempt_ref text NOT NULL" in schema
    assert "success_disposition text CHECK" in schema
    assert "success_disposition IN ('accepted', 'orphaned')" in schema
    assert "transition_attempt_id text NOT NULL" in schema
    assert "REFERENCES waje_runtime.durable_call_attempts" in schema
    assert "task_id text" in schema
    for call_kind in (
        "conversation_provider",
        "topic_selection",
        "intent_provider",
        "clarification_provider",
        "planner_provider",
        "plan_patch_provider",
        "query",
        "capability",
        "semantic_provider",
        "narrative_provider",
    ):
        assert f"'{call_kind}'" in schema


def test_deterministic_topic_selection_cannot_be_invoked_as_a_provider_call():
    with pytest.raises(DurableCallJournalError, match="provider_call_kind_invalid"):
        DurableProviderClient(
            _Provider(),
            journal=InMemoryDurableCallJournal(),
            run_attempt_id="run-topic-selection-control",
            intent_revision_id=None,
            plan_revision_id=None,
            call_kind="topic_selection",
            task_id=None,
            stage_name="conversation_entry",
        )


@pytest.mark.parametrize(
    ("call_kind", "intent_revision_id", "plan_revision_id", "task_id"),
    (
        ("intent_provider", "intent-unexpected", None, None),
        ("planner_provider", "intent-required", "plan-unexpected", None),
        ("plan_patch_provider", "intent-required", None, None),
        ("semantic_provider", "intent-required", None, None),
        ("capability", "intent-required", "plan-required", None),
    ),
)
def test_call_spec_rejects_scope_fields_outside_exact_call_kind_contract(
    call_kind,
    intent_revision_id,
    plan_revision_id,
    task_id,
):
    with pytest.raises(DurableCallJournalError, match="call_spec_.*_invalid"):
        DurableCallSpec.create(
            run_attempt_id="run-scope",
            intent_revision_id=intent_revision_id,
            plan_revision_id=plan_revision_id,
            task_id=task_id,
            stage_name="stage-scope",
            call_kind=call_kind,
            operation_name="operation-scope",
            input_ref="input-scope",
            input_payload={"value": "scope"},
        )
