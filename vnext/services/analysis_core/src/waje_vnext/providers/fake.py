"""Deterministic provider for controller and replay tests."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from waje_vnext.domain.actions import AgentActionProposal
from waje_vnext.domain.controller import PrimaryAgentRequest

from .base import ProviderPermanentError


class ScriptedPrimaryAgentProvider:
    def __init__(self, proposals: Iterable[AgentActionProposal]) -> None:
        self._proposals = deque(proposals)
        self.requests: list[PrimaryAgentRequest] = []

    def propose(self, request: PrimaryAgentRequest) -> AgentActionProposal:
        self.requests.append(request)
        if not self._proposals:
            raise ProviderPermanentError("scripted provider has no proposal left")
        return self._proposals.popleft()
