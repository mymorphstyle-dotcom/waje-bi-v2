"""Effect execution port used by the authoritative controller."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Mapping, Protocol

from waje_vnext.domain.authority import (
    EvidenceStrength,
    EvidenceType,
    ResultHandle,
)
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
class EvidenceDraft:
    task_id: str
    capability_name: str
    query_spec_ref: str | None
    semantic_contract_refs: tuple[str, ...]
    snapshot_release_ref: str
    grain: str
    evidence_type: EvidenceType
    strength: EvidenceStrength
    business_summary: str
    limitations: tuple[str, ...]
    provenance: Mapping[str, FrozenJson]
    inline_payload: Mapping[str, FrozenJson] | None
    result_handle: ResultHandle | None = None

    def __post_init__(self) -> None:
        for name in (
            "task_id",
            "capability_name",
            "snapshot_release_ref",
            "grain",
            "business_summary",
        ):
            require_nonempty(getattr(self, name), name)
        if self.query_spec_ref is not None:
            require_nonempty(self.query_spec_ref, "query_spec_ref")
        _validate_string_tuple(
            self.semantic_contract_refs,
            "semantic_contract_refs",
            required=True,
        )
        _validate_string_tuple(self.limitations, "limitations", required=False)
        if not isinstance(self.evidence_type, EvidenceType):
            raise TypeError("evidence_type must be EvidenceType")
        if not isinstance(self.strength, EvidenceStrength):
            raise TypeError("strength must be EvidenceStrength")
        frozen_provenance = freeze_json(self.provenance)
        if not isinstance(frozen_provenance, Mapping):
            raise TypeError("provenance must be a JSON object")
        object.__setattr__(self, "provenance", frozen_provenance)
        if (self.inline_payload is None) == (self.result_handle is None):
            raise ValueError(
                "evidence draft requires inline payload or result handle"
            )
        if self.inline_payload is not None:
            frozen_payload = freeze_json(self.inline_payload)
            if not isinstance(frozen_payload, Mapping):
                raise TypeError("inline_payload must be a JSON object")
            object.__setattr__(self, "inline_payload", frozen_payload)

    @property
    def payload_sha256(self) -> str:
        if self.inline_payload is not None:
            return content_sha256(self.inline_payload)
        assert self.result_handle is not None
        return self.result_handle.content_sha256

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)


@dataclass(frozen=True, slots=True)
class EffectExecutionResult:
    payload: Mapping[str, FrozenJson]
    business_summary: str
    evidence: tuple[EvidenceDraft, ...] = ()

    def __post_init__(self) -> None:
        require_nonempty(self.business_summary, "business_summary")
        frozen = freeze_json(self.payload)
        if not isinstance(frozen, Mapping):
            raise TypeError("effect result payload must be a JSON object")
        object.__setattr__(self, "payload", frozen)
        if not isinstance(self.evidence, tuple):
            raise TypeError("effect evidence must be a tuple")
        for index, draft in enumerate(self.evidence):
            if not isinstance(draft, EvidenceDraft):
                raise TypeError(
                    "effect evidence[{}] must be EvidenceDraft".format(index)
                )

    @property
    def content_sha256(self) -> str:
        return content_sha256(
            {
                "payload": self.payload,
                "business_summary": self.business_summary,
                "evidence": self.evidence,
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


def _validate_string_tuple(
    values: tuple[str, ...],
    field_name: str,
    *,
    required: bool,
) -> None:
    if not isinstance(values, tuple):
        raise TypeError("{} must be a tuple".format(field_name))
    if required and not values:
        raise ValueError("{} must be non-empty".format(field_name))
    for index, value in enumerate(values):
        if not isinstance(value, str):
            raise TypeError(
                "{}[{}] must be a string".format(field_name, index)
            )
        require_nonempty(value, "{}[{}]".format(field_name, index))
