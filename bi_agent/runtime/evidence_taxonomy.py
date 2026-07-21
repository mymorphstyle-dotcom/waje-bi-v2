from __future__ import annotations

from types import MappingProxyType
from typing import Sequence


class EvidenceTaxonomyContractError(ValueError):
    pass


PUBLISHABLE_EVIDENCE_KIND_BY_TYPE = MappingProxyType(
    {
        "observed_comparison": "observed",
        "accounting_contribution": "derived",
        "dimension_localization": "derived",
        "candidate_mechanism": "observed",
        "statistical_association": "statistical_association",
        "trust_boundary": "boundary",
    }
)
NON_PUBLISHABLE_EVIDENCE_TYPES = frozenset({"insufficient_evidence"})


def publication_evidence_kind(evidence_type: str) -> str:
    if not isinstance(evidence_type, str) or not evidence_type:
        raise EvidenceTaxonomyContractError("evidence_type_invalid")
    if evidence_type in NON_PUBLISHABLE_EVIDENCE_TYPES:
        raise EvidenceTaxonomyContractError(
            f"evidence_type_not_publishable:{evidence_type}"
        )
    evidence_kind = PUBLISHABLE_EVIDENCE_KIND_BY_TYPE.get(evidence_type)
    if evidence_kind is None:
        raise EvidenceTaxonomyContractError(f"evidence_type_unknown:{evidence_type}")
    return evidence_kind


def publication_evidence_kinds(
    evidence_types: Sequence[str],
) -> tuple[str, ...]:
    if isinstance(evidence_types, (str, bytes)) or not isinstance(
        evidence_types, Sequence
    ):
        raise EvidenceTaxonomyContractError("evidence_types_invalid")
    kinds: list[str] = []
    for evidence_type in evidence_types:
        if evidence_type in NON_PUBLISHABLE_EVIDENCE_TYPES:
            continue
        kind = publication_evidence_kind(evidence_type)
        if kind not in kinds:
            kinds.append(kind)
    if not kinds:
        raise EvidenceTaxonomyContractError(
            "evidence_types_have_no_publication_authority"
        )
    return tuple(kinds)


__all__ = (
    "EvidenceTaxonomyContractError",
    "NON_PUBLISHABLE_EVIDENCE_TYPES",
    "PUBLISHABLE_EVIDENCE_KIND_BY_TYPE",
    "publication_evidence_kind",
    "publication_evidence_kinds",
)
