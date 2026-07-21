from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class EvidenceEnvelope:
    evidence_ref: str
    capability: str
    evidence_type: str
    strength: str
    wording_limit: str
    numeric_facts: dict[str, Any] = field(default_factory=dict)
    typed_payload: dict[str, Any] = field(default_factory=dict)
    limitations: tuple[str, ...] = ()
    result_refs: tuple[str, ...] = ()


def make_evidence_envelope(
    capability: str,
    *,
    evidence_type: str,
    strength: str = "low",
    wording_limit: str = "insufficient",
    numeric_facts: dict[str, Any] | None = None,
    typed_payload: dict[str, Any] | None = None,
    limitations: tuple[str, ...] = (),
    result_refs: tuple[str, ...] = (),
    evidence_ref: str | None = None,
) -> EvidenceEnvelope:
    return EvidenceEnvelope(
        evidence_ref=evidence_ref or f"{capability}:inline",
        capability=capability,
        evidence_type=evidence_type,
        strength=strength,
        wording_limit=wording_limit,
        numeric_facts=numeric_facts or {},
        typed_payload=typed_payload or {},
        limitations=limitations,
        result_refs=result_refs,
    )
