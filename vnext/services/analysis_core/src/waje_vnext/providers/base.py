"""Provider port and provider-layer failure taxonomy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from waje_vnext.domain.actions import AgentActionProposal
from waje_vnext.domain.controller import PrimaryAgentRequest
from waje_vnext.domain.runtime_amendment import (
    FrameReviewProposal,
    FrameReviewRequest,
    MessageBindingRequest,
    MessageImpactProposal,
)


class ProviderError(RuntimeError):
    pass


class ProviderConfigurationError(ProviderError):
    pass


class ProviderTransientError(ProviderError):
    pass


class ProviderPermanentError(ProviderError):
    pass


@dataclass(frozen=True, slots=True)
class ProviderAttemptTrace:
    disposition: str
    provider_response_id: str | None
    output_sha256: str | None
    finish_reason: str | None
    usage_payload: dict[str, object]
    completed_at: datetime


class PrimaryAgentProvider(Protocol):
    def propose(self, request: PrimaryAgentRequest) -> AgentActionProposal: ...


class MessageBindingProvider(Protocol):
    def bind_message(
        self,
        request: MessageBindingRequest,
    ) -> MessageImpactProposal: ...


class MeasurementReviewerProvider(Protocol):
    def review(self, request: FrameReviewRequest) -> FrameReviewProposal: ...
