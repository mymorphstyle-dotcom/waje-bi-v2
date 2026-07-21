from __future__ import annotations

import pytest

from bi_agent.runtime.evidence_taxonomy import (
    EvidenceTaxonomyContractError,
    publication_evidence_kind,
    publication_evidence_kinds,
)


@pytest.mark.parametrize(
    ("evidence_type", "evidence_kind"),
    (
        ("observed_comparison", "observed"),
        ("accounting_contribution", "derived"),
        ("candidate_mechanism", "observed"),
        ("statistical_association", "statistical_association"),
        ("trust_boundary", "boundary"),
    ),
)
def test_primitive_evidence_types_bind_to_closed_publication_kinds(
    evidence_type: str,
    evidence_kind: str,
) -> None:
    assert publication_evidence_kind(evidence_type) == evidence_kind


def test_insufficient_evidence_cannot_become_publication_authority() -> None:
    with pytest.raises(
        EvidenceTaxonomyContractError,
        match="^evidence_type_not_publishable:insufficient_evidence$",
    ):
        publication_evidence_kind("insufficient_evidence")


def test_unknown_evidence_type_exposes_contract_drift() -> None:
    with pytest.raises(
        EvidenceTaxonomyContractError,
        match="^evidence_type_unknown:contextual_evidence$",
    ):
        publication_evidence_kind("contextual_evidence")


def test_obligation_minimum_uses_deduplicated_publication_kinds() -> None:
    assert publication_evidence_kinds(
        (
            "observed_comparison",
            "candidate_mechanism",
            "insufficient_evidence",
            "accounting_contribution",
        )
    ) == ("observed", "derived")
