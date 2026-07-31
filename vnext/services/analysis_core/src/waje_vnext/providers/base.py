"""Provider port and provider-layer failure taxonomy."""

from __future__ import annotations

from typing import Protocol

from waje_vnext.domain.actions import AgentActionProposal
from waje_vnext.domain.controller import PrimaryAgentRequest


class ProviderError(RuntimeError):
    pass


class ProviderConfigurationError(ProviderError):
    pass


class ProviderTransientError(ProviderError):
    pass


class ProviderPermanentError(ProviderError):
    pass


class PrimaryAgentProvider(Protocol):
    def propose(self, request: PrimaryAgentRequest) -> AgentActionProposal: ...
