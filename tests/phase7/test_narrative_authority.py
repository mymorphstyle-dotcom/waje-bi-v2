from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from bi_agent.runtime.claim_authority import (
    AuthorityBundle,
    ClaimAuthorityNamespace,
    ClaimKey,
    ClaimPublicationCeiling,
    ClaimRevision,
    RECOMMENDATION_COMMITMENT_CONTRACT_VERSION,
    RecommendationCommitment,
    RecommendationProposal,
    RecommendationRecord,
    SemanticVerificationAttempt,
    SemanticVerificationDecision,
    SupportEdge,
)
from bi_agent.runtime.narrative_authority import (
    BlockLocalValidationReport,
    BlockVerificationAttempt,
    BlockVerifierReport,
    BlockVeto,
    NarrativeAuthorityContractError,
    NarrativeBlock,
    NarrativeDocument,
    NarrativeFactBinding,
    NarrativeWriterAttempt,
    PublicationFieldVisibilityPolicy,
    PublicClaimPalette,
    PublicFactDescriptor,
    PublicLimitation,
    PublicRecommendation,
    PublicRecommendationCommitment,
    RestrictedProviderResponse,
    SensitiveOutputFinding,
    _internal_fact_name_exposure_refs,
    _ranking_position_binding_gaps,
)
from bi_agent.runtime.evidence_authority import canonical_digest
from bi_agent.runtime.narrative_material_projection import (
    NarrativeMaterialProjection,
    ProjectedEvidenceFact,
    ProjectedEvidenceMaterial,
    ProjectedNarrativeClaim,
    _boundary_projection,
)


def test_public_fact_is_an_explicit_typed_descriptor_without_payload_path() -> None:
    _, claims, _, _, _ = _authority()
    policy = PublicationFieldVisibilityPolicy.fixed(
        policy_id="aggregate-answer",
        revision=1,
        restricted_output_policy_ref="test-policy:raw-identifiers",
        restricted_output_policy_version="1",
        restricted_output_fields=("order_id", "user_id"),
    )

    descriptor = PublicFactDescriptor.create(
        claim=claims[0],
        public_name="change_rate",
        fact_kind="number",
        value="0.125",
        range_end=None,
        unit="ratio",
        source_material_ref=claims[0].support_edge_refs[0],
    )

    assert "source_path" not in descriptor.to_dict()
    assert descriptor.claim_ref == claims[0].claim_ref
    assert descriptor.claim_digest == claims[0].content_digest
    assert policy.visible_fields

    with pytest.raises(
        NarrativeAuthorityContractError,
        match="public_fact_source_material_closure_invalid",
    ):
        PublicFactDescriptor.create(
            claim=claims[0],
            public_name="change_rate",
            fact_kind="number",
            value="0.125",
            range_end=None,
            unit="ratio",
            source_material_ref="raw-row:7",
        )


def test_multi_item_ranking_requires_each_typed_position_binding() -> None:
    contract = {
        "ranking_scope": "cross_dimension_diagnostic_priority",
        "ranking_measure": "diagnostic_priority_score",
        "ranking_order": "diagnostic_priority_score_descending",
        "ranking_position_measure": "priority_rank",
        "priority_rank_order": "ascending",
    }
    score_a = SimpleNamespace(fact_handle="f_score_a", name="diagnostic_priority_score")
    rank_a = SimpleNamespace(fact_handle="f_rank_a", name="priority_rank")
    score_b = SimpleNamespace(fact_handle="f_score_b", name="diagnostic_priority_score")
    rank_b = SimpleNamespace(fact_handle="f_rank_b", name="priority_rank")
    materials = {
        "m_a": SimpleNamespace(
            material_handle="m_a",
            interpretation_contract=contract,
            facts=(score_a, rank_a),
        ),
        "m_b": SimpleNamespace(
            material_handle="m_b",
            interpretation_contract=contract,
            facts=(score_b, rank_b),
        ),
    }
    claims = {
        "c_a": SimpleNamespace(material_handles=("m_a",)),
        "c_b": SimpleNamespace(material_handles=("m_b",)),
    }
    facts = {
        "f_score_a": ("m_a", score_a),
        "f_rank_a": ("m_a", rank_a),
        "f_score_b": ("m_b", score_b),
        "f_rank_b": ("m_b", rank_b),
    }
    score_only = SimpleNamespace(
        material_fact_bindings=(
            SimpleNamespace(claim_handle="c_a", fact_handle="f_score_a"),
            SimpleNamespace(claim_handle="c_b", fact_handle="f_score_b"),
        )
    )

    gaps = _ranking_position_binding_gaps(
        block=score_only,
        claims_by_handle=claims,
        materials_by_handle=materials,
        facts_by_handle=facts,
    )

    assert gaps == (
        ("c_a", "m_a", "f_rank_a"),
        ("c_b", "m_b", "f_rank_b"),
    )

    complete = SimpleNamespace(
        material_fact_bindings=(
            *score_only.material_fact_bindings,
            SimpleNamespace(claim_handle="c_a", fact_handle="f_rank_a"),
            SimpleNamespace(claim_handle="c_b", fact_handle="f_rank_b"),
        )
    )
    assert (
        _ranking_position_binding_gaps(
            block=complete,
            claims_by_handle=claims,
            materials_by_handle=materials,
            facts_by_handle=facts,
        )
        == ()
    )


def test_customer_prose_rejects_exact_machine_fact_names() -> None:
    materials = {
        "material": SimpleNamespace(
            facts=(
                SimpleNamespace(
                    name="change_rate",
                    projected_fact_ref="projected-fact:change-rate",
                ),
                SimpleNamespace(
                    name="direction",
                    projected_fact_ref="projected-fact:direction",
                ),
            )
        )
    }

    assert _internal_fact_name_exposure_refs(
        block=SimpleNamespace(text="付费金额增长，change_rate 为 12.5%。"),
        materials_by_handle=materials,
    ) == ("projected-fact:change-rate",)
    assert _internal_fact_name_exposure_refs(
        block=SimpleNamespace(text="付费金额增长 12.5%。"),
        materials_by_handle=materials,
    ) == ()


def test_provider_and_model_identity_are_sealed_in_typed_attempt_records() -> None:
    response = RestrictedProviderResponse.create(
        attempt_id="writer-attempt:typed",
        purpose="narrative_writer",
        provider_ref="provider:deepseek",
        model_ref="deepseek-chat",
        input_ref="writer-input:typed",
        input_digest="a" * 64,
        attempt_number=1,
        content='{"blocks": []}',
    )
    writer_attempt = NarrativeWriterAttempt.create(
        authority_bundle_ref="authority-bundle:test",
        material_projection_ref="narrative-material-projection:test",
        material_projection_digest="c" * 64,
        input_ref="writer-input:typed",
        input_digest="a" * 64,
        attempt_number=1,
        provider_response=response,
    )

    assert writer_attempt.attempt_id == response.attempt_id
    assert writer_attempt.provider_ref == "provider:deepseek"
    assert writer_attempt.model_ref == "deepseek-chat"
    assert writer_attempt.provider_response == response
    assert RestrictedProviderResponse.from_dict(response.to_dict()) == response

    with pytest.raises(
        NarrativeAuthorityContractError,
        match="narrative_writer_attempt_input_closure_invalid",
    ):
        NarrativeWriterAttempt.create(
            authority_bundle_ref="authority-bundle:test",
            material_projection_ref="narrative-material-projection:test",
            material_projection_digest="c" * 64,
            input_ref="writer-input:other",
            input_digest="a" * 64,
            attempt_number=1,
            provider_response=response,
        )

    with pytest.raises(
        NarrativeAuthorityContractError,
        match="restricted_provider_response_integrity_invalid",
    ):
        NarrativeWriterAttempt.create(
            authority_bundle_ref="authority-bundle:test",
            material_projection_ref="narrative-material-projection:test",
            material_projection_digest="c" * 64,
            input_ref="writer-input:typed",
            input_digest="a" * 64,
            attempt_number=1,
            provider_response=replace(response, content='{"blocks": ["tampered"]}'),
        )

    wrong_purpose = RestrictedProviderResponse.create(
        attempt_id="writer-attempt:wrong-purpose",
        purpose="block_verification",
        provider_ref="provider:deepseek",
        model_ref="deepseek-chat",
        input_ref="writer-input:wrong-purpose",
        input_digest="b" * 64,
        attempt_number=1,
        content='{"blocks": []}',
    )
    with pytest.raises(
        NarrativeAuthorityContractError,
        match="narrative_writer_attempt_provider_response_purpose_invalid",
    ):
        NarrativeWriterAttempt.create(
            authority_bundle_ref="authority-bundle:test",
            material_projection_ref="narrative-material-projection:test",
            material_projection_digest="c" * 64,
            input_ref=wrong_purpose.input_ref,
            input_digest=wrong_purpose.input_digest,
            attempt_number=1,
            provider_response=wrong_purpose,
        )


def _ceiling(claim_class: str) -> ClaimPublicationCeiling:
    strength = {
        "observed_fact": "directional",
        "dimension_localization": "dimension_localization",
    }[claim_class]
    return ClaimPublicationCeiling.create(
        claim_class=claim_class,
        strength=strength,
    )


def _namespace() -> ClaimAuthorityNamespace:
    return ClaimAuthorityNamespace.create(
        run_attempt_id="run-attempt:phase5",
        intent_revision_id="intent-revision:phase5",
        plan_revision_id="plan-revision:phase5",
    )


def _claim(
    *,
    suffix: str,
    claim_class: str,
    factual_payload: dict[str, object],
    limitation_refs: tuple[str, ...],
    authority_namespace: ClaimAuthorityNamespace,
) -> tuple[ClaimKey, SupportEdge, ClaimRevision]:
    key = ClaimKey.create(
        authority_namespace=authority_namespace,
        goal_id="goal:paid-amount",
        claim_kind=(
            "metric_change"
            if claim_class == "observed_fact"
            else "dimension_localization"
        ),
        subject=f"paid amount {suffix}",
        metric_ref="metric:paid_amount",
        target_window_ref="window:target",
        baseline_window_ref="window:baseline",
        scope="full_sample",
        grain="day",
        dimension_path=() if claim_class == "observed_fact" else ("device",),
    )
    edge = SupportEdge.create(
        authority_namespace=authority_namespace,
        kind="supports",
        source_type="evidence",
        source_ref=f"evidence:{suffix}",
        source_epistemic_class=claim_class,
        source_publication_ceiling=_ceiling(claim_class),
        target_claim_key=key.claim_key,
        limitation_refs=limitation_refs,
    )
    claim = ClaimRevision.create(
        authority_namespace=authority_namespace,
        claim_key=key,
        factual_payload=factual_payload,
        claim_class=claim_class,
        support_edges=(edge,),
        dependency_claim_refs=(),
        limitation_refs=limitation_refs,
        status="verified",
        publication_ceiling=_ceiling(claim_class),
    )
    return key, edge, claim


def _authority() -> tuple[
    AuthorityBundle,
    tuple[ClaimRevision, ...],
    tuple[ClaimKey, ...],
    tuple[PublicFactDescriptor, ...],
    tuple[PublicLimitation, ...],
]:
    namespace = _namespace()
    direction_key, direction_edge, direction_claim = _claim(
        suffix="direction",
        claim_class="observed_fact",
        factual_payload={
            "direction": "increase",
            "change_rate": "0.125",
            "target_date": "2026-07-17",
            "baseline_date": "2026-07-16",
            "scope": "full_sample",
            "owner_id": "internal-owner-42",
            "debug": "provider-trace-secret",
            "raw_rows": [{"player_id": "raw-user-7"}],
        },
        limitation_refs=("limitation:partial-day",),
        authority_namespace=namespace,
    )
    device_key, device_edge, device_claim = _claim(
        suffix="device",
        claim_class="dimension_localization",
        factual_payload={
            "device": "Android",
            "share_delta": "-0.08",
            "window_start": "2026-07-16",
            "window_end": "2026-07-17",
            "scope": "paying_users",
            "debug": "hidden-dimension-diagnostic",
        },
        limitation_refs=("limitation:device-coverage",),
        authority_namespace=namespace,
    )
    claims = (direction_claim, device_claim)
    namespace_token = namespace.authority_namespace_ref.removeprefix(
        "claim-authority-namespace:sha256:"
    )[:24]
    manifest = {
        "authority_namespace_ref": namespace.authority_namespace_ref,
        "bundle_revision": 1,
        "supersedes_bundle_ref": None,
        "run_attempt_id": namespace.run_attempt_id,
        "intent_revision_id": "intent-revision:phase5",
        "decision_refs": ("decision:baseline",),
        "plan_revision_id": "plan-revision:phase5",
        "authority_context_ref": "authority-context:phase5",
        "execution_result_ref": "authoritative-execution-result:phase5",
        "execution_result_digest": "e" * 64,
        "claim_settlement_ref": f"claim-settlement:{namespace_token}:sha256:{'c' * 64}",
        "claim_settlement_digest": "c" * 64,
        "claim_graph_ref": f"claim-graph:{namespace_token}:sha256:{'d' * 64}",
        "claim_graph_digest": "d" * 64,
        "authority_mode": "claim_bearing",
        "required_obligation_ids": (),
        "obligation_coverage_refs": (),
        "evidence_refs": tuple(
            sorted((direction_edge.source_ref, device_edge.source_ref))
        ),
        "verified_claim_refs": tuple(sorted(claim.claim_ref for claim in claims)),
        "recommendation_refs": (),
        "assumption_refs": (),
        "limitation_refs": (
            "limitation:device-coverage",
            "limitation:partial-day",
        ),
        "claim_verifier_report_ref": (
            f"claim-verifier-report:{namespace_token}:sha256:{'v' * 64}"
        ),
    }
    bundle_digest = canonical_digest(
        {
            key: value
            for key, value in manifest.items()
            if key != "authority_namespace_ref"
        }
    )
    bundle = AuthorityBundle(
        bundle_ref=(f"authority-bundle:{namespace_token}:sha256:{bundle_digest}"),
        bundle_digest=bundle_digest,
        seal_state="sealed",
        sealed_at="2026-07-18T10:00:00Z",
        content_digest=bundle_digest,
        **manifest,
    )
    facts = tuple(
        PublicFactDescriptor.create(
            claim=claim,
            public_name=name,
            fact_kind=kind,
            value=value,
            range_end=range_end,
            unit=unit,
            source_material_ref=claim.support_edge_refs[0],
        )
        for claim, name, kind, value, range_end, unit in (
            (
                direction_claim,
                "direction",
                "label",
                "increase",
                None,
                None,
            ),
            (
                direction_claim,
                "change_rate",
                "number",
                "0.125",
                None,
                "ratio",
            ),
            (
                direction_claim,
                "target_date",
                "date",
                "2026-07-17",
                None,
                None,
            ),
            (
                direction_claim,
                "baseline_date",
                "date",
                "2026-07-16",
                None,
                None,
            ),
            (
                direction_claim,
                "analysis_scope",
                "scope",
                "full_sample",
                None,
                None,
            ),
            (device_claim, "device", "label", "Android", None, None),
            (
                device_claim,
                "share_delta",
                "number",
                "-0.08",
                None,
                "ratio",
            ),
            (
                device_claim,
                "comparison_window",
                "date_range",
                "2026-07-16",
                "2026-07-17",
                None,
            ),
            (
                device_claim,
                "analysis_scope",
                "scope",
                "paying_users",
                None,
                None,
            ),
        )
    )
    limitations = (
        PublicLimitation.create(
            limitation_ref="limitation:partial-day",
            public_context={
                "claims": ({"claim_class": "observed_fact"},),
            },
        ),
        PublicLimitation.create(
            limitation_ref="limitation:device-coverage",
            public_context={
                "claims": ({"claim_class": "dimension_localization"},),
            },
        ),
    )
    return bundle, claims, (direction_key, device_key), facts, limitations


def _reseal_bundle(
    bundle: AuthorityBundle,
    **overrides: object,
) -> AuthorityBundle:
    manifest = {
        "bundle_revision": bundle.bundle_revision,
        "supersedes_bundle_ref": bundle.supersedes_bundle_ref,
        "run_attempt_id": bundle.run_attempt_id,
        "intent_revision_id": bundle.intent_revision_id,
        "decision_refs": bundle.decision_refs,
        "plan_revision_id": bundle.plan_revision_id,
        "authority_context_ref": bundle.authority_context_ref,
        "execution_result_ref": bundle.execution_result_ref,
        "execution_result_digest": bundle.execution_result_digest,
        "claim_settlement_ref": bundle.claim_settlement_ref,
        "claim_settlement_digest": bundle.claim_settlement_digest,
        "claim_graph_ref": bundle.claim_graph_ref,
        "claim_graph_digest": bundle.claim_graph_digest,
        "authority_mode": bundle.authority_mode,
        "required_obligation_ids": bundle.required_obligation_ids,
        "obligation_coverage_refs": bundle.obligation_coverage_refs,
        "evidence_refs": bundle.evidence_refs,
        "verified_claim_refs": bundle.verified_claim_refs,
        "recommendation_refs": bundle.recommendation_refs,
        "assumption_refs": bundle.assumption_refs,
        "limitation_refs": bundle.limitation_refs,
        "claim_verifier_report_ref": bundle.claim_verifier_report_ref,
    }
    manifest.update(overrides)
    digest = canonical_digest(manifest)
    namespace_token = bundle.authority_namespace_ref.removeprefix(
        "claim-authority-namespace:sha256:"
    )[:24]
    return AuthorityBundle(
        bundle_ref=f"authority-bundle:{namespace_token}:sha256:{digest}",
        authority_namespace_ref=bundle.authority_namespace_ref,
        bundle_digest=digest,
        seal_state="sealed",
        sealed_at=bundle.sealed_at,
        content_digest=digest,
        **manifest,
    )


def _recommendation(
    bundle: AuthorityBundle,
    supporting_claim: ClaimRevision,
) -> RecommendationRecord:
    namespace = _namespace()
    action = "Prioritize an Android payment-funnel review."
    expected_value = "Focus investigation effort on the largest segment."
    commitments = tuple(
        sorted(
            (
                RecommendationCommitment.create(
                    authority_namespace=namespace,
                    commitment_kind="action",
                    text=action,
                    supporting_claim_refs=(supporting_claim.claim_ref,),
                    diagnostic_mode=None,
                    action_domain="analysis",
                    action_stage="investigate",
                    expected_value_kind=None,
                    expected_value_mode=None,
                ),
                RecommendationCommitment.create(
                    authority_namespace=namespace,
                    commitment_kind="expected_outcome",
                    text=expected_value,
                    supporting_claim_refs=(supporting_claim.claim_ref,),
                    diagnostic_mode=None,
                    action_domain=None,
                    action_stage=None,
                    expected_value_kind="information_gain",
                    expected_value_mode="expected_effect",
                ),
            ),
            key=lambda item: item.recommendation_commitment_ref,
        )
    )
    proposal_body = {
        "claim_graph_ref": bundle.claim_graph_ref,
        "claim_graph_digest": bundle.claim_graph_digest,
        "commitment_contract_version": (RECOMMENDATION_COMMITMENT_CONTRACT_VERSION),
        "recommendation_commitment_refs": tuple(
            item.recommendation_commitment_ref for item in commitments
        ),
        "commitments": commitments,
        "supporting_claim_refs": (supporting_claim.claim_ref,),
        "assumption_refs": (),
        "risk_refs": ("limitation:partial-day",),
        "action": action,
        "applicable_conditions": ("The observed increase remains directional.",),
        "expected_decision_value": expected_value,
    }
    proposal_digest = canonical_digest(proposal_body)
    namespace_token = namespace.authority_namespace_ref.removeprefix(
        "claim-authority-namespace:sha256:"
    )[:24]
    proposal = RecommendationProposal(
        recommendation_proposal_ref=(
            f"recommendation-proposal:{namespace_token}:sha256:{proposal_digest}"
        ),
        authority_namespace_ref=namespace.authority_namespace_ref,
        content_digest=proposal_digest,
        **proposal_body,
    )
    attempt = SemanticVerificationAttempt.create(
        authority_namespace=namespace,
        purpose="recommendation",
        authority_input_ref=bundle.claim_graph_ref,
        authority_input_digest=bundle.claim_graph_digest,
        subject_refs=(proposal.recommendation_proposal_ref,),
        provider_ref="provider:openai",
        model_ref="gpt-5",
        input_digest="a" * 64,
        attempt_number=1,
        raw_provider_response_ref="restricted-provider-response:recommendation",
        raw_provider_response_digest="b" * 64,
    )
    decision = SemanticVerificationDecision.create(
        authority_namespace=namespace,
        verification_attempt=attempt,
        subject_ref=proposal.recommendation_proposal_ref,
        disposition="accepted",
        veto_basis=None,
        reason_code=None,
        limitation_refs=(),
    )
    record_body = {
        "recommendation_proposal_ref": proposal.recommendation_proposal_ref,
        "claim_graph_ref": bundle.claim_graph_ref,
        "claim_graph_digest": bundle.claim_graph_digest,
        "commitment_contract_version": proposal.commitment_contract_version,
        "recommendation_commitment_refs": proposal.recommendation_commitment_refs,
        "commitments": proposal.commitments,
        "supporting_claim_refs": proposal.supporting_claim_refs,
        "assumption_refs": proposal.assumption_refs,
        "risk_refs": proposal.risk_refs,
        "action": proposal.action,
        "applicable_conditions": proposal.applicable_conditions,
        "expected_decision_value": proposal.expected_decision_value,
        "claim_verifier_report_ref": bundle.claim_verifier_report_ref,
        "verification_attempt_ref": attempt.verification_attempt_ref,
        "verification_decision_ref": decision.verification_decision_ref,
        "proposal": proposal,
        "verification_attempt": attempt,
        "verification_decision": decision,
    }
    record_digest = canonical_digest(record_body)
    return RecommendationRecord(
        recommendation_ref=(f"recommendation:{namespace_token}:sha256:{record_digest}"),
        authority_namespace_ref=namespace.authority_namespace_ref,
        content_digest=record_digest,
        **record_body,
    )


def _policy() -> PublicationFieldVisibilityPolicy:
    return PublicationFieldVisibilityPolicy.fixed(
        policy_id="aggregate-answer",
        revision=1,
        restricted_output_policy_ref="test-policy:raw-identifiers",
        restricted_output_policy_version="1",
        restricted_output_fields=("order_id", "user_id"),
    )


def _palette() -> PublicClaimPalette:
    bundle, claims, claim_keys, facts, limitations = _authority()
    return PublicClaimPalette.derive(
        authority_bundle=bundle,
        claims=claims,
        claim_keys=claim_keys,
        recommendations=(),
        public_facts=facts,
        public_limitations=limitations,
        visibility_policy=_policy(),
    )


def _palette_with_recommendation() -> tuple[
    PublicClaimPalette,
    PublicRecommendation,
]:
    bundle, claims, claim_keys, facts, limitations = _authority()
    recommendation = _recommendation(bundle, claims[1])
    bundle = _reseal_bundle(
        bundle,
        recommendation_refs=(recommendation.recommendation_ref,),
    )
    palette = PublicClaimPalette.derive(
        authority_bundle=bundle,
        claims=claims,
        claim_keys=claim_keys,
        recommendations=(recommendation,),
        public_facts=facts,
        public_limitations=limitations,
        visibility_policy=_policy(),
    )
    return palette, palette.recommendations[0]


def _material_projection(palette: PublicClaimPalette) -> NarrativeMaterialProjection:
    grouped_facts: dict[str, list[PublicFactDescriptor]] = {}
    for public_claim in palette.claims:
        for fact in public_claim.facts:
            grouped_facts.setdefault(fact.source_material_ref, []).append(fact)

    evidence_ref_by_edge: dict[str, str] = {}
    materials = []
    for source_material_ref in sorted(grouped_facts):
        source_facts = grouped_facts[source_material_ref]
        evidence_entry_ref = "test-evidence-entry:sha256:" + canonical_digest(
            source_material_ref
        )
        evidence_ref_by_edge[source_material_ref] = evidence_entry_ref
        facts_by_name: dict[str, list[PublicFactDescriptor]] = {}
        for fact in source_facts:
            facts_by_name.setdefault(fact.public_name, []).append(fact)
        projected_facts = []
        for name in sorted(facts_by_name):
            public_facts = facts_by_name[name]
            signatures = {
                (item.fact_kind, item.value, item.range_end, item.unit)
                for item in public_facts
            }
            if len(signatures) != 1:
                raise AssertionError("conflicting test projection fact")
            fact_kind, value, range_end, unit = next(iter(signatures))
            projected_facts.append(
                ProjectedEvidenceFact.create(
                    evidence_entry_ref=evidence_entry_ref,
                    source_fact_refs=tuple(item.fact_ref for item in public_facts),
                    name=name,
                    fact_kind=fact_kind,
                    value=value,
                    range_end=range_end,
                    unit=unit,
                )
            )
        source_claim = next(
            item
            for item in palette.claims
            if any(
                fact.source_material_ref == source_material_ref for fact in item.facts
            )
        )
        material_body = {
            "evidence_entry_ref": evidence_entry_ref,
            "evidence_entry_digest": canonical_digest(
                {"evidence_entry_ref": evidence_entry_ref}
            ),
            "evidence_edge_refs": (source_material_ref,),
            "evidence_kind": "accepted_test_evidence",
            "evidence_strength": source_claim.publication_ceiling.strength,
            "maximum_claim_strength": source_claim.publication_ceiling.strength,
            "scope": source_claim.scope,
            "dimension_path": source_claim.dimension_path,
            "facts": tuple(projected_facts),
        }
        material_digest = canonical_digest(material_body)
        materials.append(
            ProjectedEvidenceMaterial(
                evidence_material_ref=(
                    "narrative-evidence-material:sha256:" + material_digest
                ),
                material_handle=("m_" + canonical_digest(evidence_entry_ref)[:20]),
                content_digest=material_digest,
                **material_body,
            )
        )
    material_handle_by_evidence_ref = {
        item.evidence_entry_ref: item.material_handle for item in materials
    }
    projected_claims = tuple(
        ProjectedNarrativeClaim.create(
            public_claim=public_claim,
            evidence_entry_refs=tuple(
                sorted(
                    {
                        evidence_ref_by_edge[fact.source_material_ref]
                        for fact in public_claim.facts
                    }
                )
            ),
            material_handle_by_evidence_ref=material_handle_by_evidence_ref,
        )
        for public_claim in palette.claims
    )
    projected_limitations, boundary_facets = _boundary_projection(palette.limitations)
    projection_body = {
        "palette_ref": palette.palette_ref,
        "palette_digest": palette.content_digest,
        "claim_settlement_ref": "claim-settlement:test-projection",
        "claim_settlement_digest": canonical_digest("test-projection"),
        "authority_mode": palette.authority_mode,
        "claims": projected_claims,
        "publication_requirements": (),
        "evidence_materials": tuple(materials),
        "recommendations": palette.recommendations,
        "limitations": projected_limitations,
        "boundary_facets": boundary_facets,
    }
    projection_digest = canonical_digest(projection_body)
    projection = NarrativeMaterialProjection(
        projection_ref=("narrative-material-projection:sha256:" + projection_digest),
        content_digest=projection_digest,
        **projection_body,
    )
    projection.assert_integrity()
    return projection


def _projection_with_shared_first_material(
    palette: PublicClaimPalette,
) -> NarrativeMaterialProjection:
    projection = _material_projection(palette)
    first_material = projection.evidence_materials[0]
    target = next(
        item
        for item in projection.claims
        if first_material.material_handle not in set(item.material_handles)
    )
    target_body = {
        "claim_ref": target.claim_ref,
        "claim_digest": target.claim_digest,
        "claim_handle": target.claim_handle,
        "claim_class": target.claim_class,
        "publication_ceiling": target.publication_ceiling,
        "subject": target.subject,
        "scope": target.scope,
        "grain": target.grain,
        "dimension_path": target.dimension_path,
        "evidence_entry_refs": (first_material.evidence_entry_ref,),
        "material_handles": (first_material.material_handle,),
        "limitation_handles": target.limitation_handles,
    }
    target_digest = canonical_digest(target_body)
    shared_target = ProjectedNarrativeClaim(
        projected_claim_ref="narrative-projected-claim:sha256:" + target_digest,
        content_digest=target_digest,
        **target_body,
    )
    claims = tuple(
        shared_target if item.claim_handle == target.claim_handle else item
        for item in projection.claims
    )
    projection_body = {
        "palette_ref": projection.palette_ref,
        "palette_digest": projection.palette_digest,
        "claim_settlement_ref": projection.claim_settlement_ref,
        "claim_settlement_digest": projection.claim_settlement_digest,
        "authority_mode": projection.authority_mode,
        "claims": claims,
        "publication_requirements": projection.publication_requirements,
        "evidence_materials": projection.evidence_materials,
        "recommendations": projection.recommendations,
        "limitations": projection.limitations,
        "boundary_facets": projection.boundary_facets,
    }
    projection_digest = canonical_digest(projection_body)
    shared = NarrativeMaterialProjection(
        projection_ref="narrative-material-projection:sha256:" + projection_digest,
        content_digest=projection_digest,
        **projection_body,
    )
    shared.assert_integrity()
    return shared


def _writer_attempt(
    palette: PublicClaimPalette,
    attempt_id: str,
    *,
    material_projection: NarrativeMaterialProjection | None = None,
) -> NarrativeWriterAttempt:
    projection = material_projection or _material_projection(palette)
    input_ref = f"writer-input:{attempt_id}"
    input_digest = canonical_digest(
        {
            "authority_bundle_ref": palette.authority_bundle_ref,
            "material_projection_ref": projection.projection_ref,
            "material_projection_digest": projection.content_digest,
            "input_ref": input_ref,
        }
    )
    response = RestrictedProviderResponse.create(
        attempt_id=attempt_id,
        purpose="narrative_writer",
        provider_ref="provider:deepseek",
        model_ref="deepseek-v3.2",
        input_ref=input_ref,
        input_digest=input_digest,
        attempt_number=1,
        content=json.dumps({"attempt_id": attempt_id, "blocks": []}),
    )
    return NarrativeWriterAttempt.create(
        authority_bundle_ref=palette.authority_bundle_ref,
        material_projection_ref=projection.projection_ref,
        material_projection_digest=projection.content_digest,
        input_ref=input_ref,
        input_digest=input_digest,
        attempt_number=1,
        provider_response=response,
    )


def _verification_attempt(
    narrative: NarrativeDocument,
    local_report: BlockLocalValidationReport,
    attempt_id: str,
) -> BlockVerificationAttempt:
    input_ref = f"verifier-input:{attempt_id}"
    input_digest = canonical_digest(
        {
            "narrative_id": narrative.narrative_id,
            "local_report_ref": local_report.local_report_ref,
            "input_ref": input_ref,
        }
    )
    response = RestrictedProviderResponse.create(
        attempt_id=attempt_id,
        purpose="block_verification",
        provider_ref="provider:openai",
        model_ref="gpt-5",
        input_ref=input_ref,
        input_digest=input_digest,
        attempt_number=1,
        content=json.dumps({"attempt_id": attempt_id, "vetoes": []}),
    )
    return BlockVerificationAttempt.create(
        narrative=narrative,
        local_report=local_report,
        input_ref=input_ref,
        input_digest=input_digest,
        attempt_number=1,
        provider_response=response,
    )


def _fact(
    palette: PublicClaimPalette,
    source_path: tuple[str, ...],
) -> tuple[str, PublicFactDescriptor]:
    expected_name = {
        ("direction",): "direction",
        ("change_rate",): "change_rate",
        ("target_date",): "target_date",
        ("baseline_date",): "baseline_date",
        ("scope",): "analysis_scope",
        ("device",): "device",
        ("share_delta",): "share_delta",
        ("window_start",): "comparison_window",
    }[source_path]
    matches = []
    for public_claim in palette.claims:
        for fact in public_claim.facts:
            if fact.public_name == expected_name:
                matches.append((public_claim.claim_handle, fact))
    if source_path == ("scope",):
        return next(item for item in matches if item[1].value == "full_sample")
    if len(matches) == 1:
        return matches[0]
    raise AssertionError(f"missing fact: {source_path}")


def _binding(
    claim_handle: str,
    fact: PublicFactDescriptor,
    material_projection: NarrativeMaterialProjection,
    *,
    value: str | None = None,
    range_end: str | None = None,
) -> NarrativeFactBinding:
    projected_claim = next(
        item for item in material_projection.claims if item.claim_handle == claim_handle
    )
    projected_fact = next(
        candidate
        for material in material_projection.evidence_materials
        if material.material_handle in set(projected_claim.material_handles)
        for candidate in material.facts
        if (
            candidate.name == fact.public_name
            and candidate.fact_kind == fact.fact_kind
            and candidate.value == fact.value
            and candidate.range_end == fact.range_end
            and candidate.unit == fact.unit
        )
    )
    return NarrativeFactBinding.create(
        claim_handle=claim_handle,
        fact_handle=projected_fact.fact_handle,
        fact_kind=fact.fact_kind,
        value=fact.value if value is None else value,
        range_end=fact.range_end if range_end is None else range_end,
        unit=fact.unit,
    )


def _block(
    palette: PublicClaimPalette,
    *,
    writer_attempt_id: str,
    source_path: tuple[str, ...],
    text: str,
    role: str = "direction",
    required: bool = True,
    binding_value: str | None = None,
    binding_range_end: str | None = None,
) -> NarrativeBlock:
    material_projection = _material_projection(palette)
    claim_handle, fact = _fact(palette, source_path)
    public_claim = next(
        item for item in palette.claims if item.claim_handle == claim_handle
    )
    return NarrativeBlock.create(
        writer_attempt_id=writer_attempt_id,
        role=role,
        text=text,
        claim_handles=(claim_handle,),
        recommendation_handles=(),
        limitation_handles=public_claim.limitation_handles,
        material_fact_bindings=(
            _binding(
                claim_handle,
                fact,
                material_projection,
                value=binding_value,
                range_end=binding_range_end,
            ),
        ),
        statement_role="business_finding",
        required=required,
    )


def test_writer_payload_comes_from_material_projection() -> None:
    bundle, claims, claim_keys, facts, limitations = _authority()
    palette = PublicClaimPalette.derive(
        authority_bundle=bundle,
        claims=claims,
        claim_keys=claim_keys,
        recommendations=(),
        public_facts=facts,
        public_limitations=limitations,
        visibility_policy=_policy(),
    )

    projection = _material_projection(palette)
    writer_payload = projection.to_writer_payload()
    rendered = json.dumps(writer_payload, ensure_ascii=False, sort_keys=True)

    assert palette.authority_bundle_ref == bundle.bundle_ref
    assert projection.palette_ref == palette.palette_ref
    assert projection.palette_digest == palette.content_digest
    assert not hasattr(palette, "to_writer_payload")
    assert not hasattr(facts[0], "to_writer_payload")
    assert {item.claim_ref for item in palette.claims} == set(
        bundle.verified_claim_refs
    )
    assert set(writer_payload) == {
        "authority_mode",
        "claims",
        "publication_requirements",
        "evidence_materials",
        "recommendations",
        "limitations",
        "boundary_facets",
    }
    assert writer_payload["publication_requirements"] == []
    assert "0.125" in rendered
    assert "change_rate" in rendered
    assert "paid amount direction" in rendered
    assert "2026-07-17" in rendered
    assert "full_sample" in rendered
    assert "limitation:partial-day" not in rendered
    assert claims[0].claim_ref not in rendered
    assert "internal-owner-42" not in rendered
    assert "provider-trace-secret" not in rendered
    assert "raw-user-7" not in rendered
    assert "raw_rows" not in rendered
    assert "owner_id" not in rendered
    assert "debug" not in rendered
    assert (
        PublicClaimPalette.from_dict(
            palette.to_dict(),
            authority_bundle=bundle,
            claims=claims,
            claim_keys=claim_keys,
            recommendations=(),
            visibility_policy=_policy(),
        )
        == palette
    )
    tampered_palette = palette.to_dict()
    tampered_palette["required_obligation_ids"] = ["claim-obligation:tampered"]
    with pytest.raises(
        NarrativeAuthorityContractError,
        match="public_palette_bundle_closure_invalid",
    ):
        PublicClaimPalette.from_dict(
            tampered_palette,
            authority_bundle=bundle,
            claims=claims,
            claim_keys=claim_keys,
            recommendations=(),
            visibility_policy=_policy(),
        )


def test_palette_is_order_normalized_and_limitations_remain_claim_local() -> None:
    bundle, claims, claim_keys, facts, limitations = _authority()
    first = PublicClaimPalette.derive(
        authority_bundle=bundle,
        claims=claims,
        claim_keys=claim_keys,
        recommendations=(),
        public_facts=facts,
        public_limitations=limitations,
        visibility_policy=_policy(),
    )
    reordered = PublicClaimPalette.derive(
        authority_bundle=bundle,
        claims=tuple(reversed(claims)),
        claim_keys=tuple(reversed(claim_keys)),
        recommendations=(),
        public_facts=tuple(reversed(facts)),
        public_limitations=tuple(reversed(limitations)),
        visibility_policy=_policy(),
    )

    assert reordered == first
    assert reordered.palette_ref == first.palette_ref
    assert reordered.content_digest == first.content_digest
    limitation_sets = {
        claim.claim_ref: set(claim.limitation_refs) for claim in first.claims
    }
    assert limitation_sets == {
        claims[0].claim_ref: {"limitation:partial-day"},
        claims[1].claim_ref: {"limitation:device-coverage"},
    }


def test_palette_requires_exact_bundle_claim_and_limitation_closure() -> None:
    bundle, claims, claim_keys, facts, limitations = _authority()

    with pytest.raises(
        NarrativeAuthorityContractError,
        match="public_palette_claim_closure_invalid",
    ):
        PublicClaimPalette.derive(
            authority_bundle=bundle,
            claims=claims[:1],
            claim_keys=claim_keys[:1],
            recommendations=(),
            public_facts=tuple(
                fact for fact in facts if fact.claim_ref == claims[0].claim_ref
            ),
            public_limitations=limitations[:1],
            visibility_policy=_policy(),
        )

    with pytest.raises(
        NarrativeAuthorityContractError,
        match="public_palette_limitation_closure_invalid",
    ):
        PublicClaimPalette.derive(
            authority_bundle=bundle,
            claims=claims,
            claim_keys=claim_keys,
            recommendations=(),
            public_facts=facts,
            public_limitations=limitations[:1],
            visibility_policy=_policy(),
        )


def test_verified_recommendation_has_its_own_public_handle_and_projection() -> None:
    bundle, claims, claim_keys, facts, limitations = _authority()
    recommendation = _recommendation(bundle, claims[0])
    bundle = _reseal_bundle(
        bundle,
        recommendation_refs=(recommendation.recommendation_ref,),
    )
    palette = PublicClaimPalette.derive(
        authority_bundle=bundle,
        claims=claims,
        claim_keys=claim_keys,
        recommendations=(recommendation,),
        public_facts=facts,
        public_limitations=limitations,
        visibility_policy=_policy(),
    )
    public_recommendation = palette.recommendations[0]
    writer_recommendation = _material_projection(palette).to_writer_payload()[
        "recommendations"
    ][0]

    assert type(public_recommendation) is PublicRecommendation
    assert public_recommendation.recommendation_ref == recommendation.recommendation_ref
    assert public_recommendation.commitment_contract_version == (
        RECOMMENDATION_COMMITMENT_CONTRACT_VERSION
    )
    assert all(
        type(item) is PublicRecommendationCommitment
        for item in public_recommendation.commitments
    )
    assert writer_recommendation["action"] == recommendation.action
    assert writer_recommendation["risk_handles"] == [limitations[0].limitation_handle]
    assert writer_recommendation["applicable_conditions"] == list(
        recommendation.applicable_conditions
    )
    assert "risk_refs" not in writer_recommendation
    assert "assumption_refs" not in writer_recommendation
    assert writer_recommendation["commitment_contract_version"] == (
        RECOMMENDATION_COMMITMENT_CONTRACT_VERSION
    )
    assert {
        item["commitment_kind"] for item in writer_recommendation["commitments"]
    } == {"action", "expected_outcome"}
    assert all(
        item["supporting_claim_handles"]
        == list(public_recommendation.supporting_claim_handles)
        for item in writer_recommendation["commitments"]
    )
    serialized_writer_recommendation = json.dumps(
        writer_recommendation, ensure_ascii=False, sort_keys=True
    )
    supporting_claim_ref = claims[0].claim_ref
    assert supporting_claim_ref not in serialized_writer_recommendation
    assert all(
        commitment.recommendation_commitment_ref not in serialized_writer_recommendation
        for commitment in recommendation.commitments
    )
    assert "supporting_claim_refs" not in serialized_writer_recommendation
    assert "recommendation_commitment_ref" not in serialized_writer_recommendation
    assert (
        PublicClaimPalette.from_dict(
            palette.to_dict(),
            authority_bundle=bundle,
            claims=claims,
            claim_keys=claim_keys,
            recommendations=(recommendation,),
            visibility_policy=_policy(),
        )
        == palette
    )
    tampered_palette = palette.to_dict()
    tampered_palette["recommendations"][0]["commitments"][0][
        "supporting_claim_handles"
    ] = ["c_forged"]
    with pytest.raises(
        NarrativeAuthorityContractError,
        match="public_recommendation_commitment_integrity_invalid|public_recommendation_commitments_invalid",
    ):
        PublicClaimPalette.from_dict(
            tampered_palette,
            authority_bundle=bundle,
            claims=claims,
            claim_keys=claim_keys,
            recommendations=(recommendation,),
            visibility_policy=_policy(),
        )

    attempt_id = "writer-attempt:recommendation"
    accepted = NarrativeBlock.create(
        writer_attempt_id=attempt_id,
        role="next_action",
        text="Review the Android payment funnel while the signal remains directional.",
        claim_handles=(),
        recommendation_handles=(public_recommendation.recommendation_handle,),
        limitation_handles=(),
        material_fact_bindings=(),
        statement_role="recommendation",
        required=True,
    )
    document = NarrativeDocument.create(
        authority_bundle_ref=bundle.bundle_ref,
        material_projection_ref=_material_projection(palette).projection_ref,
        material_projection_digest=_material_projection(palette).content_digest,
        writer_attempt=_writer_attempt(palette, attempt_id),
        parent_narrative_id=None,
        blocks=(accepted,),
    )
    local = BlockLocalValidationReport.validate(
        narrative=document,
        material_projection=_material_projection(palette),
        visibility_policy=_policy(),
        sensitive_output_findings=(),
    )
    assert local.accepted_block_ids == (accepted.block_id,)
    assert local.issues == ()
    verifier = BlockVerifierReport.create(
        narrative=document,
        material_projection=_material_projection(palette),
        visibility_policy=_policy(),
        local_report=local,
        verification_attempt=_verification_attempt(
            document,
            local,
            "verifier-attempt:recommendation",
        ),
        accepted_block_ids=(accepted.block_id,),
        vetoes=(),
    )
    assert verifier.accepted_block_ids == (accepted.block_id,)

    with pytest.raises(
        NarrativeAuthorityContractError,
        match="public_recommendation_integrity_invalid",
    ):
        PublicClaimPalette.derive(
            authority_bundle=bundle,
            claims=claims,
            claim_keys=claim_keys,
            recommendations=(replace(recommendation, action="Tampered action."),),
            public_facts=facts,
            public_limitations=limitations,
            visibility_policy=_policy(),
        )


def test_claim_bearing_boundary_may_summarize_palette_wide_limitations() -> None:
    palette = _palette()
    block = NarrativeBlock.create(
        writer_attempt_id="writer-attempt:claim-bearing-boundary",
        role="boundary",
        text="The answer is bounded by partial-day and device-coverage limits.",
        claim_handles=(),
        recommendation_handles=(),
        limitation_handles=tuple(
            limitation.limitation_handle for limitation in palette.limitations
        ),
        material_fact_bindings=(),
        statement_role="boundary",
        required=True,
    )
    document = NarrativeDocument.create(
        authority_bundle_ref=palette.authority_bundle_ref,
        material_projection_ref=_material_projection(palette).projection_ref,
        material_projection_digest=_material_projection(palette).content_digest,
        writer_attempt=_writer_attempt(
            palette,
            "writer-attempt:claim-bearing-boundary",
        ),
        parent_narrative_id=None,
        blocks=(block,),
    )

    report = BlockLocalValidationReport.validate(
        narrative=document,
        material_projection=_material_projection(palette),
        visibility_policy=_policy(),
        sensitive_output_findings=(),
    )

    assert report.accepted_block_ids == (block.block_id,)
    assert report.rejected_block_ids == ()
    assert report.issues == ()


@pytest.mark.parametrize(
    "role",
    (
        "executive_answer",
        "direction",
        "accounting_drivers",
        "dimension_localization",
        "contextual_pattern",
        "next_action",
    ),
)
def test_every_non_boundary_role_may_be_recommendation_only_and_bind_its_risks(
    role: str,
) -> None:
    palette, public_recommendation = _palette_with_recommendation()
    block = NarrativeBlock.create(
        writer_attempt_id="writer-attempt:recommendation-risk",
        role=role,
        text="Prioritize the verified action while carrying its explicit risk.",
        claim_handles=(),
        recommendation_handles=(public_recommendation.recommendation_handle,),
        limitation_handles=public_recommendation.risk_handles,
        material_fact_bindings=(),
        statement_role="recommendation",
        required=False,
    )
    document = NarrativeDocument.create(
        authority_bundle_ref=palette.authority_bundle_ref,
        material_projection_ref=_material_projection(palette).projection_ref,
        material_projection_digest=_material_projection(palette).content_digest,
        writer_attempt=_writer_attempt(palette, "writer-attempt:recommendation-risk"),
        parent_narrative_id=None,
        blocks=(block,),
    )

    report = BlockLocalValidationReport.validate(
        narrative=document,
        material_projection=_material_projection(palette),
        visibility_policy=_policy(),
        sensitive_output_findings=(),
    )

    assert report.accepted_block_ids == (block.block_id,)
    assert report.issues == ()


def test_recommendation_only_block_roundtrips_and_replay_rejects_tampering() -> None:
    palette, public_recommendation = _palette_with_recommendation()
    block = NarrativeBlock.create(
        writer_attempt_id="writer-attempt:recommendation-replay",
        role="direction",
        text="Prioritize the verified action while carrying its explicit risk.",
        claim_handles=(),
        recommendation_handles=(public_recommendation.recommendation_handle,),
        limitation_handles=public_recommendation.risk_handles,
        material_fact_bindings=(),
        statement_role="recommendation",
        required=False,
    )
    assert NarrativeBlock.from_dict(block.to_dict()) == block

    document = NarrativeDocument.create(
        authority_bundle_ref=palette.authority_bundle_ref,
        material_projection_ref=_material_projection(palette).projection_ref,
        material_projection_digest=_material_projection(palette).content_digest,
        writer_attempt=_writer_attempt(
            palette,
            "writer-attempt:recommendation-replay",
        ),
        parent_narrative_id=None,
        blocks=(block,),
    )
    assert NarrativeDocument.from_dict(document.to_dict()) == document

    tampered = document.to_dict()
    tampered["blocks"][0]["recommendation_handles"] = []
    with pytest.raises(
        NarrativeAuthorityContractError,
        match="narrative_block_authority_handles_invalid",
    ):
        NarrativeDocument.from_dict(tampered)


@pytest.mark.parametrize("limitation_handles", ((), ("limitation-handle:test",)))
def test_non_boundary_block_requires_claim_or_recommendation(
    limitation_handles: tuple[str, ...],
) -> None:
    with pytest.raises(
        NarrativeAuthorityContractError,
        match="narrative_block_authority_handles_invalid",
    ):
        NarrativeBlock.create(
            writer_attempt_id="writer-attempt:unbound-direction",
            role="direction",
            text="This prose has no governing claim or recommendation.",
            claim_handles=(),
            recommendation_handles=(),
            limitation_handles=limitation_handles,
            material_fact_bindings=(),
            statement_role="business_finding",
            required=False,
        )


def test_boundary_requires_limitation_even_when_it_binds_a_claim() -> None:
    palette = _palette()

    with pytest.raises(
        NarrativeAuthorityContractError,
        match="narrative_block_authority_handles_invalid",
    ):
        NarrativeBlock.create(
            writer_attempt_id="writer-attempt:unbounded-boundary",
            role="boundary",
            text="A boundary declaration must carry an explicit limitation.",
            claim_handles=(palette.claims[0].claim_handle,),
            recommendation_handles=(),
            limitation_handles=(),
            material_fact_bindings=(),
            statement_role="boundary",
            required=False,
        )


def test_next_action_requires_verified_recommendation_handle() -> None:
    palette = _palette()

    with pytest.raises(
        NarrativeAuthorityContractError,
        match="narrative_block_authority_handles_invalid",
    ):
        NarrativeBlock.create(
            writer_attempt_id="writer-attempt:claim-only-action",
            role="next_action",
            text="A claim alone cannot authorize an action.",
            claim_handles=(palette.claims[0].claim_handle,),
            recommendation_handles=(),
            limitation_handles=palette.claims[0].limitation_handles,
            material_fact_bindings=(),
            statement_role="recommendation",
            required=False,
        )


def test_boundary_only_palette_and_narrative_require_explicit_sealed_mode() -> None:
    source_bundle, _, _, _, _ = _authority()
    limitation = PublicLimitation.create(
        limitation_ref="limitation:no-authoritative-evidence",
        public_context={
            "obligations": ({"status": "unavailable"},),
        },
    )
    bundle = _reseal_bundle(
        source_bundle,
        authority_mode="boundary_only",
        obligation_coverage_refs=("obligation-coverage:all-unavailable",),
        evidence_refs=(),
        verified_claim_refs=(),
        recommendation_refs=(),
        assumption_refs=(),
        limitation_refs=(limitation.limitation_ref,),
    )
    palette = PublicClaimPalette.derive(
        authority_bundle=bundle,
        claims=(),
        claim_keys=(),
        recommendations=(),
        public_facts=(),
        public_limitations=(limitation,),
        visibility_policy=_policy(),
    )
    block = NarrativeBlock.create(
        writer_attempt_id="writer-attempt:boundary",
        role="boundary",
        text="The current authority has no supported business conclusion.",
        claim_handles=(),
        recommendation_handles=(),
        limitation_handles=(limitation.limitation_handle,),
        material_fact_bindings=(),
        statement_role="boundary",
        required=True,
    )
    document = NarrativeDocument.create(
        authority_bundle_ref=bundle.bundle_ref,
        material_projection_ref=_material_projection(palette).projection_ref,
        material_projection_digest=_material_projection(palette).content_digest,
        writer_attempt=_writer_attempt(palette, "writer-attempt:boundary"),
        parent_narrative_id=None,
        blocks=(block,),
    )
    local = BlockLocalValidationReport.validate(
        narrative=document,
        material_projection=_material_projection(palette),
        visibility_policy=_policy(),
        sensitive_output_findings=(),
    )
    verifier = BlockVerifierReport.create(
        narrative=document,
        material_projection=_material_projection(palette),
        visibility_policy=_policy(),
        local_report=local,
        verification_attempt=_verification_attempt(
            document,
            local,
            "verifier-attempt:boundary",
        ),
        accepted_block_ids=(block.block_id,),
        vetoes=(),
    )
    assert verifier.accepted_block_ids == (block.block_id,)

    claim_bearing_without_claims = _reseal_bundle(
        bundle,
        authority_mode="claim_bearing",
    )
    with pytest.raises(
        NarrativeAuthorityContractError,
        match="public_palette_bundle_integrity_invalid",
    ):
        PublicClaimPalette.derive(
            authority_bundle=claim_bearing_without_claims,
            claims=(),
            claim_keys=(),
            recommendations=(),
            public_facts=(),
            public_limitations=(limitation,),
            visibility_policy=_policy(),
        )


def test_original_writer_text_survives_local_and_semantic_validation_unchanged() -> (
    None
):
    palette = _palette()
    original = (
        "WajeSpecial（DeepSeek-V3.2）显示：付费金额为 −12.5%，"
        "方向表达可保持自然；Android 标签也可直接使用。  "
    )
    block = _block(
        palette,
        writer_attempt_id="writer-attempt:1",
        source_path=("change_rate",),
        text=original,
    )
    document = NarrativeDocument.create(
        authority_bundle_ref=palette.authority_bundle_ref,
        material_projection_ref=_material_projection(palette).projection_ref,
        material_projection_digest=_material_projection(palette).content_digest,
        writer_attempt=_writer_attempt(palette, "writer-attempt:1"),
        parent_narrative_id=None,
        blocks=(block,),
    )
    local = BlockLocalValidationReport.validate(
        narrative=document,
        material_projection=_material_projection(palette),
        visibility_policy=_policy(),
        sensitive_output_findings=(),
    )
    semantic = BlockVerifierReport.create(
        narrative=document,
        material_projection=_material_projection(palette),
        visibility_policy=_policy(),
        local_report=local,
        verification_attempt=_verification_attempt(
            document, local, "verifier-attempt:1"
        ),
        accepted_block_ids=(block.block_id,),
        vetoes=(),
    )

    assert local.accepted_block_ids == (block.block_id,)
    assert semantic.accepted_block_ids == (block.block_id,)
    assert document.blocks[0].text == original
    assert block.text == original
    assert NarrativeDocument.from_dict(document.to_dict()) == document


def test_exact_machine_fact_name_rejects_customer_block() -> None:
    palette = _palette()
    projection = _material_projection(palette)
    block = _block(
        palette,
        writer_attempt_id="writer-attempt:machine-name",
        source_path=("change_rate",),
        text="付费金额的 change_rate 为 12.5%。",
    )
    document = NarrativeDocument.create(
        authority_bundle_ref=palette.authority_bundle_ref,
        material_projection_ref=projection.projection_ref,
        material_projection_digest=projection.content_digest,
        writer_attempt=_writer_attempt(palette, "writer-attempt:machine-name"),
        parent_narrative_id=None,
        blocks=(block,),
    )

    report = BlockLocalValidationReport.validate(
        narrative=document,
        material_projection=projection,
        visibility_policy=_policy(),
        sensitive_output_findings=(),
    )

    assert report.accepted_block_ids == ()
    assert report.rejected_block_ids == (block.block_id,)
    assert {issue.code for issue in report.issues} == {
        "internal_fact_name_exposed"
    }


@pytest.mark.parametrize(
    ("source_path", "changed_value", "changed_range_end", "issue_code"),
    (
        (("change_rate",), "0.126", None, "material_fact_binding_mismatch"),
        (("target_date",), "2026-07-18", None, "material_fact_binding_mismatch"),
        (("scope",), "new_users", None, "material_fact_binding_mismatch"),
        (
            ("window_start",),
            None,
            "2026-07-18",
            "material_fact_binding_mismatch",
        ),
    ),
)
def test_material_fact_mismatch_fails_closed_on_only_its_block(
    source_path: tuple[str, ...],
    changed_value: str | None,
    changed_range_end: str | None,
    issue_code: str,
) -> None:
    palette = _palette()
    good = _block(
        palette,
        writer_attempt_id="writer-attempt:mismatch",
        source_path=("direction",),
        text="The primary direction increased.",
    )
    bad = _block(
        palette,
        writer_attempt_id="writer-attempt:mismatch",
        source_path=source_path,
        text="This block has a mismatched structured material fact.",
        role="dimension_localization"
        if source_path == ("window_start",)
        else "accounting_drivers",
        binding_value=changed_value,
        binding_range_end=changed_range_end,
    )
    document = NarrativeDocument.create(
        authority_bundle_ref=palette.authority_bundle_ref,
        material_projection_ref=_material_projection(palette).projection_ref,
        material_projection_digest=_material_projection(palette).content_digest,
        writer_attempt=_writer_attempt(palette, "writer-attempt:mismatch"),
        parent_narrative_id=None,
        blocks=(good, bad),
    )

    report = BlockLocalValidationReport.validate(
        narrative=document,
        material_projection=_material_projection(palette),
        visibility_policy=_policy(),
        sensitive_output_findings=(),
    )

    assert report.accepted_block_ids == (good.block_id,)
    assert report.rejected_block_ids == (bad.block_id,)
    assert {issue.code for issue in report.issues} == {issue_code}


def test_shared_evidence_fact_is_legal_for_every_claim_bound_to_its_material() -> None:
    palette = _palette()
    base_projection = _material_projection(palette)
    source_material = base_projection.evidence_materials[0]
    target_handle = next(
        item.claim_handle
        for item in base_projection.claims
        if source_material.material_handle not in set(item.material_handles)
    )
    material_projection = _projection_with_shared_first_material(palette)
    target_claim = next(
        item
        for item in material_projection.claims
        if item.claim_handle == target_handle
    )
    fact = material_projection.evidence_materials[0].facts[0]
    attempt_id = "writer-attempt:shared-material"
    block = NarrativeBlock.create(
        writer_attempt_id=attempt_id,
        role="contextual_pattern",
        text="The shared evidence can support a claim explicitly bound to its material.",
        claim_handles=(target_claim.claim_handle,),
        recommendation_handles=(),
        limitation_handles=target_claim.limitation_handles,
        material_fact_bindings=(
            NarrativeFactBinding.create(
                claim_handle=target_claim.claim_handle,
                fact_handle=fact.fact_handle,
                fact_kind=fact.fact_kind,
                value=fact.value,
                range_end=fact.range_end,
                unit=fact.unit,
            ),
        ),
        statement_role="business_finding",
        required=True,
    )
    document = NarrativeDocument.create(
        authority_bundle_ref=palette.authority_bundle_ref,
        material_projection_ref=material_projection.projection_ref,
        material_projection_digest=material_projection.content_digest,
        writer_attempt=_writer_attempt(
            palette,
            attempt_id,
            material_projection=material_projection,
        ),
        parent_narrative_id=None,
        blocks=(block,),
    )

    report = BlockLocalValidationReport.validate(
        narrative=document,
        material_projection=material_projection,
        visibility_policy=_policy(),
        sensitive_output_findings=(),
    )

    assert report.accepted_block_ids == (block.block_id,)
    assert report.issues == ()


def test_fact_from_an_unbound_material_cannot_be_borrowed_by_another_claim() -> None:
    palette = _palette()
    material_projection = _material_projection(palette)
    source_material = material_projection.evidence_materials[0]
    target_claim = next(
        item
        for item in material_projection.claims
        if source_material.material_handle not in set(item.material_handles)
    )
    fact = source_material.facts[0]
    attempt_id = "writer-attempt:cross-claim-borrow"
    block = NarrativeBlock.create(
        writer_attempt_id=attempt_id,
        role="contextual_pattern",
        text="This block attempts to borrow evidence outside the claim binding.",
        claim_handles=(target_claim.claim_handle,),
        recommendation_handles=(),
        limitation_handles=target_claim.limitation_handles,
        material_fact_bindings=(
            NarrativeFactBinding.create(
                claim_handle=target_claim.claim_handle,
                fact_handle=fact.fact_handle,
                fact_kind=fact.fact_kind,
                value=fact.value,
                range_end=fact.range_end,
                unit=fact.unit,
            ),
        ),
        statement_role="business_finding",
        required=True,
    )
    document = NarrativeDocument.create(
        authority_bundle_ref=palette.authority_bundle_ref,
        material_projection_ref=material_projection.projection_ref,
        material_projection_digest=material_projection.content_digest,
        writer_attempt=_writer_attempt(
            palette,
            attempt_id,
            material_projection=material_projection,
        ),
        parent_narrative_id=None,
        blocks=(block,),
    )

    report = BlockLocalValidationReport.validate(
        narrative=document,
        material_projection=material_projection,
        visibility_policy=_policy(),
        sensitive_output_findings=(),
    )

    assert report.accepted_block_ids == ()
    assert report.rejected_block_ids == (block.block_id,)
    assert {issue.code for issue in report.issues} == {"material_fact_binding_mismatch"}


def test_unknown_handle_fails_closed_without_affecting_a_valid_sibling() -> None:
    palette = _palette()
    good = _block(
        palette,
        writer_attempt_id="writer-attempt:handles",
        source_path=("direction",),
        text="The paid amount direction increased.",
    )
    bad = NarrativeBlock.create(
        writer_attempt_id="writer-attempt:handles",
        role="contextual_pattern",
        text="This block references a handle outside the supplied palette.",
        claim_handles=("c_unknown",),
        recommendation_handles=(),
        limitation_handles=(),
        material_fact_bindings=(),
        statement_role="business_finding",
        required=False,
    )
    document = NarrativeDocument.create(
        authority_bundle_ref=palette.authority_bundle_ref,
        material_projection_ref=_material_projection(palette).projection_ref,
        material_projection_digest=_material_projection(palette).content_digest,
        writer_attempt=_writer_attempt(palette, "writer-attempt:handles"),
        parent_narrative_id=None,
        blocks=(good, bad),
    )

    report = BlockLocalValidationReport.validate(
        narrative=document,
        material_projection=_material_projection(palette),
        visibility_policy=_policy(),
        sensitive_output_findings=(),
    )

    assert report.accepted_block_ids == (good.block_id,)
    assert report.rejected_block_ids == (bad.block_id,)
    assert {issue.code for issue in report.issues} == {"unknown_claim_handle"}


def test_sensitive_output_finding_is_a_typed_block_local_veto() -> None:
    palette = _palette()
    safe = _block(
        palette,
        writer_attempt_id="writer-attempt:sensitive",
        source_path=("direction",),
        text="The observed direction increased.",
    )
    affected = _block(
        palette,
        writer_attempt_id="writer-attempt:sensitive",
        source_path=("device",),
        text="A fixed sensitive-output policy flagged this provider response block.",
        role="dimension_localization",
    )
    document = NarrativeDocument.create(
        authority_bundle_ref=palette.authority_bundle_ref,
        material_projection_ref=_material_projection(palette).projection_ref,
        material_projection_digest=_material_projection(palette).content_digest,
        writer_attempt=_writer_attempt(palette, "writer-attempt:sensitive"),
        parent_narrative_id=None,
        blocks=(safe, affected),
    )
    finding = SensitiveOutputFinding.create(
        block_id=affected.block_id,
        field_visibility_policy_ref=_policy().policy_ref,
        policy_rule_ref="sensitive-output-policy:fixed-identifier",
        material_ref="restricted-material:1",
    )

    report = BlockLocalValidationReport.validate(
        narrative=document,
        material_projection=_material_projection(palette),
        visibility_policy=_policy(),
        sensitive_output_findings=(finding,),
    )

    assert report.accepted_block_ids == (safe.block_id,)
    assert report.rejected_block_ids == (affected.block_id,)
    assert {issue.code for issue in report.issues} == {
        "sensitive_output_policy_violation"
    }

    wrong_policy = SensitiveOutputFinding.create(
        block_id=affected.block_id,
        field_visibility_policy_ref="field-visibility-policy:other",
        policy_rule_ref="sensitive-output-policy:fixed-identifier",
        material_ref="restricted-material:1",
    )
    with pytest.raises(
        NarrativeAuthorityContractError,
        match="block_local_sensitive_finding_policy_closure_invalid",
    ):
        BlockLocalValidationReport.validate(
            narrative=document,
            material_projection=_material_projection(palette),
            visibility_policy=_policy(),
            sensitive_output_findings=(wrong_policy,),
        )


def test_semantic_verifier_is_veto_only_and_block_local() -> None:
    palette = _palette()
    accepted = _block(
        palette,
        writer_attempt_id="writer-attempt:semantic",
        source_path=("direction",),
        text="The verified direction increased.",
    )
    rejected = _block(
        palette,
        writer_attempt_id="writer-attempt:semantic",
        source_path=("device",),
        text="The device pattern proves a causal effect.",
        role="dimension_localization",
    )
    document = NarrativeDocument.create(
        authority_bundle_ref=palette.authority_bundle_ref,
        material_projection_ref=_material_projection(palette).projection_ref,
        material_projection_digest=_material_projection(palette).content_digest,
        writer_attempt=_writer_attempt(palette, "writer-attempt:semantic"),
        parent_narrative_id=None,
        blocks=(accepted, rejected),
    )
    local = BlockLocalValidationReport.validate(
        narrative=document,
        material_projection=_material_projection(palette),
        visibility_policy=_policy(),
        sensitive_output_findings=(),
    )
    veto = BlockVeto.create(
        narrative_id=document.narrative_id,
        block_id=rejected.block_id,
        reason_code="claim_meaning_exceeds_publication_ceiling",
        affected_claim_handles=rejected.claim_handles,
        affected_recommendation_handles=(),
        limitation_handles=rejected.limitation_handles,
    )
    report = BlockVerifierReport.create(
        narrative=document,
        material_projection=_material_projection(palette),
        visibility_policy=_policy(),
        local_report=local,
        verification_attempt=_verification_attempt(
            document, local, "verifier-attempt:semantic"
        ),
        accepted_block_ids=(accepted.block_id,),
        vetoes=(veto,),
    )

    assert report.accepted_block_ids == (accepted.block_id,)
    assert report.rejected_block_ids == (rejected.block_id,)
    assert document.blocks == (accepted, rejected)
    assert "replacement" not in json.dumps(report.to_dict(), sort_keys=True)

    invalid = veto.to_dict()
    invalid["replacement_text"] = "Rewrite supplied by verifier."
    with pytest.raises(
        NarrativeAuthorityContractError,
        match="block_veto_shape_invalid",
    ):
        BlockVeto.from_dict(invalid)


def test_revision_snapshot_preserves_block_origin_across_writer_attempts() -> None:
    palette = _palette()
    preserved = _block(
        palette,
        writer_attempt_id="writer-attempt:original",
        source_path=("direction",),
        text="The accepted direction remains unchanged.",
    )
    original_target = _block(
        palette,
        writer_attempt_id="writer-attempt:original",
        source_path=("device",),
        text="The device pattern proves a causal effect.",
        role="dimension_localization",
    )
    parent = NarrativeDocument.create(
        authority_bundle_ref=palette.authority_bundle_ref,
        material_projection_ref=_material_projection(palette).projection_ref,
        material_projection_digest=_material_projection(palette).content_digest,
        writer_attempt=_writer_attempt(palette, "writer-attempt:original"),
        parent_narrative_id=None,
        blocks=(preserved, original_target),
    )
    replacement = _block(
        palette,
        writer_attempt_id="writer-attempt:focused-retry",
        source_path=("device",),
        text="The device pattern remains within its verified ceiling.",
        role="dimension_localization",
    )
    retry_attempt = _writer_attempt(palette, "writer-attempt:focused-retry")

    retry = NarrativeDocument.create(
        authority_bundle_ref=parent.authority_bundle_ref,
        material_projection_ref=parent.material_projection_ref,
        material_projection_digest=parent.material_projection_digest,
        writer_attempt=retry_attempt,
        parent_narrative_id=parent.narrative_id,
        blocks=(preserved, replacement),
    )

    assert retry.parent_narrative_id == parent.narrative_id
    assert retry.writer_attempt_id != parent.writer_attempt_id
    assert retry.narrative_id != parent.narrative_id
    assert retry.blocks[0] is preserved
    assert retry.blocks[0].block_id == parent.blocks[0].block_id
    assert retry.blocks[0].writer_attempt_id == parent.writer_attempt_id
    assert retry.blocks[1] == replacement
    assert retry.blocks[1].writer_attempt_id == retry_attempt.attempt_id
    assert NarrativeDocument.from_dict(retry.to_dict()) == retry

    with pytest.raises(
        NarrativeAuthorityContractError,
        match="narrative_document_initial_attempt_closure_invalid",
    ):
        NarrativeDocument.create(
            authority_bundle_ref=parent.authority_bundle_ref,
            material_projection_ref=parent.material_projection_ref,
            material_projection_digest=parent.material_projection_digest,
            writer_attempt=retry_attempt,
            parent_narrative_id=None,
            blocks=(preserved, replacement),
        )
    with pytest.raises(
        NarrativeAuthorityContractError,
        match="narrative_document_revision_attempt_closure_invalid",
    ):
        NarrativeDocument.create(
            authority_bundle_ref=parent.authority_bundle_ref,
            material_projection_ref=parent.material_projection_ref,
            material_projection_digest=parent.material_projection_digest,
            writer_attempt=retry_attempt,
            parent_narrative_id=parent.narrative_id,
            blocks=(preserved,),
        )


def test_block_verifier_input_order_is_canonical() -> None:
    palette = _palette()
    first = _block(
        palette,
        writer_attempt_id="writer-attempt:canonical",
        source_path=("direction",),
        text="First rejected block.",
    )
    second = _block(
        palette,
        writer_attempt_id="writer-attempt:canonical",
        source_path=("device",),
        text="Second rejected block.",
        role="dimension_localization",
    )
    document = NarrativeDocument.create(
        authority_bundle_ref=palette.authority_bundle_ref,
        material_projection_ref=_material_projection(palette).projection_ref,
        material_projection_digest=_material_projection(palette).content_digest,
        writer_attempt=_writer_attempt(palette, "writer-attempt:canonical"),
        parent_narrative_id=None,
        blocks=(first, second),
    )
    local = BlockLocalValidationReport.validate(
        narrative=document,
        material_projection=_material_projection(palette),
        visibility_policy=_policy(),
        sensitive_output_findings=(),
    )
    first_veto = BlockVeto.create(
        narrative_id=document.narrative_id,
        block_id=first.block_id,
        reason_code="unsupported_synthesis",
        affected_claim_handles=first.claim_handles,
        affected_recommendation_handles=(),
        limitation_handles=first.limitation_handles,
    )
    second_veto = BlockVeto.create(
        narrative_id=document.narrative_id,
        block_id=second.block_id,
        reason_code="scope_drift",
        affected_claim_handles=second.claim_handles,
        affected_recommendation_handles=(),
        limitation_handles=second.limitation_handles,
    )

    forward = BlockVerifierReport.create(
        narrative=document,
        material_projection=_material_projection(palette),
        visibility_policy=_policy(),
        local_report=local,
        verification_attempt=_verification_attempt(
            document, local, "verifier-attempt:canonical"
        ),
        accepted_block_ids=(),
        vetoes=(first_veto, second_veto),
    )
    reverse = BlockVerifierReport.create(
        narrative=document,
        material_projection=_material_projection(palette),
        visibility_policy=_policy(),
        local_report=local,
        verification_attempt=_verification_attempt(
            document, local, "verifier-attempt:canonical"
        ),
        accepted_block_ids=(),
        vetoes=(second_veto, first_veto),
    )

    assert reverse == forward
    assert reverse.verifier_report_ref == forward.verifier_report_ref
    assert (
        BlockVerifierReport.from_dict(
            forward.to_dict(),
            narrative=document,
            material_projection=_material_projection(palette),
            visibility_policy=_policy(),
            local_report=local,
        )
        == forward
    )


def test_palette_replays_policy_and_fact_descriptors_before_exposure() -> None:
    bundle, claims, claim_keys, facts, limitations = _authority()

    with pytest.raises(
        NarrativeAuthorityContractError,
        match="public_fact_integrity_invalid",
    ):
        PublicClaimPalette.derive(
            authority_bundle=bundle,
            claims=claims,
            claim_keys=claim_keys,
            recommendations=(),
            public_facts=(replace(facts[0], value="999"), *facts[1:]),
            public_limitations=limitations,
            visibility_policy=_policy(),
        )

    forbidden = PublicFactDescriptor.create(
        claim=claims[0],
        public_name="owner_id",
        fact_kind="label",
        value="internal-owner-42",
        range_end=None,
        unit=None,
        source_material_ref=claims[0].support_edge_refs[0],
    )
    with pytest.raises(
        NarrativeAuthorityContractError,
        match="public_fact_name_forbidden",
    ):
        PublicClaimPalette.derive(
            authority_bundle=bundle,
            claims=claims,
            claim_keys=claim_keys,
            recommendations=(),
            public_facts=(*facts, forbidden),
            public_limitations=limitations,
            visibility_policy=_policy(),
        )

    with pytest.raises(
        NarrativeAuthorityContractError,
        match="publication_visibility_policy_integrity_invalid",
    ):
        PublicClaimPalette.derive(
            authority_bundle=bundle,
            claims=claims,
            claim_keys=claim_keys,
            recommendations=(),
            public_facts=facts,
            public_limitations=limitations,
            visibility_policy=replace(_policy(), forbidden_fields=()),
        )


def test_local_boundary_replays_blocks_and_sensitive_findings() -> None:
    palette = _palette()
    block = _block(
        palette,
        writer_attempt_id="writer-attempt:integrity",
        source_path=("direction",),
        text="The verified direction increased.",
    )
    document = NarrativeDocument.create(
        authority_bundle_ref=palette.authority_bundle_ref,
        material_projection_ref=_material_projection(palette).projection_ref,
        material_projection_digest=_material_projection(palette).content_digest,
        writer_attempt=_writer_attempt(palette, "writer-attempt:integrity"),
        parent_narrative_id=None,
        blocks=(block,),
    )

    with pytest.raises(
        NarrativeAuthorityContractError,
        match="narrative_block_integrity_invalid",
    ):
        BlockLocalValidationReport.validate(
            narrative=replace(
                document,
                blocks=(replace(block, text="Changed after sealing."),),
            ),
            material_projection=_material_projection(palette),
            visibility_policy=_policy(),
            sensitive_output_findings=(),
        )

    finding = SensitiveOutputFinding.create(
        block_id=block.block_id,
        field_visibility_policy_ref=_policy().policy_ref,
        policy_rule_ref="sensitive-output-policy:fixed",
        material_ref="restricted-material:1",
    )
    with pytest.raises(
        NarrativeAuthorityContractError,
        match="sensitive_output_finding_integrity_invalid",
    ):
        BlockLocalValidationReport.validate(
            narrative=document,
            material_projection=_material_projection(palette),
            visibility_policy=_policy(),
            sensitive_output_findings=(
                replace(finding, material_ref="restricted-material:2"),
            ),
        )


def test_verifier_covers_local_rejections_and_revision_preserves_accepted_block() -> (
    None
):
    palette = _palette()
    accepted = _block(
        palette,
        writer_attempt_id="writer-attempt:local-retry",
        source_path=("direction",),
        text="The verified direction increased.",
    )
    rejected = NarrativeBlock.create(
        writer_attempt_id="writer-attempt:local-retry",
        role="contextual_pattern",
        text="This draft used a handle outside the palette.",
        claim_handles=("c_unknown",),
        recommendation_handles=(),
        limitation_handles=(),
        material_fact_bindings=(),
        statement_role="context",
        required=False,
    )
    document = NarrativeDocument.create(
        authority_bundle_ref=palette.authority_bundle_ref,
        material_projection_ref=_material_projection(palette).projection_ref,
        material_projection_digest=_material_projection(palette).content_digest,
        writer_attempt=_writer_attempt(palette, "writer-attempt:local-retry"),
        parent_narrative_id=None,
        blocks=(accepted, rejected),
    )
    local = BlockLocalValidationReport.validate(
        narrative=document,
        material_projection=_material_projection(palette),
        visibility_policy=_policy(),
        sensitive_output_findings=(),
    )
    report = BlockVerifierReport.create(
        narrative=document,
        material_projection=_material_projection(palette),
        visibility_policy=_policy(),
        local_report=local,
        verification_attempt=_verification_attempt(
            document, local, "verifier-attempt:local-retry"
        ),
        accepted_block_ids=(accepted.block_id,),
        vetoes=(),
    )

    assert report.evaluated_block_ids == tuple(
        sorted((accepted.block_id, rejected.block_id))
    )
    assert report.rejected_block_ids == (rejected.block_id,)

    repaired = _block(
        palette,
        writer_attempt_id="writer-attempt:local-retry-2",
        source_path=("direction",),
        text="This context is now bound to the verified claim.",
        role="contextual_pattern",
        required=False,
    )
    retry = NarrativeDocument.create(
        authority_bundle_ref=document.authority_bundle_ref,
        material_projection_ref=document.material_projection_ref,
        material_projection_digest=document.material_projection_digest,
        writer_attempt=_writer_attempt(palette, "writer-attempt:local-retry-2"),
        parent_narrative_id=document.narrative_id,
        blocks=(accepted, repaired),
    )

    assert retry.parent_narrative_id == document.narrative_id
    assert retry.blocks == (accepted, repaired)
    assert retry.blocks[0].writer_attempt_id == document.writer_attempt_id
    assert retry.blocks[1].writer_attempt_id == retry.writer_attempt_id


def test_verifier_attempt_is_independent_from_writer_attempt() -> None:
    palette = _palette()
    block = _block(
        palette,
        writer_attempt_id="attempt:shared",
        source_path=("direction",),
        text="The verified direction increased.",
    )
    document = NarrativeDocument.create(
        authority_bundle_ref=palette.authority_bundle_ref,
        material_projection_ref=_material_projection(palette).projection_ref,
        material_projection_digest=_material_projection(palette).content_digest,
        writer_attempt=_writer_attempt(palette, "attempt:shared"),
        parent_narrative_id=None,
        blocks=(block,),
    )
    local = BlockLocalValidationReport.validate(
        narrative=document,
        material_projection=_material_projection(palette),
        visibility_policy=_policy(),
        sensitive_output_findings=(),
    )

    input_ref = "verifier-input:shared-attempt"
    input_digest = canonical_digest({"input_ref": input_ref})
    shared_response = RestrictedProviderResponse.create(
        attempt_id=document.writer_attempt_id,
        purpose="block_verification",
        provider_ref="provider:openai",
        model_ref="gpt-5",
        input_ref=input_ref,
        input_digest=input_digest,
        attempt_number=1,
        content='{"accepted": true}',
    )
    with pytest.raises(
        NarrativeAuthorityContractError,
        match="block_verification_attempt_independence_invalid",
    ):
        BlockVerificationAttempt.create(
            narrative=document,
            local_report=local,
            input_ref=input_ref,
            input_digest=input_digest,
            attempt_number=1,
            provider_response=shared_response,
        )
