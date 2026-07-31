"""Closed storage admission checks for provisional Answer candidates."""

from __future__ import annotations

from dataclasses import replace

from waje_vnext.domain.actions import ActionKind, ProposeAnswerPayload
from waje_vnext.domain.answering import ProvisionalAnswerCandidate
from waje_vnext.domain.async_runtime import AuthoritySnapshot
from waje_vnext.domain.controller import PersistedAction


def validate_answer_candidate_action(
    *,
    candidate: ProvisionalAnswerCandidate,
    persisted_action: PersistedAction,
) -> None:
    action = persisted_action.action
    if (
        action.action_id != candidate.created_by_action_id
        or action.case_id != candidate.case_id
        or action.kind is not ActionKind.PROPOSE_ANSWER
        or action.expected_head_version
        != candidate.authority_snapshot.head_version
        or not isinstance(action.payload, ProposeAnswerPayload)
        or action.payload.claims != candidate.claims
        or action.payload.narrative_blocks != candidate.narrative_blocks
    ):
        raise ValueError(
            "answer candidate does not match its admitted propose_answer action"
        )


def accepted_answer_candidate_is_current(
    *,
    candidate: ProvisionalAnswerCandidate,
    current_authority: AuthoritySnapshot,
) -> bool:
    """Allow only the head increment caused by accepting this candidate."""

    return (
        current_authority.head_version
        == candidate.authority_snapshot.head_version + 1
        and replace(
            current_authority,
            head_version=candidate.authority_snapshot.head_version,
        )
        == candidate.authority_snapshot
    )
