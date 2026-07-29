"""Effect execution port used by the authoritative controller."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Mapping, Protocol

from waje_vnext.domain.canonical import (
    FrozenJson,
    content_sha256,
    freeze_json,
    require_nonempty,
)
from waje_vnext.domain.runtime_state import OutboxMessage


class EffectError(RuntimeError):
    pass


class EffectTransientError(EffectError):
    pass


class EffectPermanentError(EffectError):
    pass


@dataclass(frozen=True, slots=True)
class EffectExecutionResult:
    payload: Mapping[str, FrozenJson]
    business_summary: str

    def __post_init__(self) -> None:
        require_nonempty(self.business_summary, "business_summary")
        frozen = freeze_json(self.payload)
        if not isinstance(frozen, Mapping):
            raise TypeError("effect result payload must be a JSON object")
        object.__setattr__(self, "payload", frozen)

    @property
    def content_sha256(self) -> str:
        return content_sha256(
            {
                "payload": self.payload,
                "business_summary": self.business_summary,
            }
        )


class EffectExecutor(Protocol):
    def execute(self, message: OutboxMessage) -> EffectExecutionResult: ...


class ScriptedEffectExecutor:
    """Deterministic, idempotency-aware executor for runtime acceptance tests."""

    def __init__(
        self,
        outcomes: Iterable[
            EffectExecutionResult | EffectTransientError | EffectPermanentError
        ],
    ) -> None:
        self._outcomes = deque(outcomes)
        self.calls: list[OutboxMessage] = []
        self._completed: dict[str, EffectExecutionResult] = {}

    def execute(self, message: OutboxMessage) -> EffectExecutionResult:
        self.calls.append(message)
        completed = self._completed.get(message.idempotency_key)
        if completed is not None:
            return completed
        if not self._outcomes:
            raise EffectPermanentError("scripted executor has no outcome left")
        outcome = self._outcomes.popleft()
        if isinstance(outcome, EffectError):
            raise outcome
        self._completed[message.idempotency_key] = outcome
        return outcome
