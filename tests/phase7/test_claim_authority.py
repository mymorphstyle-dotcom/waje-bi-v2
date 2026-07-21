from __future__ import annotations

from dataclasses import replace

import pytest

from bi_agent.runtime.claim_authority import (
    ClaimAuthorityContractError,
    ClaimAuthorityNamespace,
    ClaimGraph,
    ClaimKey,
    ClaimPublicationCeiling,
    ClaimRevision,
    ClaimVerifierReport,
    ClaimVeto,
    LocalBoundaryAuthority,
    ObligationCoverage,
    RecommendationCommitment,
    SemanticVerificationAttempt,
    SemanticVerificationDecision,
    SupportEdge,
    recommendation_authorization_for_ceiling,
)


_DEFAULT_STRENGTH = {
    "observed_fact": "directional",
    "accounting_identity_contribution": "quantified_contribution",
    "dimension_localization": "dimension_localization",
    "statistical_association": "statistical_association",
    "candidate_mechanism": "candidate_mechanism",
    "candidate_impact": "candidate_driver",
    "causal_effect": "causal_effect",
    "scenario": "scenario",
    "boundary": "boundary",
}


def _namespace(
    *,
    run_attempt_id: str = "run-attempt:test",
) -> ClaimAuthorityNamespace:
    return ClaimAuthorityNamespace.create(
        run_attempt_id=run_attempt_id,
        intent_revision_id="intent-revision:test",
        plan_revision_id="plan-revision:test",
    )


def _ceiling(
    claim_class: str,
    strength: str | None = None,
) -> ClaimPublicationCeiling:
    return ClaimPublicationCeiling.create(
        claim_class=claim_class,
        strength=strength or _DEFAULT_STRENGTH[claim_class],
    )


def _key(
    namespace: ClaimAuthorityNamespace,
    *,
    claim_kind: str = "comparative_change",
    subject: str = "paid amount",
    dimension_path: tuple[str, ...] = (),
) -> ClaimKey:
    return ClaimKey.create(
        authority_namespace=namespace,
        goal_id="goal:paid-amount-change",
        claim_kind=claim_kind,
        subject=subject,
        metric_ref="metric:paid_amount",
        target_window_ref="window:target",
        baseline_window_ref="window:baseline",
        scope="scope:full-sample",
        grain="day",
        dimension_path=dimension_path,
    )


def _edge(
    namespace: ClaimAuthorityNamespace,
    key: ClaimKey,
    *,
    source_ref: str = "evidence:paid-amount-window",
    source_class: str = "observed_fact",
    source_strength: str | None = None,
    kind: str = "supports",
    source_type: str = "evidence",
    limitation_refs: tuple[str, ...] = (),
) -> SupportEdge:
    return SupportEdge.create(
        authority_namespace=namespace,
        kind=kind,
        source_type=source_type,
        source_ref=source_ref,
        source_epistemic_class=source_class,
        source_publication_ceiling=_ceiling(source_class, source_strength),
        target_claim_key=key.claim_key,
        limitation_refs=limitation_refs,
    )


def _claim(
    namespace: ClaimAuthorityNamespace,
    key: ClaimKey,
    edges: tuple[SupportEdge, ...],
    *,
    claim_class: str = "observed_fact",
    status: str = "proposed",
    payload: dict[str, object] | None = None,
    limitations: tuple[str, ...] = (),
    ceiling: ClaimPublicationCeiling | None = None,
) -> ClaimRevision:
    return ClaimRevision.create(
        authority_namespace=namespace,
        claim_key=key,
        factual_payload=payload or {"direction": "increase", "change_rate": "0.12"},
        claim_class=claim_class,
        support_edges=edges,
        dependency_claim_refs=(),
        limitation_refs=limitations,
        status=status,
        publication_ceiling=ceiling or _ceiling(claim_class),
    )


def _accepted_authority(
    namespace: ClaimAuthorityNamespace,
) -> tuple[
    ClaimKey,
    SupportEdge,
    ClaimRevision,
    ClaimRevision,
    SemanticVerificationAttempt,
    SemanticVerificationDecision,
    ClaimVerifierReport,
    ObligationCoverage,
    ClaimGraph,
]:
    key = _key(namespace)
    edge = _edge(namespace, key)
    proposed = _claim(namespace, key, (edge,))
    verified = _claim(namespace, key, (edge,), status="verified")
    attempt = SemanticVerificationAttempt.create(
        authority_namespace=namespace,
        purpose="claim_settlement",
        authority_input_ref="claim-settlement-checkpoint:test",
        authority_input_digest="a" * 64,
        subject_refs=(proposed.claim_ref,),
        provider_ref="provider:deepseek",
        model_ref="deepseek-chat",
        input_digest="1" * 64,
        attempt_number=1,
        raw_provider_response_ref="restricted-provider-response:claim-test",
        raw_provider_response_digest="9" * 64,
    )
    decision = SemanticVerificationDecision.create(
        authority_namespace=namespace,
        verification_attempt=attempt,
        subject_ref=proposed.claim_ref,
        disposition="accepted",
        veto_basis=None,
        reason_code=None,
        limitation_refs=(),
    )
    report = ClaimVerifierReport.create(
        authority_namespace=namespace,
        verification_attempt=attempt,
        local_boundary_authority=None,
        verification_decisions=(decision,),
        proposed_to_verified={proposed.claim_ref: verified.claim_ref},
        vetoes=(),
    )
    coverage = ObligationCoverage.create(
        authority_namespace=namespace,
        verifier_report=report,
        obligation_id="obligation:paid-amount-change",
        status="satisfied",
        claim_refs=(verified.claim_ref,),
        limitation_refs=(),
    )
    graph = ClaimGraph.create(
        authority_namespace=namespace,
        authority_mode="claim_bearing",
        claim_keys=(key,),
        claims=(verified,),
        support_edges=(edge,),
        obligation_coverage=(coverage,),
        verifier_report=report,
        evidence_ceiling_by_ref={edge.source_ref: edge.source_publication_ceiling},
        assumption_refs=(),
        limitation_refs=(),
    )
    return key, edge, proposed, verified, attempt, decision, report, coverage, graph


def test_recommendation_commitment_is_typed_content_addressed_and_replayable() -> None:
    namespace = _namespace()
    commitment = RecommendationCommitment.create(
        authority_namespace=namespace,
        commitment_kind="action",
        text="Run a controlled experiment.",
        supporting_claim_refs=("claim:candidate",),
        diagnostic_mode=None,
        action_domain="business_operation",
        action_stage="experiment",
        expected_value_kind=None,
        expected_value_mode=None,
    )

    assert commitment.recommendation_commitment_ref.startswith(
        "recommendation-commitment:"
    )
    assert (
        RecommendationCommitment.from_dict(
            commitment.to_dict(), authority_namespace=namespace
        )
        == commitment
    )

    with pytest.raises(
        ClaimAuthorityContractError,
        match="recommendation_commitment_typed_fields_invalid",
    ):
        RecommendationCommitment.create(
            authority_namespace=namespace,
            commitment_kind="action",
            text="Run a controlled experiment.",
            supporting_claim_refs=("claim:candidate",),
            diagnostic_mode="causal",
            action_domain="business_operation",
            action_stage="experiment",
            expected_value_kind=None,
            expected_value_mode=None,
        )


def test_recommendation_authorization_preserves_noncausal_and_causal_ceilings() -> None:
    candidate = recommendation_authorization_for_ceiling(
        _ceiling("statistical_association", "candidate_driver")
    )
    causal = recommendation_authorization_for_ceiling(_ceiling("causal_effect"))
    boundary = recommendation_authorization_for_ceiling(_ceiling("boundary"))

    assert {
        "action_domain": "business_operation",
        "action_stage": "experiment",
    } in candidate["actions"]
    assert {
        "action_domain": "business_operation",
        "action_stage": "intervene",
    } not in candidate["actions"]
    assert {
        "expected_value_kind": "business_metric_effect",
        "expected_value_mode": "expected_effect",
    } not in candidate["expected_values"]
    assert {
        "action_domain": "business_operation",
        "action_stage": "intervene",
    } in causal["actions"]
    assert {
        "expected_value_kind": "business_metric_effect",
        "expected_value_mode": "expected_effect",
    } in causal["expected_values"]
    assert {
        "action_domain": "data_quality",
        "action_stage": "intervene",
    } in boundary["actions"]
    assert {
        "action_domain": "business_operation",
        "action_stage": "intervene",
    } not in boundary["actions"]


@pytest.mark.parametrize(
    "source_classes",
    (
        ("observed_fact",),
        ("candidate_mechanism",),
        ("observed_fact", "statistical_association"),
    ),
)
def test_candidate_impact_requires_observed_and_event_mechanism_support(
    source_classes: tuple[str, ...],
) -> None:
    namespace = _namespace()
    key = _key(namespace, claim_kind="business_object_candidate_impact")
    edges = tuple(
        _edge(
            namespace,
            key,
            source_ref=f"evidence:candidate-impact:{index}",
            source_class=source_class,
        )
        for index, source_class in enumerate(source_classes)
    )

    with pytest.raises(
        ClaimAuthorityContractError,
        match="claim_candidate_impact_composite_support_invalid",
    ):
        _claim(
            namespace,
            key,
            edges,
            claim_class="candidate_impact",
        )


def test_candidate_impact_accepts_the_two_required_support_classes() -> None:
    namespace = _namespace()
    key = _key(namespace, claim_kind="business_object_candidate_impact")
    edges = (
        _edge(
            namespace,
            key,
            source_ref="evidence:event-window-metric",
            source_class="observed_fact",
        ),
        _edge(
            namespace,
            key,
            source_ref="evidence:event-presence",
            source_class="candidate_mechanism",
        ),
    )

    claim = _claim(
        namespace,
        key,
        edges,
        claim_class="candidate_impact",
    )

    assert claim.publication_ceiling == _ceiling("candidate_impact")


def test_candidate_impact_rejects_causal_support() -> None:
    namespace = _namespace()
    key = _key(namespace, claim_kind="business_object_candidate_impact")
    edges = (
        _edge(
            namespace,
            key,
            source_ref="evidence:event-window-metric",
            source_class="observed_fact",
        ),
        _edge(
            namespace,
            key,
            source_ref="evidence:event-presence",
            source_class="candidate_mechanism",
        ),
        _edge(
            namespace,
            key,
            source_ref="evidence:causal",
            source_class="causal_effect",
        ),
    )

    with pytest.raises(
        ClaimAuthorityContractError,
        match="claim_support_epistemic_class_invalid",
    ):
        _claim(
            namespace,
            key,
            edges,
            claim_class="candidate_impact",
        )


def test_claim_refs_use_analysis_authority_without_customer_identity() -> None:
    first_namespace = _namespace()
    same_analysis_namespace = _namespace()
    second_namespace = _namespace(
        run_attempt_id="run-attempt:other",
    )

    first_key = _key(first_namespace)
    same_analysis_key = _key(same_analysis_namespace)
    second_key = _key(second_namespace)
    first_edge = _edge(first_namespace, first_key)
    same_analysis_edge = _edge(same_analysis_namespace, same_analysis_key)
    second_edge = _edge(second_namespace, second_key)
    first_claim = _claim(first_namespace, first_key, (first_edge,))
    same_analysis_claim = _claim(
        same_analysis_namespace,
        same_analysis_key,
        (same_analysis_edge,),
    )
    second_claim = _claim(second_namespace, second_key, (second_edge,))

    assert first_namespace == same_analysis_namespace
    assert first_key.claim_key == same_analysis_key.claim_key
    assert first_claim.claim_ref == same_analysis_claim.claim_ref
    assert first_key.content_digest == second_key.content_digest
    assert first_key.claim_key != second_key.claim_key
    assert first_claim.claim_ref != second_claim.claim_ref
    assert (
        first_claim.authority_namespace_ref == first_namespace.authority_namespace_ref
    )
    assert (
        second_claim.authority_namespace_ref == second_namespace.authority_namespace_ref
    )


def test_claim_revisions_keep_stable_key_and_content_addressed_revision() -> None:
    namespace = _namespace()
    key = _key(namespace)
    edge = _edge(namespace, key)
    first = _claim(namespace, key, (edge,))
    reordered = _claim(
        namespace,
        key,
        (edge,),
        payload={"change_rate": "0.12", "direction": "increase"},
    )
    revised = _claim(
        namespace,
        key,
        (edge,),
        payload={"direction": "increase", "change_rate": "0.15"},
    )

    assert first.claim_ref == reordered.claim_ref
    assert first.claim_ref != revised.claim_ref
    assert first.claim_key == revised.claim_key
    assert ClaimKey.from_dict(key.to_dict(), authority_namespace=namespace) == key
    assert (
        ClaimRevision.from_dict(
            first.to_dict(),
            authority_namespace=namespace,
            claim_key=key,
            support_edges=(edge,),
        )
        == first
    )


@pytest.mark.parametrize(
    ("source_class", "target_class"),
    (
        ("observed_fact", "accounting_identity_contribution"),
        ("observed_fact", "statistical_association"),
        ("statistical_association", "causal_effect"),
        ("boundary", "candidate_mechanism"),
    ),
)
def test_epistemic_classes_cannot_be_relabelled_by_shared_strength(
    source_class: str,
    target_class: str,
) -> None:
    namespace = _namespace()
    key = _key(namespace, claim_kind=target_class)
    edge = _edge(namespace, key, source_class=source_class)

    with pytest.raises(
        ClaimAuthorityContractError, match="claim_support_epistemic_class_invalid"
    ):
        _claim(
            namespace,
            key,
            (edge,),
            claim_class=target_class,
            ceiling=_ceiling(target_class),
        )


def test_same_class_support_cannot_raise_publication_ceiling() -> None:
    namespace = _namespace()
    key = _key(namespace)
    edge = _edge(namespace, key, source_strength="descriptive")

    with pytest.raises(
        ClaimAuthorityContractError, match="claim_support_strength_ceiling_exceeded"
    ):
        _claim(
            namespace,
            key,
            (edge,),
            ceiling=_ceiling("observed_fact", "directional"),
        )


def test_many_to_many_support_is_order_invariant_and_retained() -> None:
    namespace = _namespace()
    first_key = _key(namespace)
    second_key = _key(
        namespace,
        claim_kind="dimension_localization",
        subject="region localization",
        dimension_path=("region",),
    )
    first_edges = (
        _edge(namespace, first_key, source_ref="evidence:target"),
        _edge(namespace, first_key, source_ref="evidence:baseline"),
    )
    second_edge = _edge(
        namespace,
        second_key,
        source_ref="evidence:region",
        source_class="dimension_localization",
    )
    shared_context = _edge(
        namespace,
        second_key,
        source_ref="evidence:target",
        kind="contextualizes",
    )
    first_proposed = _claim(namespace, first_key, tuple(reversed(first_edges)))
    second_proposed = _claim(
        namespace,
        second_key,
        (second_edge, shared_context),
        claim_class="dimension_localization",
    )
    first = replace(first_proposed, status="verified")
    first = ClaimRevision.create(
        authority_namespace=namespace,
        claim_key=first_key,
        factual_payload=first_proposed.factual_payload,
        claim_class=first_proposed.claim_class,
        support_edges=first_edges,
        dependency_claim_refs=(),
        limitation_refs=(),
        status="verified",
        publication_ceiling=first_proposed.publication_ceiling,
    )
    second = ClaimRevision.create(
        authority_namespace=namespace,
        claim_key=second_key,
        factual_payload=second_proposed.factual_payload,
        claim_class=second_proposed.claim_class,
        support_edges=(shared_context, second_edge),
        dependency_claim_refs=(),
        limitation_refs=(),
        status="verified",
        publication_ceiling=second_proposed.publication_ceiling,
    )
    attempt = SemanticVerificationAttempt.create(
        authority_namespace=namespace,
        purpose="claim_settlement",
        authority_input_ref="checkpoint:many-to-many",
        authority_input_digest="b" * 64,
        subject_refs=(first_proposed.claim_ref, second_proposed.claim_ref),
        provider_ref="provider:deepseek",
        model_ref="deepseek-chat",
        input_digest="2" * 64,
        attempt_number=1,
        raw_provider_response_ref="restricted-provider-response:many-to-many",
        raw_provider_response_digest="8" * 64,
    )
    decisions = tuple(
        SemanticVerificationDecision.create(
            authority_namespace=namespace,
            verification_attempt=attempt,
            subject_ref=item.claim_ref,
            disposition="accepted",
            veto_basis=None,
            reason_code=None,
            limitation_refs=(),
        )
        for item in (second_proposed, first_proposed)
    )
    report = ClaimVerifierReport.create(
        authority_namespace=namespace,
        verification_attempt=attempt,
        local_boundary_authority=None,
        verification_decisions=decisions,
        proposed_to_verified={
            first_proposed.claim_ref: first.claim_ref,
            second_proposed.claim_ref: second.claim_ref,
        },
        vetoes=(),
    )
    coverage = (
        ObligationCoverage.create(
            authority_namespace=namespace,
            verifier_report=report,
            obligation_id="obligation:change",
            status="satisfied",
            claim_refs=(first.claim_ref,),
            limitation_refs=(),
        ),
        ObligationCoverage.create(
            authority_namespace=namespace,
            verifier_report=report,
            obligation_id="obligation:region",
            status="satisfied",
            claim_refs=(second.claim_ref,),
            limitation_refs=(),
        ),
    )
    graph = ClaimGraph.create(
        authority_namespace=namespace,
        authority_mode="claim_bearing",
        claim_keys=(second_key, first_key),
        claims=(second, first),
        support_edges=(shared_context, second_edge, *first_edges),
        obligation_coverage=coverage,
        verifier_report=report,
        evidence_ceiling_by_ref={
            "evidence:target": _ceiling("observed_fact"),
            "evidence:baseline": _ceiling("observed_fact"),
            "evidence:region": _ceiling("dimension_localization"),
        },
        assumption_refs=(),
        limitation_refs=(),
    )
    reordered = ClaimGraph.create(
        authority_namespace=namespace,
        authority_mode="claim_bearing",
        claim_keys=(first_key, second_key),
        claims=(first, second),
        support_edges=(*reversed(first_edges), second_edge, shared_context),
        obligation_coverage=tuple(reversed(coverage)),
        verifier_report=report,
        evidence_ceiling_by_ref={
            "evidence:region": _ceiling("dimension_localization"),
            "evidence:baseline": _ceiling("observed_fact"),
            "evidence:target": _ceiling("observed_fact"),
        },
        assumption_refs=(),
        limitation_refs=(),
    )

    assert graph == reordered
    assert graph.content_digest == reordered.content_digest
    assert len(first.support_edge_refs) == 2


def test_semantic_report_requires_attempt_decisions_and_proposed_to_verified_mapping() -> (
    None
):
    namespace = _namespace()
    _, _, proposed, verified, attempt, decision, report, _, _ = _accepted_authority(
        namespace
    )

    assert report.evaluated_claim_refs == (proposed.claim_ref,)
    assert report.proposed_to_verified == {proposed.claim_ref: verified.claim_ref}
    assert report.accepted_claim_refs == (verified.claim_ref,)
    assert (
        ClaimVerifierReport.from_dict(report.to_dict(), authority_namespace=namespace)
        == report
    )

    with pytest.raises(
        ClaimAuthorityContractError,
        match="claim_verifier_report_decision_coverage_invalid",
    ):
        ClaimVerifierReport.create(
            authority_namespace=namespace,
            verification_attempt=attempt,
            local_boundary_authority=None,
            verification_decisions=(),
            proposed_to_verified={},
            vetoes=(),
        )

    with pytest.raises(
        ClaimAuthorityContractError, match="claim_verifier_report_mapping_invalid"
    ):
        ClaimVerifierReport.create(
            authority_namespace=namespace,
            verification_attempt=attempt,
            local_boundary_authority=None,
            verification_decisions=(decision,),
            proposed_to_verified={},
            vetoes=(),
        )


def test_veto_is_bound_to_the_exact_semantic_decision() -> None:
    namespace = _namespace()
    key = _key(namespace)
    edge = _edge(namespace, key)
    proposed = _claim(namespace, key, (edge,))
    attempt = SemanticVerificationAttempt.create(
        authority_namespace=namespace,
        purpose="claim_settlement",
        authority_input_ref="checkpoint:veto",
        authority_input_digest="c" * 64,
        subject_refs=(proposed.claim_ref,),
        provider_ref="provider:deepseek",
        model_ref="deepseek-chat",
        input_digest="3" * 64,
        attempt_number=1,
        raw_provider_response_ref="restricted-provider-response:veto",
        raw_provider_response_digest="7" * 64,
    )
    decision = SemanticVerificationDecision.create(
        authority_namespace=namespace,
        verification_attempt=attempt,
        subject_ref=proposed.claim_ref,
        disposition="vetoed",
        veto_basis="semantic_boundary_exceeded",
        reason_code="semantic_scope_mismatch",
        limitation_refs=("limitation:scope",),
    )
    veto = ClaimVeto.create(
        authority_namespace=namespace,
        claim_ref=proposed.claim_ref,
        reason_code="semantic_scope_mismatch",
        limitation_refs=("limitation:scope",),
    )
    report = ClaimVerifierReport.create(
        authority_namespace=namespace,
        verification_attempt=attempt,
        local_boundary_authority=None,
        verification_decisions=(decision,),
        proposed_to_verified={},
        vetoes=(veto,),
    )
    assert report.rejected_claim_refs == (proposed.claim_ref,)

    forged_veto = replace(veto, reason_code="different_reason")
    with pytest.raises(
        ClaimAuthorityContractError, match="claim_verifier_report_vetoes_invalid"
    ):
        ClaimVerifierReport.create(
            authority_namespace=namespace,
            verification_attempt=attempt,
            local_boundary_authority=None,
            verification_decisions=(decision,),
            proposed_to_verified={},
            vetoes=(forged_veto,),
        )


def test_reasoned_veto_does_not_require_an_unrelated_limitation() -> None:
    namespace = _namespace()
    key = _key(namespace)
    edge = _edge(namespace, key)
    proposed = _claim(namespace, key, (edge,))
    attempt = SemanticVerificationAttempt.create(
        authority_namespace=namespace,
        purpose="claim_settlement",
        authority_input_ref="checkpoint:veto-without-limitation",
        authority_input_digest="c" * 64,
        subject_refs=(proposed.claim_ref,),
        provider_ref="provider:deepseek",
        model_ref="deepseek-chat",
        input_digest="3" * 64,
        attempt_number=1,
        raw_provider_response_ref="restricted-provider-response:veto-empty",
        raw_provider_response_digest="7" * 64,
    )
    decision = SemanticVerificationDecision.create(
        authority_namespace=namespace,
        verification_attempt=attempt,
        subject_ref=proposed.claim_ref,
        disposition="vetoed",
        veto_basis="factual_support_invalid",
        reason_code="factual_mismatch",
        limitation_refs=(),
    )
    veto = ClaimVeto.create(
        authority_namespace=namespace,
        claim_ref=proposed.claim_ref,
        reason_code="factual_mismatch",
        limitation_refs=(),
    )

    report = ClaimVerifierReport.create(
        authority_namespace=namespace,
        verification_attempt=attempt,
        local_boundary_authority=None,
        verification_decisions=(decision,),
        proposed_to_verified={},
        vetoes=(veto,),
    )

    assert report.rejected_claim_refs == (proposed.claim_ref,)


def test_all_unavailable_forms_explicit_boundary_only_graph() -> None:
    namespace = _namespace()
    boundary = LocalBoundaryAuthority.create(
        authority_namespace=namespace,
        checkpoint_ref="claim-settlement-checkpoint:boundary",
        checkpoint_digest="4" * 64,
        obligation_ids=("obligation:payment-success",),
        limitation_refs=("limitation:contract-missing",),
    )
    report = ClaimVerifierReport.create(
        authority_namespace=namespace,
        verification_attempt=None,
        local_boundary_authority=boundary,
        verification_decisions=(),
        proposed_to_verified={},
        vetoes=(),
    )
    coverage = ObligationCoverage.create(
        authority_namespace=namespace,
        verifier_report=report,
        obligation_id="obligation:payment-success",
        status="unavailable",
        claim_refs=(),
        limitation_refs=("limitation:contract-missing",),
    )
    graph = ClaimGraph.create(
        authority_namespace=namespace,
        authority_mode="boundary_only",
        claim_keys=(),
        claims=(),
        support_edges=(),
        obligation_coverage=(coverage,),
        verifier_report=report,
        evidence_ceiling_by_ref={},
        assumption_refs=(),
        limitation_refs=("limitation:contract-missing",),
    )

    assert graph.authority_mode == "boundary_only"
    assert graph.claim_refs == ()

    unresolved = ObligationCoverage.create(
        authority_namespace=namespace,
        verifier_report=report,
        obligation_id="obligation:payment-success",
        status="unresolved",
        claim_refs=(),
        limitation_refs=(),
    )
    with pytest.raises(
        ClaimAuthorityContractError, match="claim_graph_boundary_only_invalid"
    ):
        ClaimGraph.create(
            authority_namespace=namespace,
            authority_mode="boundary_only",
            claim_keys=(),
            claims=(),
            support_edges=(),
            obligation_coverage=(unresolved,),
            verifier_report=report,
            evidence_ceiling_by_ref={},
            assumption_refs=(),
            limitation_refs=(),
        )


def test_every_child_authority_boundary_replays_exact_content() -> None:
    namespace = _namespace()
    key, edge, _, verified, _, _, report, coverage, _ = _accepted_authority(namespace)

    forged_key = replace(key, subject="forged")
    with pytest.raises(ClaimAuthorityContractError, match="claim_revision_key_invalid"):
        _claim(namespace, forged_key, (edge,), status="verified")

    forged_edge = replace(edge, source_ref="evidence:forged")
    with pytest.raises(
        ClaimAuthorityContractError, match="claim_revision_support_edges_invalid"
    ):
        _claim(namespace, key, (forged_edge,), status="verified")

    forged_claim = replace(verified, factual_payload={"direction": "decrease"})
    with pytest.raises(ClaimAuthorityContractError, match="claim_graph_claims_invalid"):
        ClaimGraph.create(
            authority_namespace=namespace,
            authority_mode="claim_bearing",
            claim_keys=(key,),
            claims=(forged_claim,),
            support_edges=(edge,),
            obligation_coverage=(coverage,),
            verifier_report=report,
            evidence_ceiling_by_ref={edge.source_ref: edge.source_publication_ceiling},
            assumption_refs=(),
            limitation_refs=(),
        )

    forged_report = replace(report, accepted_claim_refs=("claim:forged",))
    with pytest.raises(
        ClaimAuthorityContractError, match="claim_graph_verifier_report_invalid"
    ):
        ClaimGraph.create(
            authority_namespace=namespace,
            authority_mode="claim_bearing",
            claim_keys=(key,),
            claims=(verified,),
            support_edges=(edge,),
            obligation_coverage=(coverage,),
            verifier_report=forged_report,
            evidence_ceiling_by_ref={edge.source_ref: edge.source_publication_ceiling},
            assumption_refs=(),
            limitation_refs=(),
        )


def test_exact_shapes_and_unknown_enums_fail_closed() -> None:
    namespace = _namespace()
    key = _key(namespace)
    payload = key.to_dict()
    payload.pop("grain")
    with pytest.raises(ClaimAuthorityContractError, match="claim_key_shape_invalid"):
        ClaimKey.from_dict(payload, authority_namespace=namespace)

    with pytest.raises(ClaimAuthorityContractError, match="support_edge_kind_invalid"):
        SupportEdge.create(
            authority_namespace=namespace,
            kind="maybe_supports",
            source_type="evidence",
            source_ref="evidence:x",
            source_epistemic_class="observed_fact",
            source_publication_ceiling=_ceiling("observed_fact"),
            target_claim_key=key.claim_key,
            limitation_refs=(),
        )
