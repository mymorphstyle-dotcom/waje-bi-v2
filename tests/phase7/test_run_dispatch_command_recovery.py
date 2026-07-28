from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from bi_agent.conversation.material_revision_continuation import (
    MaterialRevisionContinuation,
)
from bi_agent.runtime.single_authority import InteractionDirective
from tools.runtime.recover_run_dispatches import (
    recover_pending_run_dispatches,
    run_agent_core_dispatch,
)


def _lease(
    suffix: str,
    request_payload: dict,
    *,
    producer_kind: str = "thread_message",
    scope_ref: str = "thread-recovery",
) -> dict:
    return {
        "dispatch_id": f"dispatch-{suffix}",
        "run_id": f"run-{suffix}",
        "thread_id": "thread-recovery",
        "producer_kind": producer_kind,
        "scope_ref": scope_ref,
        "request_identity": f"request-{suffix}",
        "request_payload": request_payload,
        "dispatch_owner_id": f"recovery-owner-{suffix}",
        "lease_epoch": 3,
    }


def _resume_request() -> dict:
    return {
        "schema_version": "single-authority-phase02-waiting.v1",
    }


def test_crash_recovery_replays_each_exact_agent_core_command_envelope() -> None:
    topic_selection = {
        "sourceRunId": "run-topic-choice-source",
        "topicId": "topic-revenue",
    }
    topic_choice_answer = {
        "sourceRunId": "run-topic-choice-source",
        "answer": "继续看近期收入波动那个主题",
    }
    intent_revision_context = {
        "supersedes_intent_revision_id": "intent-source",
        "superseded_plan_fields": ["analysis_axes", "filters"],
        "intent_revision_reason_ref": "typed_material_correction",
        "parent_transition_id": "transition-source",
    }
    clarification = {
        "sourceRunId": "run-clarification",
        "resolutionId": "single-authority:request-clarification",
        "attemptRunId": "run-clarification",
        "answer": "采用上一日作为比较基线",
        "selectedOptionIds": ["comparison_baseline.previous_day"],
        "source": "user",
        "retryAttempt": False,
    }
    leases = (
        _lease(
            "topic-selection",
            {"message": "收入变化", "topicSelection": topic_selection},
        ),
        _lease(
            "topic-choice-answer",
            {
                "message": topic_choice_answer["answer"],
                "topicChoiceAnswer": topic_choice_answer,
            },
        ),
        _lease(
            "intent-revision",
            {
                "message": "改为按自然月比较收入",
                "intentRevisionContext": intent_revision_context,
            },
        ),
        _lease(
            "clarification",
            {
                "message": clarification["answer"],
                "clarification": clarification,
                "resumeRequest": _resume_request(),
            },
            producer_kind="clarification_resolution",
            scope_ref="run-clarification",
        ),
    )
    calls: list[dict] = []

    class Store:
        def sweep_expired_run_dispatches(self, *, limit):
            return ()

        def lease_recoverable_run_dispatches(self, *, limit):
            assert limit == 1
            return leases[:1]

        def fail_owned_run_dispatch(self, **_kwargs):
            raise AssertionError("valid_command_was_terminalized_as_failed")

    class Core:
        store = SimpleNamespace(connection=SimpleNamespace(close=lambda: None))

        def run_message(self, **kwargs):
            calls.append(kwargs)
            return {"status": "planned", "run_id": kwargs["run_id"]}

    with patch(
        "tools.runtime.recover_run_dispatches.ConversationAgentCore.from_environment",
        return_value=Core(),
    ):
        summary = recover_pending_run_dispatches(store=Store(), limit=10)

    assert summary["failed"] == []
    assert summary["dispatched"] == [
        {"run_id": leases[0]["run_id"], "status": "planned"}
    ]
    assert calls == [
        {
            "thread_id": "thread-recovery",
            "run_id": "run-topic-selection",
            "user_message": "收入变化",
            "run_dispatch": {
                "dispatch_id": "dispatch-topic-selection",
                "dispatch_owner_id": "recovery-owner-topic-selection",
                "lease_epoch": 3,
            },
            "topic_selection": {
                "source_run_id": topic_selection["sourceRunId"],
                "topic_id": topic_selection["topicId"],
            },
        },
    ]


def test_crash_recovery_accepts_exact_free_text_clarification_envelope() -> None:
    clarification = {
        "sourceRunId": "run-free-text",
        "resolutionId": "single-authority:request-free-text",
        "attemptRunId": "run-free-text",
        "answer": "改成与最近七个完整自然日均值比较",
        "selectedOptionIds": [],
        "source": "user",
        "retryAttempt": False,
    }
    lease = _lease(
        "free-text",
        {
            "message": clarification["answer"],
            "clarification": clarification,
            "resumeRequest": _resume_request(),
        },
        producer_kind="clarification_resolution",
        scope_ref="run-free-text",
    )
    calls: list[dict] = []

    class Core:
        store = SimpleNamespace(connection=SimpleNamespace(close=lambda: None))

        def run_message(self, **kwargs):
            calls.append(kwargs)
            return {"status": "completed", "run_id": kwargs["run_id"]}

    with patch(
        "tools.runtime.recover_run_dispatches.ConversationAgentCore.from_environment",
        return_value=Core(),
    ):
        result = run_agent_core_dispatch(lease)

    assert result == {"status": "completed", "run_id": "run-free-text"}
    assert calls == [
        {
            "thread_id": "thread-recovery",
            "run_id": "run-free-text",
            "user_message": clarification["answer"],
            "run_dispatch": {
                "dispatch_id": "dispatch-free-text",
                "dispatch_owner_id": "recovery-owner-free-text",
                "lease_epoch": 3,
            },
            "clarification": clarification,
        }
    ]


@pytest.mark.parametrize(
    "payload",
    (
        {},
        {"message": "问题", "localFallback": True},
        {
            "message": "问题",
            "topicSelection": {
                "sourceRunId": "run-source",
                "topicId": "topic-one",
            },
            "topicChoiceAnswer": {
                "sourceRunId": "run-source",
                "answer": "问题",
            },
        },
        {
            "message": "问题",
            "topicSelection": {
                "sourceRunId": "run-source",
                "topicId": "topic-one",
                "label": "forbidden",
            },
        },
        {
            "message": "继续收入主题",
            "topicChoiceAnswer": {
                "sourceRunId": "run-source",
                "answer": "继续留存主题",
            },
        },
        {
            "message": "修改问题",
            "intentRevisionContext": {
                "supersedes_intent_revision_id": "intent-source",
                "superseded_plan_fields": ["unknown_field"],
                "intent_revision_reason_ref": "typed_material_correction",
                "parent_transition_id": "transition-source",
            },
        },
    ),
)
def test_recovery_rejects_non_exact_agent_core_command(payload: dict) -> None:
    with (
        patch(
            "tools.runtime.recover_run_dispatches."
            "ConversationAgentCore.from_environment",
            side_effect=AssertionError("invalid_command_started_agent_core"),
        ),
        pytest.raises(
            ValueError,
            match="^run_dispatch_recovery_payload_invalid$",
        ),
    ):
        run_agent_core_dispatch(_lease("invalid", payload))


def test_material_revision_continuation_is_an_exact_recoverable_command() -> None:
    directive = InteractionDirective.create(
        run_attempt_id="run-material-source",
        intent_revision_id="intent-material-source",
        kind="material_intent_change",
        target_refs=("intent-material-source",),
        original_user_text="改为分析最近七个完整自然日",
    )
    continuation = MaterialRevisionContinuation.create(
        directive=directive,
        thread_id="thread-recovery",
        successor_user_text="分析最近七个完整自然日的付费金额变化",
        superseded_plan_fields=("time_spec", "resolved_window_refs"),
        parent_transition_id="transition-material-revision",
    )
    lease = {
        "dispatch_id": continuation.successor_dispatch_id,
        "run_id": continuation.successor_run_id,
        "thread_id": continuation.thread_id,
        "producer_kind": continuation.producer_kind,
        "scope_ref": continuation.scope_ref,
        "request_identity": continuation.request_identity,
        "request_payload": continuation.request_payload,
        "dispatch_owner_id": "recovery-owner-material",
        "lease_epoch": 2,
    }
    calls: list[dict] = []

    class Core:
        store = SimpleNamespace(connection=SimpleNamespace(close=lambda: None))

        def run_message(self, **kwargs):
            calls.append(kwargs)
            return {"status": "planned", "run_id": kwargs["run_id"]}

    with patch(
        "tools.runtime.recover_run_dispatches.ConversationAgentCore.from_environment",
        return_value=Core(),
    ):
        result = run_agent_core_dispatch(lease)

    assert result == {
        "status": "planned",
        "run_id": continuation.successor_run_id,
    }
    assert calls == [
        {
            "thread_id": continuation.thread_id,
            "run_id": continuation.successor_run_id,
            "user_message": continuation.successor_user_text,
            "run_dispatch": {
                "dispatch_id": continuation.successor_dispatch_id,
                "dispatch_owner_id": "recovery-owner-material",
                "lease_epoch": 2,
            },
            "intent_revision_context": continuation.request_payload[
                "intentRevisionContext"
            ],
        }
    ]
