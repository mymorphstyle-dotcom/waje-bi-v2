from __future__ import annotations

from copy import deepcopy

import pytest

from bi_agent.conversation.agent_core import (
    _build_clarification_source_envelope,
)
from bi_agent.conversation.clarification_authority import (
    clarification_attempt_request_digest,
    clarification_resolution_digest,
    clarification_resolution_source_request_digest,
    validate_clarification_resolution_attempt,
)
from bi_agent.runtime.evidence_authority import EvidenceIntegrityError


def _authority_material(*, freeform: bool = False) -> dict:
    selected = {
        "choice_id": "material-baseline-previous-day",
        "action_kind": "bind_material_choice",
        "business_label": "跟前一天比较（推荐）",
        "material_patch": {"baseline_candidates": ["previous_day"]},
        "affected_material_slots": ["baseline"],
    }
    redirect = {
        "choice_id": "material-user-redirect",
        "action_kind": "user_redirect",
        "business_label": "tell the agent to do differently",
    }
    accepted_choice = redirect if freeform else selected
    answer = "改为跟月初比较" if freeform else selected["business_label"]
    selected_option_id = None if freeform else selected["choice_id"]
    clarification = {
        "questions": [
            {
                "question": "希望使用哪个比较基线？",
                "options": [
                    selected["business_label"],
                    redirect["business_label"],
                ],
            }
        ],
        "choice_actions": [selected, redirect],
    }
    source_request = {
        "clarification_source_envelope": _build_clarification_source_envelope(
            source_run_id="run-source",
            source_thread_id="thread-source",
            source_topic_id="topic-source",
            source_owner_id="user-1",
            question="2026年6月1日付费金额为什么上涨？",
            analysis_context={"as_of": "2026-07-17T09:00:00+08:00"},
            original_intent={
                "question": "2026年6月1日付费金额为什么上涨？",
                "target_metric": "paid_amount",
            },
            material_slots={"target_metrics": ["paid_amount"]},
            clarification=clarification,
        )
    }
    submission = {
        "sourceRunId": "run-source",
        "answer": answer,
        "selectedOptionId": selected_option_id,
        "source": "user",
    }
    source_request_digest = clarification_resolution_source_request_digest(
        source_request
    )
    resolution = {
        "resolution_id": "resolution-1",
        "source_run_id": "run-source",
        "thread_id": "thread-source",
        "topic_id": "topic-source",
        "owner_id": "user-1",
        "submission": submission,
        "accepted_choice": accepted_choice,
        "message_id": "message-1",
        "message_thread_id": "thread-source",
        "message_role": "user",
        "message_text": answer,
        "source_request_digest": source_request_digest,
        "status": "accepted",
        "accepted_at": "2026-07-17T09:01:00+08:00",
    }
    resolution["resolution_digest"] = clarification_resolution_digest(
        resolution_id=resolution["resolution_id"],
        source_run_id=resolution["source_run_id"],
        thread_id=resolution["thread_id"],
        topic_id=resolution["topic_id"],
        owner_id=resolution["owner_id"],
        submission=submission,
        accepted_choice=accepted_choice,
        message_id=resolution["message_id"],
        source_request_digest=source_request_digest,
    )
    return {
        "resolution": resolution,
        "source_run": {
            "run_id": "run-source",
            "thread_id": "thread-source",
            "topic_id": "topic-source",
            "owner_id": "user-1",
            "status": "waiting_for_clarification",
            "request": source_request,
        },
        "answer": answer,
        "selected_option_id": selected_option_id,
        "accepted_choice": accepted_choice,
    }


def _attempt_validation(
    material: dict,
    *,
    attempt_number: int,
) -> dict:
    retry = attempt_number > 1
    attempt_run_id = f"run-attempt-{attempt_number}"
    previous_attempt_run_id = "run-attempt-1" if retry else None
    request_payload = {
        "sourceRunId": "run-source",
        "resolutionId": "resolution-1",
        "attemptRunId": attempt_run_id,
        "answer": material["answer"],
        "selectedOptionId": material["selected_option_id"],
        "source": "user",
        "retryAttempt": retry,
    }
    if retry:
        request_payload["previousAttemptRunId"] = previous_attempt_run_id
    producer_kind = "clarification_retry" if retry else "clarification_resume"
    request_digest = clarification_attempt_request_digest(
        producer_kind=producer_kind,
        scope_ref="resolution-1",
        thread_id="thread-source",
        text=material["answer"],
        request_payload=request_payload,
    )
    return {
        "resolution": deepcopy(material["resolution"]),
        "attempt": {
            "resolution_id": "resolution-1",
            "attempt_run_id": attempt_run_id,
            "previous_attempt_run_id": previous_attempt_run_id,
            "attempt_number": attempt_number,
            "request_identity": f"attempt-identity-{attempt_number}",
            "request_digest": request_digest,
            "previous_resolution_id": "resolution-1" if retry else None,
            "previous_attempt_number": 1 if retry else None,
            "previous_attempt_status": "failed" if retry else None,
        },
        "source_run": deepcopy(material["source_run"]),
        "attempt_run": {
            "run_id": attempt_run_id,
            "thread_id": "thread-source",
            "status": "running",
        },
        "dispatch": {
            "producer_kind": producer_kind,
            "scope_ref": "resolution-1",
            "thread_id": "thread-source",
            "run_id": attempt_run_id,
            "request_identity": f"attempt-identity-{attempt_number}",
            "request_digest": request_digest,
            "request_payload": request_payload,
            "dispatch_state": "running",
            "text": material["answer"],
        },
        "source_run_id": "run-source",
        "attempt_run_id": attempt_run_id,
        "thread_id": "thread-source",
        "owner_id": "user-1",
        "answer": material["answer"],
        "selected_option_id": material["selected_option_id"],
        "source": "user",
    }


def test_initial_and_retry_attempts_share_one_accepted_resolution():
    material = _authority_material()

    initial = validate_clarification_resolution_attempt(
        **_attempt_validation(material, attempt_number=1)
    )
    retry = validate_clarification_resolution_attempt(
        **_attempt_validation(material, attempt_number=2)
    )

    assert initial["retry_attempt"] is False
    assert retry["retry_attempt"] is True
    assert retry["resolution_id"] == initial["resolution_id"]
    assert retry["accepted_choice"] == material["accepted_choice"]
    assert retry["material_patch"] == {
        "baseline_candidates": ["previous_day"]
    }


@pytest.mark.parametrize(
    ("path", "value", "reason"),
    (
        (("resolution", "owner_id"), "user-other", "owner_mismatch"),
        (("source_run", "topic_id"), "topic-other", "owner_mismatch"),
        (
            ("attempt", "previous_attempt_status"),
            "completed",
            "retry_attempt_invalid",
        ),
        (
            ("dispatch", "request_digest"),
            "0" * 64,
            "dispatch_mismatch",
        ),
    ),
)
def test_retry_attempt_rejects_authority_drift(path, value, reason):
    values = _attempt_validation(_authority_material(), attempt_number=2)
    values[path[0]][path[1]] = value

    with pytest.raises(EvidenceIntegrityError, match=reason):
        validate_clarification_resolution_attempt(**values)


def test_resolution_rejects_source_request_or_accepted_choice_tampering():
    material = _authority_material()
    request_tamper = _attempt_validation(material, attempt_number=1)
    request_tamper["source_run"]["request"]["forged"] = True
    with pytest.raises(EvidenceIntegrityError, match="source_request_digest"):
        validate_clarification_resolution_attempt(**request_tamper)

    choice_tamper = _attempt_validation(material, attempt_number=1)
    choice_tamper["resolution"]["accepted_choice"]["forged"] = True
    with pytest.raises(EvidenceIntegrityError, match="accepted_choice_mismatch"):
        validate_clarification_resolution_attempt(**choice_tamper)


def test_freeform_answer_binds_the_only_user_redirect_action():
    material = _authority_material(freeform=True)

    resolved = validate_clarification_resolution_attempt(
        **_attempt_validation(material, attempt_number=1)
    )

    assert resolved["accepted_choice"]["action_kind"] == "user_redirect"
    assert resolved["material_patch"] == {}


def test_dispatch_payload_rejects_unknown_or_missing_business_fields():
    values = _attempt_validation(_authority_material(), attempt_number=2)
    values["dispatch"]["request_payload"]["forged"] = True
    values["dispatch"]["request_digest"] = clarification_attempt_request_digest(
        producer_kind=values["dispatch"]["producer_kind"],
        scope_ref=values["dispatch"]["scope_ref"],
        thread_id=values["dispatch"]["thread_id"],
        text=values["dispatch"]["text"],
        request_payload=values["dispatch"]["request_payload"],
    )
    values["attempt"]["request_digest"] = values["dispatch"][
        "request_digest"
    ]

    with pytest.raises(EvidenceIntegrityError, match="request_payload_mismatch"):
        validate_clarification_resolution_attempt(**values)
