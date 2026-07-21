from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

import pytest

from bi_agent.runtime.capability_authority import EvidenceLedgerEntry
from bi_agent.runtime.claim_authority import (
    ClaimAuthorityNamespace,
    ClaimPublicationCeiling,
    ClaimRevision,
    ObligationCoverage,
    SemanticVerificationDecision,
    SupportEdge,
)
from bi_agent.runtime.claim_settlement import (
    AuthorityBundleInputs,
    prepare_claim_settlement,
    publication_ceiling_satisfies,
    settle_claim_checkpoint,
)
from bi_agent.runtime.evidence_authority import canonical_value
from bi_agent.runtime.narrative_authority import (
    PublicationFieldVisibilityPolicy,
    PublicClaimPalette,
    PublicFactDescriptor,
    PublicLimitation,
)
from bi_agent.runtime.narrative_material_projection import (
    NarrativeMaterialProjection,
    NarrativeMaterialProjectionContractError,
    ProjectedEvidenceMaterial,
    ProjectedPublicationRequirement,
    _project_material_facts,
)
from tests.phase7.test_claim_settlement import (
    _EvidenceSpec,
    _TaskSpec,
    _execution_result,
    _settle,
)


_DIMENSION_INTERPRETATION_CONTRACT = {
    "contract_id": "dimension-localization-interpretation.v1",
    "ranking_scope": "cross_dimension_diagnostic_priority",
    "ranking_measure": "diagnostic_priority_score",
    "cross_dimension_overlap": "overlapping_marginal_views",
    "cross_dimension_additivity": "forbidden",
    "within_dimension_additivity": {
        "scope": "complete_reconciled_partition",
        "additive_measures": ("baseline_amount", "target_amount", "delta"),
    },
    "contribution_semantics": {
        "delta": "within_dimension_accounting_change",
        "excess_delta": "baseline_mix_structural_deviation",
    },
}
_FORMULA_INTERPRETATION_CONTRACT = {
    "contract_id": "formula-accounting-decomposition-interpretation.v1",
    "ranking_scope": "within_formula_decomposition_components",
    "contribution_semantics": {
        "contribution": "signed_accounting_component_change",
        "contribution_share": "signed_share_of_contribution_total",
    },
    "contribution_share_denominator": "decomposition.contribution_total",
    "contribution_share_range": "unbounded_signed",
    "zero_contribution_total_policy": "contribution_share_unavailable",
}


def _policy() -> PublicationFieldVisibilityPolicy:
    return PublicationFieldVisibilityPolicy.fixed(
        policy_id="narrative-material-projection",
        revision=1,
        restricted_output_policy_ref="test-policy:aggregate-only",
        restricted_output_policy_version="1",
        restricted_output_fields=("owner_id", "raw_rows"),
    )


def _fixture(
    *,
    fact_count: int = 3,
    fact_names: tuple[str, ...] | None = None,
    second_claim_value_override: str | None = None,
    interpretation_contract: Mapping[str, object] | None = None,
):
    names = fact_names or tuple(
        f"source_1.metric_{index}" for index in range(fact_count)
    )
    execution = _execution_result(
        obligations={
            "required_change": ("comparative_change", "observed"),
            "auxiliary_change": ("comparative_change", "observed"),
        },
        tasks=(
            _TaskSpec(
                task_key="shared_observation",
                capability_id="shared_observation",
                obligation_names=("required_change", "auxiliary_change"),
                evidence=(
                    _EvidenceSpec(
                        evidence_kind="observed",
                        maximum_claim_strength="directional",
                        supported_claim_kinds=("comparative_change",),
                        observation_name="absolute_change",
                        observation_value=-12,
                        observation_fact=(
                            {
                                "name": "absolute_change",
                                "value": -12,
                                "interpretation_contract": (interpretation_contract),
                            }
                            if interpretation_contract is not None
                            else None
                        ),
                        limitation_refs=(
                            "limitation:shared-scope",
                            "limitation:shared-window",
                        ),
                    ),
                ),
            ),
        ),
    )
    namespace = ClaimAuthorityNamespace.create(
        run_attempt_id=execution.run_attempt_id,
        intent_revision_id=execution.intent_revision_id,
        plan_revision_id=execution.plan_revision_id,
    )
    checkpoint = prepare_claim_settlement(
        execution,
        authority_namespace=namespace,
    )
    attempt = checkpoint.verification_attempt(
        provider_ref="provider:test",
        model_ref="model:claim-verifier",
        input_digest="a" * 64,
        attempt_number=1,
        raw_provider_response_ref="restricted-provider-response:projection",
        raw_provider_response_digest="b" * 64,
    )
    decisions = tuple(
        SemanticVerificationDecision.create(
            authority_namespace=namespace,
            verification_attempt=attempt,
            subject_ref=claim.claim_ref,
            disposition="accepted",
            veto_basis=None,
            reason_code=None,
            limitation_refs=(),
        )
        for claim in checkpoint.proposed_claims
    )
    settlement = settle_claim_checkpoint(
        checkpoint,
        verification_attempt=attempt,
        verification_decisions=decisions,
    )
    inputs = AuthorityBundleInputs.create(
        execution_result=execution,
        claim_settlement=settlement,
        recommendations=(),
    )
    bundle = inputs.seal(
        bundle_revision=1,
        supersedes_bundle_ref=None,
        sealed_at="2026-07-19T00:00:00Z",
    )
    entries = tuple(
        entry
        for _, _, evidence_entries, _ in execution.capability_outcome_bundles
        for entry in evidence_entries
    )
    facts = []
    for claim_index, claim in enumerate(settlement.accepted_claims):
        evidence_edges = tuple(
            edge
            for edge in settlement.accepted_support_edges
            if edge.support_edge_ref in set(claim.support_edge_refs)
            and edge.kind == "supports"
            and edge.source_type == "evidence"
        )
        assert len(evidence_edges) == 1
        edge = evidence_edges[0]
        for fact_index, public_name in enumerate(names):
            value = str(fact_index + 1)
            if claim_index == 1 and fact_index == 0:
                value = second_claim_value_override or value
            facts.append(
                PublicFactDescriptor.create(
                    claim=claim,
                    public_name=public_name,
                    fact_kind="number",
                    value=value,
                    range_end=None,
                    unit="index",
                    source_material_ref=edge.support_edge_ref,
                )
            )
    limitations = tuple(
        PublicLimitation.create(
            limitation_ref=limitation_ref,
            public_context={
                "claims": (
                    {
                        "claim_class": "observed_fact",
                        "scope": "scope:full-sample",
                    },
                ),
                "obligations": (
                    {
                        "status": "satisfied",
                        "boundary": limitation_ref,
                    },
                ),
            },
        )
        for limitation_ref in bundle.limitation_refs
    )
    palette = PublicClaimPalette.derive(
        authority_bundle=bundle,
        claims=settlement.accepted_claims,
        claim_keys=settlement.accepted_claim_keys,
        recommendations=(),
        public_facts=tuple(facts),
        public_limitations=limitations,
        visibility_policy=_policy(),
    )
    return palette, settlement, entries


def _derive(fixture) -> NarrativeMaterialProjection:
    palette, settlement, entries = fixture
    return NarrativeMaterialProjection.derive(
        palette=palette,
        claim_settlement=settlement,
        evidence_entries=entries,
    )


def test_shared_evidence_is_pooled_once_with_exact_claim_material_links() -> None:
    palette, settlement, entries = _fixture()

    projection = _derive((palette, settlement, entries))
    payload = projection.to_writer_payload()

    assert len(projection.evidence_materials) == 1
    assert len(payload["evidence_materials"]) == 1
    material = payload["evidence_materials"][0]
    assert projection.evidence_materials[0].interpretation_contract == {}
    assert material["interpretation_contract"] == {}
    assert [fact["name"] for fact in material["facts"]] == [
        "metric_0",
        "metric_1",
        "metric_2",
    ]
    assert all(
        claim["material_handles"] == [material["material_handle"]]
        for claim in payload["claims"]
    )
    assert {
        fact_ref
        for item in projection.evidence_materials[0].facts
        for fact_ref in item.source_fact_refs
    } == {fact.fact_ref for claim in palette.claims for fact in claim.facts}
    assert set(entries[0].entry_ref for _ in (0,)) == {
        projection.evidence_materials[0].evidence_entry_ref
    }


@pytest.mark.parametrize(
    "interpretation_contract",
    (
        _DIMENSION_INTERPRETATION_CONTRACT,
        _FORMULA_INTERPRETATION_CONTRACT,
    ),
    ids=("dimension", "formula"),
)
def test_typed_interpretation_contract_survives_projection_and_replay(
    interpretation_contract: Mapping[str, object],
) -> None:
    palette, settlement, entries = _fixture(
        interpretation_contract=interpretation_contract
    )

    projection = _derive((palette, settlement, entries))
    material = projection.evidence_materials[0]
    expected = canonical_value(interpretation_contract)

    assert isinstance(material.interpretation_contract, Mapping)
    assert canonical_value(material.interpretation_contract) == expected
    assert (
        projection.to_dict()["evidence_materials"][0]["interpretation_contract"]
        == expected
    )
    writer_contract = projection.to_writer_payload()["evidence_materials"][0][
        "interpretation_contract"
    ]
    assert isinstance(writer_contract, dict)
    assert writer_contract == expected
    assert (
        NarrativeMaterialProjection.from_dict(
            projection.to_dict(),
            palette=palette,
            claim_settlement=settlement,
            evidence_entries=entries,
        )
        == projection
    )

    tampered = replace(
        material,
        interpretation_contract={"contract_id": "tampered"},
    )
    with pytest.raises(
        NarrativeMaterialProjectionContractError,
        match="narrative_material_projection_integrity_invalid",
    ):
        tampered.assert_integrity()


def test_inconsistent_interpretation_contracts_fail_closed() -> None:
    _, _, entries = _fixture()
    source = entries[0]
    components = {
        key: value
        for key, value in source.to_dict().items()
        if key
        not in {
            "entry_ref",
            "result_membership_digest",
            "completeness_membership_digest",
            "content_digest",
        }
    }
    components["observation_facts"] = (
        {"interpretation_contract": _DIMENSION_INTERPRETATION_CONTRACT},
        {"interpretation_contract": _FORMULA_INTERPRETATION_CONTRACT},
    )
    conflicting_entry = EvidenceLedgerEntry._from_components(**components)

    with pytest.raises(
        NarrativeMaterialProjectionContractError,
        match="narrative_material_projection_interpretation_contract_conflict",
    ):
        ProjectedEvidenceMaterial.create(
            evidence_entry=conflicting_entry,
            evidence_edge_refs=("support-edge:contract-conflict",),
            facts=(),
        )


def test_only_user_required_obligations_become_writer_publication_requirements() -> (
    None
):
    palette, settlement, entries = _fixture()

    projection = _derive((palette, settlement, entries))
    requirements = projection.publication_requirements
    basis_by_id = {
        item.obligation_id: item for item in settlement.checkpoint.obligation_basis
    }
    coverage_by_id = {
        item.obligation_id: item for item in settlement.obligation_coverage
    }
    claim_by_ref = {item.claim_ref: item for item in settlement.accepted_claims}

    assert tuple(item.obligation_id for item in requirements) == (
        palette.required_obligation_ids
    )
    assert len(palette.required_obligation_ids) == 1
    auxiliary_ids = {
        item.obligation_id for item in settlement.obligation_coverage
    } - set(palette.required_obligation_ids)
    assert len(auxiliary_ids) == 1
    assert not auxiliary_ids.intersection(item.obligation_id for item in requirements)
    requirement = requirements[0]
    basis = basis_by_id[requirement.obligation_id]
    coverage = coverage_by_id[requirement.obligation_id]
    assert requirement.status == coverage.status == "satisfied"
    assert requirement.coverage_semantics == "supported"
    assert requirement.claim_kind == "comparative_change"
    assert requirement.assertion_scope["scope_effect"] == "local_claim_family"
    assert requirement.required_claim_strength == basis.required_claim_strength
    assert set(requirement.claim_refs).issubset(coverage.claim_refs)
    assert all(
        publication_ceiling_satisfies(
            claim_by_ref[claim_ref].publication_ceiling,
            required_strength=requirement.required_claim_strength,
        )
        for claim_ref in requirement.claim_refs
    )
    assert requirement.limitation_handles == ()
    writer_requirement = projection.to_writer_payload()["publication_requirements"][0]
    assert set(writer_requirement) == {
        "requirement_handle",
        "status",
        "coverage_semantics",
        "claim_kind",
        "assertion_scope",
        "required_claim_strength",
        "claim_handles",
        "limitation_handles",
    }
    assert requirement.obligation_id not in writer_requirement.values()


def test_publication_requirement_statuses_preserve_exact_handle_closure() -> None:
    palette, settlement, entries = _fixture()
    projection = _derive((palette, settlement, entries))
    obligation_id = palette.required_obligation_ids[0]
    basis = next(
        item
        for item in settlement.checkpoint.obligation_basis
        if item.obligation_id == obligation_id
    )
    source_coverage = next(
        item
        for item in settlement.obligation_coverage
        if item.obligation_id == obligation_id
    )
    claim_ref = source_coverage.claim_refs[0]
    limitation_refs = tuple(item.limitation_ref for item in projection.limitations)
    assert limitation_refs
    accepted_claims_by_ref = {
        item.claim_ref: item for item in settlement.accepted_claims
    }
    claim_handle_by_ref = {
        item.claim_ref: item.claim_handle for item in projection.claims
    }
    limitation_handle_by_ref = {
        item.limitation_ref: item.limitation_handle for item in projection.limitations
    }
    semantic_source = projection.publication_requirements[0]
    expected_coverage_semantics = {
        "satisfied": "supported",
        "mixed": "supported_with_limitations",
        "contradicted": "contradicted",
        "unavailable": "unavailable",
    }

    for status, claim_refs, status_limitation_refs in (
        ("satisfied", (claim_ref,), ()),
        ("mixed", (claim_ref,), limitation_refs),
        ("contradicted", (claim_ref,), limitation_refs),
        ("unavailable", (), limitation_refs),
    ):
        coverage = ObligationCoverage.create(
            authority_namespace=settlement.authority_namespace,
            verifier_report=settlement.verifier_report,
            obligation_id=obligation_id,
            status=status,
            claim_refs=claim_refs,
            limitation_refs=status_limitation_refs,
        )
        requirement = ProjectedPublicationRequirement.create(
            basis=basis,
            coverage=coverage,
            accepted_claims_by_ref=accepted_claims_by_ref,
            claim_handle_by_ref=claim_handle_by_ref,
            limitation_handle_by_ref=limitation_handle_by_ref,
            claim_kind=semantic_source.claim_kind,
            assertion_scope=semantic_source.assertion_scope,
        )

        assert requirement.status == status
        assert requirement.coverage_semantics == expected_coverage_semantics[status]
        assert requirement.claim_kind == "comparative_change"
        assert requirement.assertion_scope == semantic_source.assertion_scope
        assert requirement.claim_handles == tuple(
            claim_handle_by_ref[ref] for ref in requirement.claim_refs
        )
        assert requirement.limitation_handles == tuple(
            limitation_handle_by_ref[ref] for ref in requirement.limitation_refs
        )
        if status in {"mixed", "contradicted", "unavailable"}:
            assert requirement.limitation_refs == coverage.limitation_refs

    for status, claim_refs, status_limitation_refs in (
        ("satisfied", (claim_ref,), limitation_refs),
        ("mixed", (claim_ref,), ()),
        ("unavailable", (claim_ref,), limitation_refs),
    ):
        invalid_coverage = ObligationCoverage.create(
            authority_namespace=settlement.authority_namespace,
            verifier_report=settlement.verifier_report,
            obligation_id=obligation_id,
            status=status,
            claim_refs=claim_refs,
            limitation_refs=status_limitation_refs,
        )
        with pytest.raises(
            NarrativeMaterialProjectionContractError,
            match="narrative_material_projection_requirement_status_closure_invalid",
        ):
            ProjectedPublicationRequirement.create(
                basis=basis,
                coverage=invalid_coverage,
                accepted_claims_by_ref=accepted_claims_by_ref,
                claim_handle_by_ref=claim_handle_by_ref,
                limitation_handle_by_ref=limitation_handle_by_ref,
                claim_kind=semantic_source.claim_kind,
                assertion_scope=semantic_source.assertion_scope,
            )


def test_satisfied_requirement_filters_claims_below_required_strength() -> None:
    palette, settlement, entries = _fixture()
    projection = _derive((palette, settlement, entries))
    obligation_id = palette.required_obligation_ids[0]
    basis = next(
        item
        for item in settlement.checkpoint.obligation_basis
        if item.obligation_id == obligation_id
    )
    source = next(
        item
        for item in settlement.accepted_claims
        if item.claim_ref
        in next(
            coverage.claim_refs
            for coverage in settlement.obligation_coverage
            if coverage.obligation_id == obligation_id
        )
    )
    claim_key = next(
        item
        for item in settlement.accepted_claim_keys
        if item.claim_key == source.claim_key
    )
    support_edges = tuple(
        item
        for item in settlement.accepted_support_edges
        if item.support_edge_ref in set(source.support_edge_refs)
    )
    weaker = ClaimRevision.create(
        authority_namespace=settlement.authority_namespace,
        claim_key=claim_key,
        factual_payload=source.factual_payload,
        claim_class=source.claim_class,
        support_edges=support_edges,
        dependency_claim_refs=source.dependency_claim_refs,
        limitation_refs=source.limitation_refs,
        status=source.status,
        publication_ceiling=ClaimPublicationCeiling.create(
            claim_class=source.claim_class,
            strength="descriptive",
        ),
    )
    coverage = ObligationCoverage.create(
        authority_namespace=settlement.authority_namespace,
        verifier_report=settlement.verifier_report,
        obligation_id=obligation_id,
        status="satisfied",
        claim_refs=(source.claim_ref, weaker.claim_ref),
        limitation_refs=(),
    )
    accepted_claims_by_ref = {
        item.claim_ref: item for item in (*settlement.accepted_claims, weaker)
    }
    claim_handle_by_ref = {
        item.claim_ref: item.claim_handle for item in projection.claims
    }
    claim_handle_by_ref[weaker.claim_ref] = "c_weaker_descriptive"
    semantic_source = projection.publication_requirements[0]
    requirement = ProjectedPublicationRequirement.create(
        basis=basis,
        coverage=coverage,
        accepted_claims_by_ref=accepted_claims_by_ref,
        claim_handle_by_ref=claim_handle_by_ref,
        limitation_handle_by_ref={},
        claim_kind=semantic_source.claim_kind,
        assertion_scope=semantic_source.assertion_scope,
    )

    assert source.claim_ref in requirement.claim_refs
    assert weaker.claim_ref not in requirement.claim_refs


def test_unavailable_required_obligation_projects_its_complete_boundary() -> None:
    execution = _execution_result(
        obligations={"required_change": ("comparative_change", "observed")},
        tasks=(
            _TaskSpec(
                task_key="unavailable_source",
                capability_id="unavailable_source",
                obligation_names=("required_change",),
                status="unavailable",
                limitation_refs=("limitation:source-unavailable",),
            ),
        ),
    )
    settlement = _settle(execution)
    inputs = AuthorityBundleInputs.create(
        execution_result=execution,
        claim_settlement=settlement,
        recommendations=(),
    )
    bundle = inputs.seal(
        bundle_revision=1,
        supersedes_bundle_ref=None,
        sealed_at="2026-07-19T00:00:00Z",
    )
    limitations = tuple(
        PublicLimitation.create(
            limitation_ref=limitation_ref,
            public_context={
                "applicability": (
                    {
                        "scope_effect": "local_claim_family",
                        "affected_claim_kinds": ("comparative_change",),
                    },
                ),
                "obligations": (
                    {
                        "obligation_id": bundle.required_obligation_ids[0],
                        "status": "unavailable",
                        "claim_kind": "comparative_change",
                        "role": "user_required",
                        "boundary": limitation_ref,
                    },
                ),
            },
        )
        for limitation_ref in bundle.limitation_refs
    )
    palette = PublicClaimPalette.derive(
        authority_bundle=bundle,
        claims=(),
        claim_keys=(),
        recommendations=(),
        public_facts=(),
        public_limitations=limitations,
        visibility_policy=_policy(),
    )

    projection = NarrativeMaterialProjection.derive(
        palette=palette,
        claim_settlement=settlement,
        evidence_entries=(),
    )

    assert projection.authority_mode == "boundary_only"
    assert projection.claims == ()
    assert len(projection.publication_requirements) == 1
    requirement = projection.publication_requirements[0]
    assert requirement.obligation_id == bundle.required_obligation_ids[0]
    assert requirement.status == "unavailable"
    assert requirement.coverage_semantics == "unavailable"
    assert requirement.claim_kind == "comparative_change"
    assert requirement.assertion_scope["scope_effect"] == "local_claim_family"
    assert requirement.claim_handles == ()
    assert requirement.limitation_refs == tuple(
        item.limitation_ref for item in projection.limitations
    )
    assert requirement.limitation_handles == tuple(
        item.limitation_handle for item in projection.limitations
    )


def test_writer_shape_pools_boundary_facets_and_reconstructs_every_record() -> None:
    palette, settlement, entries = _fixture()

    projection = _derive((palette, settlement, entries))
    payload = projection.to_writer_payload()

    assert set(payload) == {
        "authority_mode",
        "claims",
        "publication_requirements",
        "evidence_materials",
        "recommendations",
        "limitations",
        "boundary_facets",
    }
    assert len(payload["boundary_facets"]) < sum(
        len(records)
        for limitation in palette.limitations
        for records in limitation.public_context.values()
    )
    reconstructed = projection.reconstruct_limitation_contexts()
    assert reconstructed == {
        limitation.limitation_ref: canonical_value(limitation.public_context)
        for limitation in palette.limitations
    }
    assert all(
        set(item) == {"limitation_handle", "boundary_facet_handles"}
        for item in payload["limitations"]
    )


def test_shared_evidence_fact_conflict_fails_closed() -> None:
    fixture = _fixture(second_claim_value_override="999")

    with pytest.raises(
        NarrativeMaterialProjectionContractError,
        match="narrative_material_projection_shared_fact_conflict",
    ):
        _derive(fixture)


def test_same_claim_duplicate_support_atom_pools_all_source_fact_refs() -> None:
    _, settlement, entries = _fixture(fact_count=1)
    source_claim = settlement.accepted_claims[0]
    claim_key = next(
        item
        for item in settlement.accepted_claim_keys
        if item.claim_key == source_claim.claim_key
    )
    first_edge = next(
        item
        for item in settlement.accepted_support_edges
        if item.support_edge_ref in set(source_claim.support_edge_refs)
        and item.kind == "supports"
    )
    second_edge = SupportEdge.create(
        authority_namespace=settlement.authority_namespace,
        kind="supports",
        source_type="evidence",
        source_ref=first_edge.source_ref,
        source_epistemic_class=first_edge.source_epistemic_class,
        source_publication_ceiling=first_edge.source_publication_ceiling,
        target_claim_key=first_edge.target_claim_key,
        limitation_refs=(*first_edge.limitation_refs, "limitation:second-edge"),
    )
    claim = ClaimRevision.create(
        authority_namespace=settlement.authority_namespace,
        claim_key=claim_key,
        factual_payload=source_claim.factual_payload,
        claim_class=source_claim.claim_class,
        support_edges=(first_edge, second_edge),
        dependency_claim_refs=(),
        limitation_refs=(*source_claim.limitation_refs, "limitation:second-edge"),
        status="verified",
        publication_ceiling=source_claim.publication_ceiling,
    )
    facts = tuple(
        PublicFactDescriptor.create(
            claim=claim,
            public_name=f"source_{index}.metric",
            fact_kind="number",
            value="1",
            range_end=None,
            unit="index",
            source_material_ref=edge.support_edge_ref,
        )
        for index, edge in enumerate(
            sorted((first_edge, second_edge), key=lambda item: item.support_edge_ref),
            start=1,
        )
    )

    projected = _project_material_facts(
        evidence_entry_ref=entries[0].entry_ref,
        candidates=tuple((claim.claim_ref, fact, "metric") for fact in facts),
    )

    assert len(projected) == 1
    assert projected[0].source_fact_refs == tuple(
        sorted(item.fact_ref for item in facts)
    )


def test_only_verified_positive_materializer_prefix_is_normalized() -> None:
    projection = _derive(
        _fixture(
            fact_names=(
                "source_1.metric",
                "source_0.literal",
                "source_01.literal",
                "nested.source_1.literal",
            )
        )
    )

    assert {item.name for item in projection.evidence_materials[0].facts} == {
        "metric",
        "source_0.literal",
        "source_01.literal",
        "nested.source_1.literal",
    }

    with pytest.raises(
        NarrativeMaterialProjectionContractError,
        match="narrative_material_projection_source_prefix_mismatch",
    ):
        _derive(_fixture(fact_names=("source_2.metric",)))


def test_missing_accepted_source_edge_fails_closed() -> None:
    palette, settlement, entries = _fixture()
    broken = replace(settlement, accepted_support_edges=())

    with pytest.raises(
        NarrativeMaterialProjectionContractError,
        match="narrative_material_projection_source_edge_missing",
    ):
        NarrativeMaterialProjection.derive(
            palette=palette,
            claim_settlement=broken,
            evidence_entries=entries,
        )


def test_dense_projection_preserves_every_unique_fact_without_a_case_limit() -> None:
    fact_count = 257
    palette, settlement, entries = _fixture(fact_count=fact_count)

    projection = _derive((palette, settlement, entries))

    assert len(projection.evidence_materials) == 1
    assert len(projection.evidence_materials[0].facts) == fact_count
    assert sum(
        len(fact.source_fact_refs) for fact in projection.evidence_materials[0].facts
    ) == fact_count * len(palette.claims)
    assert projection.to_writer_payload()["evidence_materials"][0]["facts"][-1][
        "name"
    ] in {f"metric_{index}" for index in range(fact_count)}


def test_round_trip_and_integrity_reject_any_projection_mutation() -> None:
    palette, settlement, entries = _fixture()
    projection = _derive((palette, settlement, entries))

    assert (
        NarrativeMaterialProjection.from_dict(
            projection.to_dict(),
            palette=palette,
            claim_settlement=settlement,
            evidence_entries=entries,
        )
        == projection
    )
    projection.assert_integrity()

    payload = projection.to_dict()
    payload["evidence_materials"][0]["facts"][0]["value"] = "1000"
    with pytest.raises(
        NarrativeMaterialProjectionContractError,
        match="narrative_material_projection_integrity_invalid",
    ):
        NarrativeMaterialProjection.from_dict(
            payload,
            palette=palette,
            claim_settlement=settlement,
            evidence_entries=entries,
        )

    for field, value in (
        ("status", "mixed"),
        ("coverage_semantics", "contradicted"),
        ("claim_kind", "accounting_driver"),
        (
            "assertion_scope",
            {
                "scope_effect": "local_claim_family",
                "metric_refs": ["metric:tampered"],
                "target_window_refs": [],
                "baseline_window_refs": [],
                "scope_refs": ["scope:full-sample"],
                "grains": ["aggregate"],
                "dimension_paths": [[]],
            },
        ),
        ("required_claim_strength", "candidate_driver"),
        ("claim_handles", ["c_tampered"]),
        ("requirement_handle", "pr_tampered"),
    ):
        tampered = projection.to_dict()
        tampered["publication_requirements"][0][field] = value
        with pytest.raises(
            NarrativeMaterialProjectionContractError,
            match="narrative_material_projection_integrity_invalid",
        ):
            NarrativeMaterialProjection.from_dict(
                tampered,
                palette=palette,
                claim_settlement=settlement,
                evidence_entries=entries,
            )
