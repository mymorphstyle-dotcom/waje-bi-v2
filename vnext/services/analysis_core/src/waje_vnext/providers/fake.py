"""Deterministic provider for controller and replay tests."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
import json

from waje_vnext.domain.actions import AgentActionProposal
from waje_vnext.domain.controller import PrimaryAgentRequest
from waje_vnext.domain.runtime_amendment import (
    FrameReviewDisposition,
    FrameReviewProposal,
    FrameReviewRequest,
    MessageBindingDisposition,
    MessageBindingRequest,
    MessageImpactKind,
    MessageImpactProposal,
    ProposedSemanticAssertion,
    SemanticAssertionKind,
)

from .base import ProviderPermanentError


class ScriptedPrimaryAgentProvider:
    allows_test_role_multiplexing = True

    def __init__(self, proposals: Iterable[AgentActionProposal]) -> None:
        self._proposals = deque(proposals)
        self.requests: list[PrimaryAgentRequest] = []
        self.binding_requests: list[MessageBindingRequest] = []
        self.review_requests: list[FrameReviewRequest] = []

    def propose(self, request: PrimaryAgentRequest) -> AgentActionProposal:
        self.requests.append(request)
        if not self._proposals:
            raise ProviderPermanentError("scripted provider has no proposal left")
        return self._proposals.popleft()

    def review(self, request: FrameReviewRequest) -> FrameReviewProposal:
        self.review_requests.append(request)
        return FrameReviewProposal(
            disposition=FrameReviewDisposition.ACCEPT,
            objections=(),
            review_summary="Independent test Reviewer accepted the candidate.",
        )

    def bind_message(
        self,
        request: MessageBindingRequest,
    ) -> MessageImpactProposal:
        self.binding_requests.append(request)
        return MessageImpactProposal(
            impact_kind=(
                MessageImpactKind.QUESTION_REVISION
                if request.prior_question_text is None
                else MessageImpactKind.FRAME_REVISION
            ),
            disposition=MessageBindingDisposition.ACCEPTED,
            assertions=(
                ProposedSemanticAssertion(
                    kind=SemanticAssertionKind.BUSINESS_CONSTRAINT,
                    value_json=json.dumps(
                        {"business_request": request.message_content},
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    source_start_codepoint=0,
                    source_end_codepoint=len(request.message_content),
                    material=True,
                ),
            ),
            ambiguities=(),
            clarification_options=(),
            recommended_option_id=None,
        )
