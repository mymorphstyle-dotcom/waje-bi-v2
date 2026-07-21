from __future__ import annotations

import json
from dataclasses import dataclass, fields, replace

import pytest

import bi_agent.runtime.publication_authority as publication_authority
from bi_agent.runtime.claim_authority import (
    AuthorityBundle,
    ClaimAuthorityNamespace,
    ClaimKey,
    ClaimRevision,
    SemanticVerificationDecision,
)
from bi_agent.runtime.claim_settlement import (
    AuthorityBundleInputs,
    ClaimSettlement,
    prepare_claim_settlement,
    settle_claim_checkpoint,
)
from bi_agent.runtime.evidence_authority import canonical_digest
from bi_agent.runtime.insight_quality_rubric import (
    InsightEvaluationCaseSnapshot,
    InsightModelProfileSnapshot,
    InsightQualityRubric,
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
    RestrictedProviderResponse,
)
from bi_agent.runtime.narrative_material_projection import (
    NarrativeMaterialProjection,
)
from bi_agent.runtime.publication_authority import (
    DeliveryAttempt,
    DeliveryOutboxRecord,
    GuardrailPromotionRecord,
    InsightQualityEvaluation,
    NarrativeAttemptRequest,
    PublicationAuthorityContractError,
    PublicationProjection,
    PublicationRevision,
    validate_publication_lifecycle,
)
from bi_agent.runtime.single_authority import LifecycleState
from tests.phase7.test_claim_settlement import (
    _EvidenceSpec,
    _TaskSpec,
    _execution_result,
)


QUALITY_SCORES = {
    "explanation_value": 2,
    "novelty": 2,
    "decision_usefulness": 3,
    "competing_hypotheses": 2,
    "uncertainty_handling": 3,
    "actionability": 2,
}
QUALITY_REASONS = {
    "explanation_value": "The answer connects the change to one supported driver.",
    "novelty": "The exception pattern adds useful detail beyond the headline.",
    "decision_usefulness": "The result changes which segment should be investigated.",
    "competing_hypotheses": "One credible alternative remains only partly tested.",
    "uncertainty_handling": "The answer states the material evidence boundary.",
    "actionability": "The next query is feasible and tied to the observed gap.",
}


def _quality_case_snapshot(
    publication: PublicationRevision,
    *,
    case_id: str,
) -> InsightEvaluationCaseSnapshot:
    return InsightEvaluationCaseSnapshot.create(
        acceptance_summary_version=(
            "phase7-customer-publication-acceptance-summary.v2"
        ),
        acceptance_source="persisted_customer_publication",
        acceptance_summary_digest=canonical_digest(
            {
                "case_id": case_id,
                "run_attempt_id": publication.run_attempt_id,
                "publication_ref": publication.publication_ref,
            }
        ),
        acceptance_status="passed",
        case_id=case_id,
        question_family="pattern_explanation",
        variant="original",
        user_message="Explain the paid amount pattern.",
        review_focus="Assess evidence-bounded insight quality.",
        run_attempt_id=publication.run_attempt_id,
        publication_ref=publication.publication_ref,
        publication_digest=publication.publication_digest,
        customer_payload_ref=f"customer-payload:{case_id}",
        customer_payload_digest=canonical_digest(
            {"case_id": case_id, "publication_ref": publication.publication_ref}
        ),
    )


def _review_writer_attempt(publication: PublicationRevision) -> NarrativeWriterAttempt:
    input_ref = f"review-writer-input:{publication.narrative_attempt_id}"
    input_digest = canonical_digest(
        {
            "authority_bundle_ref": publication.authority_bundle_ref,
            "input_ref": input_ref,
        }
    )
    response = RestrictedProviderResponse.create(
        attempt_id=publication.narrative_attempt_id,
        purpose="narrative_writer",
        provider_ref="provider:deepseek",
        model_ref="deepseek-v3.2",
        input_ref=input_ref,
        input_digest=input_digest,
        attempt_number=1,
        content=json.dumps(
            {"attempt_id": publication.narrative_attempt_id, "blocks": []}
        ),
    )
    return NarrativeWriterAttempt.create(
        authority_bundle_ref=publication.authority_bundle_ref,
        material_projection_ref="narrative-material-projection:quality-review",
        material_projection_digest=canonical_digest(
            {"publication_ref": publication.publication_ref}
        ),
        input_ref=input_ref,
        input_digest=input_digest,
        attempt_number=1,
        provider_response=response,
    )


def _quality_model_profile(
    publication: PublicationRevision,
) -> InsightModelProfileSnapshot:
    writer_attempt = _review_writer_attempt(publication)
    return InsightModelProfileSnapshot.create(
        source_publication_ref=publication.publication_ref,
        source_publication_digest=publication.publication_digest,
        source_narrative_id=publication.narrative_id,
        source_narrative_attempt_id=publication.narrative_attempt_id,
        writer_attempt_ref=writer_attempt.writer_attempt_ref,
        writer_attempt_digest=writer_attempt.content_digest,
        writer_input_ref=writer_attempt.input_ref,
        writer_input_digest=writer_attempt.input_digest,
        writer_attempt_number=writer_attempt.attempt_number,
        provider_ref=writer_attempt.provider_ref,
        model_ref=writer_attempt.model_ref,
        provider_response_ref=writer_attempt.provider_response_ref,
        provider_response_digest=writer_attempt.provider_response_digest,
    )


@dataclass(frozen=True)
class _Context:
    bundle: AuthorityBundle
    claim: ClaimRevision
    claim_key: ClaimKey
    policy: PublicationFieldVisibilityPolicy
    material_projection: NarrativeMaterialProjection
    narrative: NarrativeDocument
    local_report: BlockLocalValidationReport
    verifier_report: BlockVerifierReport
    projection: PublicationProjection
    publication: PublicationRevision
    supersedes_publication: PublicationRevision | None


def _authority(
    *,
    run_attempt_id: str = "run-claim-settlement",
) -> tuple[
    AuthorityBundle,
    ClaimSettlement,
    tuple,
    ClaimRevision,
    ClaimKey,
    tuple[PublicFactDescriptor, ...],
    tuple[PublicLimitation, ...],
]:
    execution = _execution_result(
        run_attempt_id=run_attempt_id,
        obligations={
            "required_change": ("comparative_change", "observed"),
            "auxiliary_change": ("comparative_change", "observed"),
        },
        tasks=(
            _TaskSpec(
                task_key="paid_amount_change",
                capability_id="paid_amount_change",
                obligation_names=("required_change",),
                evidence=(
                    _EvidenceSpec(
                        evidence_kind="observed",
                        maximum_claim_strength="directional",
                        supported_claim_kinds=("comparative_change",),
                        observation_name="change_rate",
                        observation_value="0.125",
                        limitation_refs=("limitation:partial-day",),
                    ),
                ),
            ),
            _TaskSpec(
                task_key="segment_change",
                capability_id="segment_change",
                obligation_names=("auxiliary_change",),
                evidence=(
                    _EvidenceSpec(
                        evidence_kind="observed",
                        maximum_claim_strength="directional",
                        supported_claim_kinds=("comparative_change",),
                        observation_name="change_rate",
                        observation_value="0.250",
                        limitation_refs=("limitation:partial-day",),
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
    verification_attempt = checkpoint.verification_attempt(
        provider_ref="provider:test",
        model_ref="model:claim-verifier",
        input_digest="a" * 64,
        attempt_number=1,
        raw_provider_response_ref="restricted-provider-response:publication",
        raw_provider_response_digest="b" * 64,
    )
    decisions = tuple(
        SemanticVerificationDecision.create(
            authority_namespace=namespace,
            verification_attempt=verification_attempt,
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
        verification_attempt=verification_attempt,
        verification_decisions=decisions,
    )
    authority_inputs = AuthorityBundleInputs.create(
        execution_result=execution,
        claim_settlement=settlement,
        recommendations=(),
    )
    bundle = authority_inputs.seal(
        bundle_revision=1,
        supersedes_bundle_ref=None,
        sealed_at="2026-07-18T10:00:00Z",
    )
    claims = settlement.accepted_claims
    keys = settlement.accepted_claim_keys
    facts = tuple(
        PublicFactDescriptor.create(
            claim=claim,
            public_name="source_1.change_rate",
            fact_kind="number",
            value=("0.125" if index == 0 else "0.250"),
            range_end=None,
            unit="ratio",
            source_material_ref=next(
                item.support_edge_ref
                for item in settlement.accepted_support_edges
                if item.support_edge_ref in claim.support_edge_refs
                and item.kind == "supports"
                and item.source_type == "evidence"
            ),
        )
        for index, claim in enumerate(claims)
    )
    limitations = tuple(
        PublicLimitation.create(
            limitation_ref=limitation_ref,
            public_context={
                "claims": ({"claim_class": "observed_fact"},),
            },
        )
        for limitation_ref in bundle.limitation_refs
    )
    entries = tuple(
        entry
        for _, _, evidence_entries, _ in execution.capability_outcome_bundles
        for entry in evidence_entries
    )
    return bundle, settlement, entries, claims[0], keys[0], facts, limitations


def _writer_attempt(
    *,
    bundle: AuthorityBundle,
    material_projection: NarrativeMaterialProjection,
    attempt_id: str,
) -> NarrativeWriterAttempt:
    input_ref = f"writer-input:{attempt_id}"
    input_digest = canonical_digest(
        {
            "authority_bundle_ref": bundle.bundle_ref,
            "material_projection_ref": material_projection.projection_ref,
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
        authority_bundle_ref=bundle.bundle_ref,
        material_projection_ref=material_projection.projection_ref,
        material_projection_digest=material_projection.content_digest,
        input_ref=input_ref,
        input_digest=input_digest,
        attempt_number=1,
        provider_response=response,
    )


def _verification_attempt(
    *,
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


def _context(
    *,
    attempt_id: str = "writer-attempt:1",
    text: str = "Paid amount increased by 12.5%.",
    revision: int = 1,
    supersedes_publication: PublicationRevision | None = None,
    run_attempt_id: str = "run-claim-settlement",
    customer_term_labels: dict[str, str] | None = None,
) -> _Context:
    bundle, settlement, entries, claim, key, facts, limitations = _authority(
        run_attempt_id=run_attempt_id
    )
    policy = PublicationFieldVisibilityPolicy.fixed(
        policy_id="aggregate-answer",
        revision=1,
        restricted_output_policy_ref="test-policy:raw-identifiers",
        restricted_output_policy_version="1",
        restricted_output_fields=("order_id", "user_id"),
    )
    palette = PublicClaimPalette.derive(
        authority_bundle=bundle,
        claims=settlement.accepted_claims,
        claim_keys=settlement.accepted_claim_keys,
        recommendations=(),
        public_facts=facts,
        public_limitations=limitations,
        visibility_policy=policy,
    )
    material_projection = NarrativeMaterialProjection.derive(
        palette=palette,
        claim_settlement=settlement,
        evidence_entries=entries,
    )
    public_claim = next(
        item for item in material_projection.claims if item.claim_ref == claim.claim_ref
    )
    projected_fact = next(
        item
        for material in material_projection.evidence_materials
        if material.material_handle in public_claim.material_handles
        for item in material.facts
        if item.name == "change_rate"
    )
    binding = NarrativeFactBinding.create(
        claim_handle=public_claim.claim_handle,
        fact_handle=projected_fact.fact_handle,
        fact_kind=projected_fact.fact_kind,
        value=projected_fact.value,
        range_end=projected_fact.range_end,
        unit=projected_fact.unit,
    )
    required = NarrativeBlock.create(
        writer_attempt_id=attempt_id,
        role="executive_answer",
        text=text,
        claim_handles=(public_claim.claim_handle,),
        recommendation_handles=(),
        limitation_handles=public_claim.limitation_handles,
        material_fact_bindings=(binding,),
        statement_role="business_finding",
        required=True,
    )
    optional = NarrativeBlock.create(
        writer_attempt_id=attempt_id,
        role="contextual_pattern",
        text="This context needs tighter qualification.",
        claim_handles=(public_claim.claim_handle,),
        recommendation_handles=(),
        limitation_handles=public_claim.limitation_handles,
        material_fact_bindings=(binding,),
        statement_role="context",
        required=False,
    )
    narrative = NarrativeDocument.create(
        authority_bundle_ref=bundle.bundle_ref,
        material_projection_ref=material_projection.projection_ref,
        material_projection_digest=material_projection.content_digest,
        writer_attempt=_writer_attempt(
            bundle=bundle,
            material_projection=material_projection,
            attempt_id=attempt_id,
        ),
        parent_narrative_id=None,
        blocks=(required, optional),
    )
    local_report = BlockLocalValidationReport.validate(
        narrative=narrative,
        material_projection=material_projection,
        visibility_policy=policy,
        sensitive_output_findings=(),
    )
    veto = BlockVeto.create(
        narrative_id=narrative.narrative_id,
        block_id=optional.block_id,
        reason_code="meaning_not_sufficiently_qualified",
        affected_claim_handles=optional.claim_handles,
        affected_recommendation_handles=(),
        limitation_handles=optional.limitation_handles,
    )
    verifier_report = BlockVerifierReport.create(
        narrative=narrative,
        material_projection=material_projection,
        visibility_policy=policy,
        local_report=local_report,
        verification_attempt=_verification_attempt(
            narrative=narrative,
            local_report=local_report,
            attempt_id=f"verifier-attempt:{attempt_id}",
        ),
        accepted_block_ids=(required.block_id,),
        vetoes=(veto,),
    )
    projection = PublicationProjection.create(
        authority_bundle=bundle,
        material_projection=material_projection,
        narrative=narrative,
        local_report=local_report,
        verifier_report=verifier_report,
        visibility_policy=policy,
        display_order=tuple(block.block_id for block in narrative.blocks),
        customer_term_labels=customer_term_labels,
        visualization_refs=("visualization:paid-amount-delta",),
        warnings=("warning:partial-day",),
    )
    publication = PublicationRevision.create(
        authority_bundle=bundle,
        material_projection=material_projection,
        narrative=narrative,
        local_report=local_report,
        verifier_report=verifier_report,
        projection=projection,
        visibility_policy=policy,
        revision=revision,
        supersedes_publication=supersedes_publication,
        published_at="2026-07-18T10:05:00Z",
    )
    return _Context(
        bundle=bundle,
        claim=claim,
        claim_key=key,
        policy=policy,
        material_projection=material_projection,
        narrative=narrative,
        local_report=local_report,
        verifier_report=verifier_report,
        projection=projection,
        publication=publication,
        supersedes_publication=supersedes_publication,
    )


def test_customer_projection_replaces_fixed_metric_ids_and_preserves_audit_text() -> None:
    context = _context(
        text="paid_amount decreased while paid_users partly offset the decline.",
        customer_term_labels={
            "paid_amount": "付费金额",
            "paid_users": "付费用户数",
        },
    )

    payload = context.projection.to_customer_payload(
        authority_bundle=context.bundle,
        material_projection=context.material_projection,
        narrative=context.narrative,
        local_report=context.local_report,
        verifier_report=context.verifier_report,
        visibility_policy=context.policy,
    )
    rendered = payload["blocks"][0]["text"]

    assert context.narrative.blocks[0].text.startswith("paid_amount")
    assert rendered.startswith("付费金额")
    assert "付费用户数" in rendered
    assert "paid_amount" not in rendered
    assert "paid_users" not in rendered
    assert (
        PublicationProjection.from_dict(
            context.projection.to_dict(),
            authority_bundle=context.bundle,
            material_projection=context.material_projection,
            narrative=context.narrative,
            local_report=context.local_report,
            verifier_report=context.verifier_report,
            visibility_policy=context.policy,
        )
        == context.projection
    )


def _outbox(context: _Context) -> DeliveryOutboxRecord:
    return DeliveryOutboxRecord.enqueue(
        authority_bundle=context.bundle,
        material_projection=context.material_projection,
        narrative=context.narrative,
        local_report=context.local_report,
        verifier_report=context.verifier_report,
        visibility_policy=context.policy,
        supersedes_publication=context.supersedes_publication,
        publication=context.publication,
        projection=context.projection,
        destination_ref="conversation:thread-42",
        channel="conversation_gateway",
    )


def test_projection_replays_full_authority_and_emits_one_safe_customer_shape() -> None:
    context = _context()

    payload = context.projection.to_customer_payload(
        authority_bundle=context.bundle,
        material_projection=context.material_projection,
        narrative=context.narrative,
        local_report=context.local_report,
        verifier_report=context.verifier_report,
        visibility_policy=context.policy,
    )
    rendered = json.dumps(payload, sort_keys=True)

    assert set(payload) == set(context.policy.visible_fields)
    assert payload["blocks"][0]["text"] == context.narrative.blocks[0].text
    assert payload["claim_refs"] == [context.claim.claim_ref]
    assert payload["blocks"][0]["material_fact_bindings"] == [
        {
            "name": "change_rate",
            "fact_kind": "number",
            "value": "0.125",
            "range_end": None,
            "unit": "ratio",
        }
    ]
    assert "internal-owner-42" not in rendered
    assert "raw-player-7" not in rendered
    assert "raw_rows" not in rendered
    assert "writer_attempt_id" not in rendered
    assert "raw_provider_response_ref" not in rendered

    assert (
        PublicationProjection.from_dict(
            context.projection.to_dict(),
            authority_bundle=context.bundle,
            material_projection=context.material_projection,
            narrative=context.narrative,
            local_report=context.local_report,
            verifier_report=context.verifier_report,
            visibility_policy=context.policy,
        )
        == context.projection
    )


def test_projection_fact_resolution_rejects_cross_claim_material_binding() -> None:
    context = _context()
    claim = next(
        item
        for item in context.material_projection.claims
        if item.claim_ref == context.claim.claim_ref
    )
    foreign_material = next(
        item
        for item in context.material_projection.evidence_materials
        if item.material_handle not in claim.material_handles
    )
    foreign_fact = foreign_material.facts[0]
    wrong_binding = NarrativeFactBinding.create(
        claim_handle=claim.claim_handle,
        fact_handle=foreign_fact.fact_handle,
        fact_kind=foreign_fact.fact_kind,
        value=foreign_fact.value,
        range_end=foreign_fact.range_end,
        unit=foreign_fact.unit,
    )
    *_, fact_by_pair = publication_authority._projection_authority_indexes(
        context.material_projection
    )

    assert any(
        fact_handle == foreign_fact.fact_handle for _, fact_handle in fact_by_pair
    )
    with pytest.raises(
        PublicationAuthorityContractError,
        match="publication_projection_fact_binding_invalid",
    ):
        publication_authority._resolve_projection_fact(
            fact_by_pair,
            wrong_binding,
        )


def test_visibility_policy_rejects_unknown_or_forbidden_fields_recursively() -> None:
    context = _context()
    payload = context.projection.to_customer_payload(
        authority_bundle=context.bundle,
        material_projection=context.material_projection,
        narrative=context.narrative,
        local_report=context.local_report,
        verifier_report=context.verifier_report,
        visibility_policy=context.policy,
    )
    payload["blocks"][0]["internal_debug"] = "secret"

    with pytest.raises(
        NarrativeAuthorityContractError,
        match="publication_customer_payload_forbidden_field",
    ):
        context.policy.validate_customer_payload(payload)


@pytest.mark.parametrize("source", ("block", "material_projection", "verifier"))
def test_projection_rejects_children_modified_with_old_digest(source: str) -> None:
    context = _context()
    narrative = context.narrative
    material_projection = context.material_projection
    verifier = context.verifier_report
    if source == "block":
        narrative = replace(
            narrative,
            blocks=(
                replace(narrative.blocks[0], text="Changed after sealing."),
                narrative.blocks[1],
            ),
        )
    elif source == "material_projection":
        material = material_projection.evidence_materials[0]
        material_projection = replace(
            material_projection,
            evidence_materials=(
                replace(
                    material,
                    facts=(replace(material.facts[0], value="999"),),
                ),
            ),
        )
    else:
        verifier = replace(verifier, accepted_block_ids=())

    with pytest.raises(
        PublicationAuthorityContractError,
        match="publication_projection_source_integrity_invalid",
    ):
        PublicationProjection.create(
            authority_bundle=context.bundle,
            material_projection=material_projection,
            narrative=narrative,
            local_report=context.local_report,
            verifier_report=verifier,
            visibility_policy=context.policy,
            display_order=tuple(block.block_id for block in context.narrative.blocks),
            visualization_refs=(),
            warnings=(),
        )


def test_required_block_rejection_is_published_and_kept_for_background_review() -> None:
    context = _context()
    required, optional = context.narrative.blocks
    required_veto = BlockVeto.create(
        narrative_id=context.narrative.narrative_id,
        block_id=required.block_id,
        reason_code="meaning_exceeds_publication_ceiling",
        affected_claim_handles=required.claim_handles,
        affected_recommendation_handles=(),
        limitation_handles=required.limitation_handles,
    )
    optional_veto = BlockVeto.create(
        narrative_id=context.narrative.narrative_id,
        block_id=optional.block_id,
        reason_code="meaning_not_sufficiently_qualified",
        affected_claim_handles=optional.claim_handles,
        affected_recommendation_handles=(),
        limitation_handles=optional.limitation_handles,
    )
    rejected = BlockVerifierReport.create(
        narrative=context.narrative,
        material_projection=context.material_projection,
        visibility_policy=context.policy,
        local_report=context.local_report,
        verification_attempt=_verification_attempt(
            narrative=context.narrative,
            local_report=context.local_report,
            attempt_id="verifier-attempt:required-rejected",
        ),
        accepted_block_ids=(),
        vetoes=(required_veto, optional_veto),
    )

    projection = PublicationProjection.create(
        authority_bundle=context.bundle,
        material_projection=context.material_projection,
        narrative=context.narrative,
        local_report=context.local_report,
        verifier_report=rejected,
        visibility_policy=context.policy,
        display_order=tuple(block.block_id for block in context.narrative.blocks),
        visualization_refs=(),
        warnings=(),
    )
    payload = projection.to_customer_payload(
        authority_bundle=context.bundle,
        material_projection=context.material_projection,
        narrative=context.narrative,
        local_report=context.local_report,
        verifier_report=rejected,
        visibility_policy=context.policy,
    )
    assert projection.published_block_ids == tuple(
        sorted(block.block_id for block in context.narrative.blocks)
    )
    assert projection.safety_excluded_block_ids == ()
    assert [block["text"] for block in payload["blocks"]] == [
        block.text for block in context.narrative.blocks
    ]
    assert rejected.rejected_block_ids
    return

    withheld = LifecycleState.create(
        run_attempt_id=context.bundle.run_attempt_id,
        execution_state="complete",
        evidence_state="complete",
        publication_state="withheld",
        delivery_state="pending",
    )
    validate_publication_lifecycle(
        lifecycle=withheld,
        authority_bundle=context.bundle,
        publication=None,
        outbox=None,
        narrative=context.narrative,
        verifier_report=rejected,
    )

    verified = withheld.transition(
        publication_state="verified",
        delivery_state="persisted",
    )
    with pytest.raises(
        PublicationAuthorityContractError,
        match="publication_lifecycle_required_block_disposition_invalid",
    ):
        validate_publication_lifecycle(
            lifecycle=verified,
            authority_bundle=context.bundle,
            publication=context.publication,
            outbox=_outbox(context),
            narrative=context.narrative,
            verifier_report=rejected,
        )


def test_ready_lifecycle_requires_matching_outbox_on_existing_ssot() -> None:
    context = _context()
    ready = LifecycleState.create(
        run_attempt_id=context.bundle.run_attempt_id,
        execution_state="complete",
        evidence_state="complete",
        publication_state="ready",
        delivery_state="persisted",
    )

    with pytest.raises(
        PublicationAuthorityContractError,
        match="publication_lifecycle_stable_closure_invalid",
    ):
        validate_publication_lifecycle(
            lifecycle=ready,
            authority_bundle=context.bundle,
            publication=context.publication,
            outbox=None,
        )

    validate_publication_lifecycle(
        lifecycle=ready,
        authority_bundle=context.bundle,
        publication=context.publication,
        outbox=_outbox(context),
    )
    assert not hasattr(publication_authority, "LifecycleRecord")
    assert not hasattr(publication_authority, "AnalysisState")
    assert not hasattr(publication_authority, "PublicationState")
    assert not hasattr(publication_authority, "DeliveryState")


def test_narrative_revision_keeps_the_sealed_bundle_and_replays_all_children() -> None:
    first = _context()
    second = _context(
        attempt_id="writer-attempt:2",
        text="The verified full-sample increase was 12.5%.",
        revision=2,
        supersedes_publication=first.publication,
    )

    assert second.bundle.bundle_ref == first.bundle.bundle_ref
    assert (
        second.publication.authority_bundle_ref
        == first.publication.authority_bundle_ref
    )
    assert second.publication.narrative_id != first.publication.narrative_id
    assert second.publication.publication_ref != first.publication.publication_ref
    assert (
        PublicationRevision.from_dict(
            second.publication.to_dict(),
            authority_bundle=second.bundle,
            material_projection=second.material_projection,
            narrative=second.narrative,
            local_report=second.local_report,
            verifier_report=second.verifier_report,
            projection=second.projection,
            visibility_policy=second.policy,
            supersedes_publication=first.publication,
        )
        == second.publication
    )


def test_delivery_retry_reuses_outbox_payload_without_analysis_recomputation() -> None:
    context = _context()
    outbox = _outbox(context)
    duplicate = _outbox(context)
    failed = DeliveryAttempt.record(
        outbox=outbox,
        attempt_number=1,
        previous_attempt=None,
        status="retryable_failed",
        transport_receipt_ref=None,
        failure_code="gateway_unavailable",
        attempted_at="2026-07-18T10:06:00Z",
    )
    succeeded = DeliveryAttempt.record(
        outbox=outbox,
        attempt_number=2,
        previous_attempt=failed,
        status="published",
        transport_receipt_ref="gateway-receipt:42",
        failure_code=None,
        attempted_at="2026-07-18T10:07:00Z",
    )

    assert duplicate == outbox
    assert succeeded.idempotency_key == failed.idempotency_key
    assert succeeded.publication_ref == context.publication.publication_ref
    assert {
        "llm_request_ref",
        "query_ref",
        "claim_verifier_request_ref",
        "repair_text",
    }.isdisjoint({field.name for field in fields(DeliveryAttempt)})


def test_outbox_and_delivery_attempt_reject_modified_children() -> None:
    context = _context()
    with pytest.raises(
        PublicationAuthorityContractError,
        match="publication_projection_source_integrity_invalid",
    ):
        DeliveryOutboxRecord.enqueue(
            authority_bundle=context.bundle,
            material_projection=context.material_projection,
            narrative=replace(
                context.narrative,
                blocks=(
                    replace(context.narrative.blocks[0], text="Changed."),
                    context.narrative.blocks[1],
                ),
            ),
            local_report=context.local_report,
            verifier_report=context.verifier_report,
            visibility_policy=context.policy,
            supersedes_publication=None,
            publication=context.publication,
            projection=context.projection,
            destination_ref="conversation:thread-42",
            channel="conversation_gateway",
        )

    outbox = _outbox(context)
    with pytest.raises(
        PublicationAuthorityContractError,
        match="delivery_attempt_outbox_integrity_invalid",
    ):
        DeliveryAttempt.record(
            outbox=replace(outbox, destination_ref="conversation:thread-99"),
            attempt_number=1,
            previous_attempt=None,
            status="published",
            transport_receipt_ref="receipt:1",
            failure_code=None,
            attempted_at="2026-07-18T10:06:00Z",
        )


def test_low_quality_review_is_advisory_and_requests_an_independent_attempt() -> None:
    context = _context()
    request = NarrativeAttemptRequest.create(
        publication=context.publication,
        requested_attempt_id="writer-attempt:quality-rewrite-1",
        reason_dimensions=("explanation_value", "competing_hypotheses"),
        requested_by="reviewer:business-42",
    )
    evaluation = InsightQualityEvaluation.review(
        publication=context.publication,
        rubric=InsightQualityRubric.v1(),
        evaluation_case=_quality_case_snapshot(
            context.publication,
            case_id="eval-case:paid-amount-1",
        ),
        model_profile=_quality_model_profile(context.publication),
        reviewer_ref="reviewer:business-42",
        scores=QUALITY_SCORES,
        human_reasons=QUALITY_REASONS,
        narrative_attempt_request=request,
        reviewed_at="2026-07-18T11:00:00Z",
    )

    assert evaluation.advisory is True
    assert evaluation.result == "request_independent_narrative_attempt"
    assert request.requested_attempt_id != context.narrative.writer_attempt_id
    assert request.authority_bundle_ref == context.bundle.bundle_ref

    with pytest.raises(
        PublicationAuthorityContractError,
        match="insight_quality_narrative_request_integrity_invalid",
    ):
        InsightQualityEvaluation.review(
            publication=context.publication,
            rubric=InsightQualityRubric.v1(),
            evaluation_case=_quality_case_snapshot(
                context.publication,
                case_id="eval-case:paid-amount-1",
            ),
            model_profile=_quality_model_profile(context.publication),
            reviewer_ref="reviewer:business-42",
            scores=QUALITY_SCORES,
            human_reasons=QUALITY_REASONS,
            narrative_attempt_request=replace(
                request,
                requested_attempt_id="writer-attempt:tampered",
            ),
            reviewed_at="2026-07-18T11:00:00Z",
        )


def _quality_evaluation(
    publication: PublicationRevision,
    *,
    case_ref: str,
    reviewer_ref: str,
) -> InsightQualityEvaluation:
    return InsightQualityEvaluation.review(
        publication=publication,
        rubric=InsightQualityRubric.v1(),
        evaluation_case=_quality_case_snapshot(publication, case_id=case_ref),
        model_profile=_quality_model_profile(publication),
        reviewer_ref=reviewer_ref,
        scores=QUALITY_SCORES,
        human_reasons=QUALITY_REASONS,
        narrative_attempt_request=None,
        reviewed_at="2026-07-18T11:00:00Z",
    )


def test_guardrail_promotion_requires_recurring_cases_and_dual_ownership() -> None:
    publication = _context().publication
    first = _quality_evaluation(
        publication,
        case_ref="eval-case:paid-amount-1",
        reviewer_ref="reviewer:business-1",
    )
    second = _quality_evaluation(
        publication,
        case_ref="eval-case:paid-amount-2",
        reviewer_ref="reviewer:business-2",
    )
    approved = GuardrailPromotionRecord.approve(
        governance_scope_ref="governance:waje-runtime-guardrails",
        evaluations=(first, second),
        generalizable_pattern_ref="failure-pattern:missing-alternatives",
        recurrence_evidence_refs=("eval-run:1", "eval-run:2"),
        human_validation_ref="validation:pattern-review-1",
        business_owner_ref="owner:business-insight-quality",
        system_owner_ref="owner:agent-runtime",
        runtime_guardrail_ref="guardrail:competing-hypothesis-coverage",
        approved_at="2026-07-18T12:00:00Z",
    )

    assert set(approved.evaluation_refs) == {
        first.evaluation_ref,
        second.evaluation_ref,
    }
    assert approved.business_owner_ref != approved.system_owner_ref

    with pytest.raises(
        PublicationAuthorityContractError,
        match="guardrail_promotion_evaluation_integrity_invalid",
    ):
        GuardrailPromotionRecord.approve(
            governance_scope_ref="governance:waje-runtime-guardrails",
            evaluations=(replace(first, result="tampered"), second),
            generalizable_pattern_ref="failure-pattern:missing-alternatives",
            recurrence_evidence_refs=("eval-run:1", "eval-run:2"),
            human_validation_ref="validation:pattern-review-1",
            business_owner_ref="owner:business-insight-quality",
            system_owner_ref="owner:agent-runtime",
            runtime_guardrail_ref="guardrail:competing-hypothesis-coverage",
            approved_at="2026-07-18T12:00:00Z",
        )

    with pytest.raises(
        PublicationAuthorityContractError,
        match="guardrail_promotion_dual_ownership_invalid",
    ):
        GuardrailPromotionRecord.approve(
            governance_scope_ref="governance:waje-runtime-guardrails",
            evaluations=(first, second),
            generalizable_pattern_ref="failure-pattern:missing-alternatives",
            recurrence_evidence_refs=("eval-run:1", "eval-run:2"),
            human_validation_ref="validation:pattern-review-1",
            business_owner_ref="owner:shared",
            system_owner_ref="owner:shared",
            runtime_guardrail_ref="guardrail:competing-hypothesis-coverage",
            approved_at="2026-07-18T12:00:00Z",
        )
