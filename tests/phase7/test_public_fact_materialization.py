from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from bi_agent.runtime.capability_authority import EvidenceLedgerEntry
from bi_agent.runtime.claim_authority import (
    AuthorityBundle,
    ClaimAuthorityNamespace,
    ClaimKey,
    ClaimPublicationCeiling,
    ClaimRevision,
    SupportEdge,
)
from bi_agent.runtime.evidence_authority import canonical_digest
from bi_agent.runtime.narrative_authority import PublicationFieldVisibilityPolicy
from bi_agent.runtime.public_fact_materialization import (
    PublicFactMaterialization,
    PublicFactMaterializationContractError,
    PublicFactMaterializationIssue,
    materialize_public_facts,
)


def _namespace(seed: str = "facts") -> ClaimAuthorityNamespace:
    return ClaimAuthorityNamespace.create(
        run_attempt_id=f"run:{seed}",
        intent_revision_id=f"intent:{seed}",
        plan_revision_id=f"plan:{seed}",
    )


def _entry(
    namespace: ClaimAuthorityNamespace,
    observation_facts,
) -> EvidenceLedgerEntry:
    return EvidenceLedgerEntry._from_components(
        run_attempt_id=namespace.run_attempt_id,
        authority_context_ref="authority-context:public-facts",
        plan_revision_id="plan-revision:public-facts",
        task_id="task:public-facts",
        outcome_ref="outcome:public-facts",
        evidence_ref="evidence:public-facts",
        binding_record_ref="binding:public-facts",
        execution_state="available",
        evidence_kind="observed",
        data_contract_state="complete",
        supported_claim_kinds=("comparative_change",),
        evidence_strength="qualified",
        maximum_claim_strength="descriptive",
        observation_facts=observation_facts,
        scope="scope:full-sample",
        window_refs=("window:target", "window:baseline"),
        dimension_path=(),
        limitation_refs=(),
        result_refs=("result:public-facts",),
        completeness_report_refs=("completeness:public-facts",),
        hierarchy_qualified=False,
    )


def _authority(
    observation_facts,
    *,
    factual_payload_extra=None,
    seed: str = "facts",
):
    namespace = _namespace(seed)
    entry = _entry(namespace, observation_facts)
    key = ClaimKey.create(
        authority_namespace=namespace,
        goal_id="goal:public-facts",
        claim_kind="comparative_change",
        subject="aggregate metric movement",
        metric_ref="metric:aggregate",
        target_window_ref="window:target",
        baseline_window_ref="window:baseline",
        scope="scope:full-sample",
        grain="day",
        dimension_path=(),
    )
    ceiling = ClaimPublicationCeiling.create(
        claim_class="observed_fact",
        strength="descriptive",
    )
    edge = SupportEdge.create(
        authority_namespace=namespace,
        kind="supports",
        source_type="evidence",
        source_ref=entry.entry_ref,
        source_epistemic_class="observed_fact",
        source_publication_ceiling=ceiling,
        target_claim_key=key.claim_key,
        limitation_refs=(),
    )
    payload = {
        "obligation_id": "obligation:public-facts",
        "claim_kind": "comparative_change",
        **(factual_payload_extra or {}),
    }
    claim = ClaimRevision.create(
        authority_namespace=namespace,
        claim_key=key,
        factual_payload=payload,
        claim_class="observed_fact",
        support_edges=(edge,),
        dependency_claim_refs=(),
        limitation_refs=(),
        status="verified",
        publication_ceiling=ceiling,
    )
    bundle = _bundle(namespace, claim=claim, entry=entry)
    return namespace, entry, key, edge, claim, bundle


def _bundle(
    namespace: ClaimAuthorityNamespace,
    *,
    claim: ClaimRevision | None,
    entry: EvidenceLedgerEntry | None,
) -> AuthorityBundle:
    token = namespace.authority_namespace_ref.removeprefix(
        "claim-authority-namespace:sha256:"
    )[:24]
    boundary_only = claim is None
    manifest = {
        "bundle_revision": 1,
        "supersedes_bundle_ref": None,
        "run_attempt_id": namespace.run_attempt_id,
        "intent_revision_id": "intent-revision:public-facts",
        "decision_refs": ("decision:baseline",),
        "plan_revision_id": "plan-revision:public-facts",
        "authority_context_ref": "authority-context:public-facts",
        "execution_result_ref": "authoritative-execution-result:public-facts",
        "execution_result_digest": "e" * 64,
        "claim_settlement_ref": (f"claim-settlement:{token}:sha256:{'s' * 64}"),
        "claim_settlement_digest": "s" * 64,
        "claim_graph_ref": f"claim-graph:{token}:sha256:{'g' * 64}",
        "claim_graph_digest": "g" * 64,
        "authority_mode": "boundary_only" if boundary_only else "claim_bearing",
        "required_obligation_ids": (),
        "obligation_coverage_refs": (
            ("obligation-coverage:unavailable",) if boundary_only else ()
        ),
        "evidence_refs": (() if entry is None else (entry.entry_ref,)),
        "verified_claim_refs": (() if claim is None else (claim.claim_ref,)),
        "recommendation_refs": (),
        "assumption_refs": (),
        "limitation_refs": (("limitation:no-authority",) if boundary_only else ()),
        "claim_verifier_report_ref": (
            f"claim-verifier-report:{token}:sha256:{'v' * 64}"
        ),
    }
    digest = canonical_digest(manifest)
    return AuthorityBundle(
        bundle_ref=f"authority-bundle:{token}:sha256:{digest}",
        authority_namespace_ref=namespace.authority_namespace_ref,
        bundle_digest=digest,
        seal_state="sealed",
        sealed_at="2026-07-18T00:00:00Z",
        content_digest=digest,
        **manifest,
    )


def _policy() -> PublicationFieldVisibilityPolicy:
    return PublicationFieldVisibilityPolicy.fixed(
        policy_id="single-analysis-publication",
        revision=1,
        restricted_output_policy_ref="test-policy:raw-identifiers",
        restricted_output_policy_version="1",
        restricted_output_fields=("order_id", "user_id"),
    )


def _materialize(authority):
    namespace, entry, key, edge, claim, bundle = authority
    return materialize_public_facts(
        authority_bundle=bundle,
        authority_namespace=namespace,
        claims=(claim,),
        claim_keys=(key,),
        support_edges=(edge,),
        evidence_entries=(entry,),
        visibility_policy=_policy(),
    )


def _fact_ending(result: PublicFactMaterialization, suffix: str):
    matches = tuple(
        item for item in result.public_facts if item.public_name.endswith(suffix)
    )
    assert len(matches) == 1
    return matches[0]


def test_materializes_typed_aggregate_facts_and_nested_records() -> None:
    authority = _authority(
        (
            {"name": "absolute_change", "value": Decimal("12.500")},
            {
                "name": "analysis_window",
                "fact_kind": "date_range",
                "value": "2026-07-16",
                "range_end": "2026-07-17",
            },
            {
                "name": "weighted_amount",
                "fact_kind": "number",
                "value": Decimal("40.250"),
                "unit": "USD",
            },
            {
                "members": (
                    {
                        "member": "A",
                        "baseline_value": 100,
                        "target_value": Decimal("112.5"),
                        "active": True,
                        "owner_id": "internal-owner",
                    },
                    {
                        "member": "B",
                        "baseline_value": 80,
                        "target_value": 75,
                    },
                )
            },
        )
    )

    result = _materialize(authority)

    assert result.materialization_state == "ready"
    assert result.claims_without_public_facts == ()
    assert _fact_ending(result, "absolute_change").value == "12.5"
    window = _fact_ending(result, "analysis_window")
    assert (window.fact_kind, window.value, window.range_end) == (
        "date_range",
        "2026-07-16",
        "2026-07-17",
    )
    assert _fact_ending(result, "members[0].member").value == "A"
    assert _fact_ending(result, "members[0].target_value").value == "112.5"
    assert _fact_ending(result, "members[1].baseline_value").value == "80"
    assert _fact_ending(result, "evidence_scope").fact_kind == "scope"
    weighted = _fact_ending(result, "weighted_amount")
    assert (weighted.value, weighted.unit) == ("40.25", "USD")
    assert {item.issue_code for item in result.issues} == {
        "boolean_not_public_fact",
        "field_visibility_blocked",
    }
    assert all("owner_id" not in item.public_name for item in result.public_facts)
    assert all(
        item.source_material_ref == authority[3].support_edge_ref
        for item in result.public_facts
    )
    assert (
        result.replay(
            authority_bundle=authority[5],
            authority_namespace=authority[0],
            claims=(authority[4],),
            claim_keys=(authority[2],),
            support_edges=(authority[3],),
            evidence_entries=(authority[1],),
            visibility_policy=_policy(),
        )
        == result
    )


def test_reviewed_synthesis_contract_projects_writer_facts_without_losing_evidence() -> None:
    authority = _authority(
        (
            {
                "full_rows": (
                    {"member": "A", "value": 10},
                    {"member": "B", "value": 20},
                ),
                "decision_summary": {"member": "A", "value": 10},
                "claim_boundary": "directional_only",
                "synthesis_contract": {
                    "schema_version": "public-fact-projection.v1",
                    "public_fact_paths": (
                        "decision_summary",
                        "claim_boundary",
                    ),
                },
            },
        ),
        seed="synthesis-projection",
    )

    result = _materialize(authority)

    names = {item.public_name for item in result.public_facts}
    assert any(name.endswith("decision_summary.member") for name in names)
    assert any(name.endswith("decision_summary.value") for name in names)
    assert any(name.endswith("claim_boundary") for name in names)
    assert all("full_rows" not in name for name in names)
    assert authority[1].observation_facts[0]["full_rows"][1]["value"] == 20


def test_malformed_synthesis_contract_fails_closed() -> None:
    authority = _authority(
        (
            {
                "decision_summary": {"value": 10},
                "synthesis_contract": {
                    "schema_version": "public-fact-projection.v1",
                    "public_fact_paths": ("missing_path",),
                },
            },
        ),
        seed="synthesis-projection-invalid",
    )

    with pytest.raises(
        PublicFactMaterializationContractError,
        match="public_fact_materialization_synthesis_contract_invalid",
    ):
        _materialize(authority)


def test_claim_payload_values_are_never_promoted_without_evidence_material() -> None:
    authority = _authority(
        ({"name": "observed_amount", "value": 25},),
        factual_payload_extra={
            "llm_generated_number": "999999",
            "llm_generated_date": "2099-01-01",
        },
    )

    result = _materialize(authority)

    values = {item.value for item in result.public_facts}
    assert "25" in values
    assert "999999" not in values
    assert "2099-01-01" not in values


def test_ambiguous_named_structure_is_explicit_and_blocks_fact_readiness() -> None:
    authority = _authority(
        (
            {
                "name": "typed_payload",
                "value": {"nested": (1, 2)},
            },
        ),
        seed="ambiguous",
    )

    result = _materialize(authority)

    assert result.materialization_state == "incomplete"
    assert result.claims_without_public_facts == (authority[4].claim_ref,)
    assert {item.issue_code for item in result.issues} == {"named_fact_shape_ambiguous"}
    assert {item.public_name for item in result.public_facts} == {
        "source_1.evidence_scope"
    }


def test_business_record_value_field_is_not_misread_as_typed_fact_envelope() -> None:
    authority = _authority(
        (
            {
                "value": "Infinix X669",
                "dimension": "device_model",
                "target_amount": 1_690_442,
                "baseline_amount": 3_619_686,
            },
        ),
        seed="business-record-value-field",
    )

    result = _materialize(authority)

    assert result.materialization_state == "ready"
    assert result.claims_without_public_facts == ()
    assert _fact_ending(result, "value").value == "Infinix X669"
    assert _fact_ending(result, "target_amount").value == "1690442"
    assert "named_fact_shape_ambiguous" not in {
        item.issue_code for item in result.issues
    }


def test_claim_payload_cannot_embed_a_second_evidence_material_copy() -> None:
    authority = _authority(
        ({"name": "observed_amount", "value": 25},),
        factual_payload_extra={
            "evidence_observations": ({"forged": 999},),
        },
        seed="copied-mismatch",
    )

    with pytest.raises(
        PublicFactMaterializationContractError,
        match="public_fact_materialization_embedded_evidence_forbidden",
    ):
        _materialize(authority)


def test_dependency_bound_replay_rejects_tampered_materialization() -> None:
    authority = _authority(
        ({"name": "observed_amount", "value": 25},),
        seed="tamper",
    )
    result = _materialize(authority)
    payload = result.to_dict()
    payload["public_facts"][0]["value"] = "999"

    with pytest.raises(
        PublicFactMaterializationContractError,
        match="public_fact_materialization_integrity_invalid",
    ):
        PublicFactMaterialization.from_dict(
            payload,
            authority_bundle=authority[5],
            authority_namespace=authority[0],
            claims=(authority[4],),
            claim_keys=(authority[2],),
            support_edges=(authority[3],),
            evidence_entries=(authority[1],),
            visibility_policy=_policy(),
        )

    issue = PublicFactMaterializationIssue.create(
        claim_ref=authority[4].claim_ref,
        source_material_ref=authority[3].support_edge_ref,
        evidence_entry_ref=authority[1].entry_ref,
        observation_path=("observation_1", "nested", "nested"),
        issue_code="empty_structure",
        source_value={},
    )
    assert PublicFactMaterializationIssue.from_dict(issue.to_dict()) == issue
    issue_payload = issue.to_dict()
    issue_payload["source_value_digest"] = "0" * 64
    with pytest.raises(PublicFactMaterializationContractError):
        PublicFactMaterializationIssue.from_dict(issue_payload)


def test_boundary_authority_has_no_fact_materialization_surface() -> None:
    namespace = _namespace("boundary")
    bundle = _bundle(namespace, claim=None, entry=None)

    result = materialize_public_facts(
        authority_bundle=bundle,
        authority_namespace=namespace,
        claims=(),
        claim_keys=(),
        support_edges=(),
        evidence_entries=(),
        visibility_policy=_policy(),
    )

    assert result.materialization_state == "boundary_only"
    assert result.public_facts == ()
    assert result.issues == ()
    assert result.claims_without_public_facts == ()


def test_bundle_timestamp_tampering_is_rejected() -> None:
    authority = _authority(
        ({"name": "observed_amount", "value": 25},),
        seed="bundle-time",
    )
    forged_bundle = replace(authority[5], sealed_at="2026-07-18T08:00:00+08:00")

    with pytest.raises(
        PublicFactMaterializationContractError,
        match="public_fact_materialization_bundle_integrity_invalid",
    ):
        materialize_public_facts(
            authority_bundle=forged_bundle,
            authority_namespace=authority[0],
            claims=(authority[4],),
            claim_keys=(authority[2],),
            support_edges=(authority[3],),
            evidence_entries=(authority[1],),
            visibility_policy=_policy(),
        )
