from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Literal

from bi_agent.runtime.evidence_authority import (
    EvidenceIntegrityError,
    canonical_value,
)


RUN_STATUS_TRANSITIONS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "running": frozenset(
            {
                "running_workflow",
                "waiting_for_clarification",
                "completed_without_workflow",
                "failed",
            }
        ),
        "running_workflow": frozenset(
            {"waiting_for_clarification", "completed", "failed"}
        ),
        "waiting_for_clarification": frozenset(),
        "completed": frozenset(),
        "completed_without_workflow": frozenset(),
        "failed": frozenset(),
    }
)


def validate_run_status_value(status: str) -> str:
    if not isinstance(status, str) or status not in RUN_STATUS_TRANSITIONS:
        raise EvidenceIntegrityError("analysis_run_status_invalid")
    return status


def validate_run_status_transition(
    *,
    current_status: str,
    next_status: str,
    current_thread_id: str,
    current_turn_id: str,
    current_topic_id: str,
    next_thread_id: str,
    next_turn_id: str,
    next_topic_id: str,
    current_request: Any,
    next_request: Any,
) -> Literal["replay", "transition"]:
    current_status = validate_run_status_value(current_status)
    next_status = validate_run_status_value(next_status)
    current_owner = (
        str(current_thread_id or ""),
        str(current_turn_id or ""),
        str(current_topic_id or ""),
    )
    next_owner = (
        str(next_thread_id or ""),
        str(next_turn_id or ""),
        str(next_topic_id or ""),
    )
    if current_status == next_status:
        if (
            current_owner != next_owner
            or canonical_value(current_request or {})
            != canonical_value(next_request or {})
        ):
            raise EvidenceIntegrityError(
                "analysis_run_status_transition_conflict"
            )
        return "replay"

    if next_status not in RUN_STATUS_TRANSITIONS.get(
        current_status,
        frozenset(),
    ):
        raise EvidenceIntegrityError(
            "analysis_run_status_transition_conflict"
        )
    if current_owner[0] != next_owner[0]:
        raise EvidenceIntegrityError(
            "analysis_run_status_transition_conflict"
        )
    for current_value, next_value in zip(
        current_owner[1:],
        next_owner[1:],
        strict=True,
    ):
        if current_value and current_value != next_value:
            raise EvidenceIntegrityError(
                "analysis_run_status_transition_conflict"
            )
    return "transition"
