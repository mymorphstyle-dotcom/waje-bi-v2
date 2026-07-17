from __future__ import annotations

from types import SimpleNamespace

import pytest

from tools.runtime import recover_run_dispatches as recovery


@pytest.mark.parametrize(
    ("producer_kind", "attempt_run_id", "retry_attempt", "previous"),
    (
        ("clarification_resume", "run-attempt-1", False, None),
        ("clarification_retry", "run-attempt-2", True, "run-attempt-1"),
    ),
)
def test_recovery_dispatches_canonical_clarification_attempt(
    monkeypatch,
    producer_kind,
    attempt_run_id,
    retry_attempt,
    previous,
):
    calls = []

    class FakeCore:
        store = SimpleNamespace(connection=SimpleNamespace(close=lambda: None))

        def run_message(self, **kwargs):
            calls.append(kwargs)
            return {"run_id": attempt_run_id, "status": "completed"}

    monkeypatch.setattr(
        recovery.ConversationAgentCore,
        "from_environment",
        lambda: FakeCore(),
    )
    payload = {
        "sourceRunId": "run-source",
        "resolutionId": "resolution-1",
        "attemptRunId": attempt_run_id,
        "answer": "跟前一天比较",
        "selectedOptionId": "material-baseline-previous-day",
        "source": "user",
        "retryAttempt": retry_attempt,
    }
    if previous:
        payload["previousAttemptRunId"] = previous

    result = recovery.run_agent_core_dispatch(
        {
            "run_id": attempt_run_id,
            "thread_id": "thread-source",
            "producer_kind": producer_kind,
            "scope_ref": "resolution-1",
            "dispatch_owner_id": "recovery-worker",
            "lease_epoch": 2,
            "request_payload": payload,
        }
    )

    assert result["status"] == "completed"
    assert calls == [
        {
            "thread_id": "thread-source",
            "run_id": attempt_run_id,
            "user_message": "跟前一天比较",
            "clarification": {
                "sourceRunId": "run-source",
                "resolutionId": "resolution-1",
                "attemptRunId": attempt_run_id,
                "answer": "跟前一天比较",
                "selectedOptionId": "material-baseline-previous-day",
                "source": "user",
                "retryAttempt": retry_attempt,
            },
            "run_dispatch": {
                "dispatch_owner_id": "recovery-worker",
                "lease_epoch": 2,
            },
        }
    ]


def test_recovery_rejects_legacy_clarification_resume_payload():
    with pytest.raises(ValueError, match="recovery_payload_invalid"):
        recovery.run_agent_core_dispatch(
            {
                "run_id": "run-attempt-1",
                "thread_id": "thread-source",
                "producer_kind": "clarification_resume",
                "scope_ref": "run-source",
                "dispatch_owner_id": "recovery-worker",
                "lease_epoch": 1,
                "request_payload": {
                    "runId": "run-source",
                    "answer": "跟前一天比较",
                    "selectedOptionId": "material-baseline-previous-day",
                    "source": "user",
                },
            }
        )


def test_recovery_rejects_retry_with_wrong_resolution_scope():
    with pytest.raises(ValueError, match="recovery_scope_mismatch"):
        recovery.run_agent_core_dispatch(
            {
                "run_id": "run-attempt-2",
                "thread_id": "thread-source",
                "producer_kind": "clarification_retry",
                "scope_ref": "resolution-other",
                "dispatch_owner_id": "recovery-worker",
                "lease_epoch": 1,
                "request_payload": {
                    "sourceRunId": "run-source",
                    "resolutionId": "resolution-1",
                    "attemptRunId": "run-attempt-2",
                    "previousAttemptRunId": "run-attempt-1",
                    "answer": "跟前一天比较",
                    "selectedOptionId": "material-baseline-previous-day",
                    "source": "user",
                    "retryAttempt": True,
                },
            }
        )
