from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import bi_agent.runtime.narrative_materialization as materialization
from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value
from bi_agent.runtime.narrative_authority import (
    NarrativeAuthorityContractError,
    PUBLICATION_FORBIDDEN_FIELDS,
    PublicLimitation,
    PublicationFieldVisibilityPolicy,
)
from bi_agent.runtime.narrative_materialization import (
    NarrativeMaterializationContractError,
    build_public_limitation_contexts,
    build_reviewed_public_materialization,
)
from bi_agent.runtime.public_fact_materialization import materialize_public_facts
from tests.phase7.test_authority_seal_persistence import _fixture


def _identity_authority_validation(**kwargs: Any) -> tuple[Any, ...]:
    return (
        kwargs["execution_result"],
        kwargs["authority_bundle"],
        kwargs["claim_settlement"],
        tuple(kwargs["recommendations"]),
    )


def _context_inputs(
    claim_specs: tuple[tuple[str, str, str], ...],
) -> tuple[Any, Any, Any]:
    limitation_ref = "limitation:shared-boundary"
    claim_keys = tuple(
        SimpleNamespace(
            claim_key=claim_key,
            claim_kind=claim_kind,
            subject={"business_subject_ref": subject_ref},
            scope="scope:full-sample",
            grain="daily",
            dimension_path=("market",),
        )
        for claim_key, claim_kind, subject_ref in claim_specs
    )
    claims = tuple(
        SimpleNamespace(
            claim_ref=f"claim:{claim_key}",
            claim_key=claim_key,
            claim_class="observed_fact",
            limitation_refs=(limitation_ref,),
        )
        for claim_key, _, _ in claim_specs
    )
    execution = SimpleNamespace(
        plan_revision=SimpleNamespace(claim_obligations=(), capability_tasks=()),
        capability_outcome_bundles=(),
    )
    bundle = SimpleNamespace(limitation_refs=(limitation_ref,))
    settlement = SimpleNamespace(
        accepted_claim_keys=claim_keys,
        accepted_claims=claims,
        obligation_coverage=(),
    )
    return execution, bundle, settlement


def _outcome_context_inputs(
    *,
    limitation_ref: str,
    outcomes: tuple[tuple[str, str, str], ...],
) -> tuple[Any, Any, Any]:
    obligation_id = "obligation:comparison-context"
    obligation = SimpleNamespace(
        obligation_id=obligation_id,
        claim_kind="recurring_pattern_existence",
    )
    tasks = tuple(
        SimpleNamespace(task_id=task_id, capability_id=capability_id)
        for task_id, capability_id, _ in outcomes
    )
    outcome_records = tuple(
        SimpleNamespace(
            task_id=task_id,
            status=status,
            retryability=("replan_required" if status == "unavailable" else "never"),
            affected_obligation_ids=(obligation_id,),
            limitation_refs=(limitation_ref,),
        )
        for task_id, _, status in outcomes
    )
    execution = SimpleNamespace(
        plan_revision=SimpleNamespace(
            claim_obligations=(obligation,),
            capability_tasks=tasks,
        ),
        capability_outcome_bundles=tuple(
            (None, outcome, (), ()) for outcome in outcome_records
        ),
    )
    bundle = SimpleNamespace(limitation_refs=(limitation_ref,))
    settlement = SimpleNamespace(
        accepted_claim_keys=(),
        accepted_claims=(),
        obligation_coverage=(),
    )
    return execution, bundle, settlement


def _nested_mapping_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(
            *(_nested_mapping_keys(child) for child in value.values())
        )
    if isinstance(value, (list, tuple)):
        return set().union(*(_nested_mapping_keys(child) for child in value))
    return set()


def test_public_limitation_context_is_immutable_and_writer_gets_context_only() -> None:
    source = {
        "failures": (
            {
                "kind": "source_unavailable",
                "business_boundary": "The requested source is unavailable.",
            },
        )
    }
    limitation = PublicLimitation.create(
        limitation_ref="limitation:source-unavailable",
        public_context=source,
    )
    source["failures"][0]["kind"] = "mutated"

    assert limitation.public_context["failures"][0]["kind"] == "source_unavailable"
    assert limitation.to_writer_payload() == {
        "limitation_handle": limitation.limitation_handle,
        "context": canonical_value(limitation.public_context),
    }
    with pytest.raises(TypeError):
        limitation.public_context["failures"] = ()
    with pytest.raises(TypeError):
        limitation.public_context["failures"][0]["kind"] = "mutated"


def test_public_limitation_rejects_empty_and_nested_forbidden_context() -> None:
    with pytest.raises(
        NarrativeAuthorityContractError,
        match="public_limitation_context_invalid",
    ):
        PublicLimitation.create(
            limitation_ref="limitation:empty",
            public_context={},
        )
    for field in ("SQL", "technical_detail_ref", "internal_record_ref"):
        with pytest.raises(
            NarrativeAuthorityContractError,
            match="public_limitation_context_forbidden_field",
        ):
            PublicLimitation.create(
                limitation_ref="limitation:unsafe",
                public_context={"failures": ({field: "restricted:value"},)},
            )


def test_limitation_context_record_order_is_content_stable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        materialization,
        "_validated_authority",
        _identity_authority_validation,
    )
    specs = (
        ("claim-key:z", "comparative_change", "subject:z"),
        ("claim-key:a", "accounting_driver", "subject:a"),
    )
    first_inputs = _context_inputs(specs)
    reordered_inputs = _context_inputs(tuple(reversed(specs)))

    first = build_public_limitation_contexts(*first_inputs, ())
    reordered = build_public_limitation_contexts(*reordered_inputs, ())
    records = canonical_value(first["limitation:shared-boundary"])["claims"]

    assert reordered == first
    assert [canonical_digest(record) for record in records] == sorted(
        canonical_digest(record) for record in records
    )
    assert canonical_value(first["limitation:shared-boundary"])["applicability"] == [
        {
            "affected_claim_kinds": ["accounting_driver", "comparative_change"],
            "scope_effect": "local_claim_family",
        }
    ]


@pytest.mark.parametrize(
    ("limitation_ref", "capability_id"),
    (
        ("no_comparable_periods", "rolling_window_compare"),
        ("future_boundary_code", "future_capability"),
    ),
)
def test_limitation_context_carries_typed_identity_and_outcome_provenance(
    monkeypatch: pytest.MonkeyPatch,
    limitation_ref: str,
    capability_id: str,
) -> None:
    monkeypatch.setattr(
        materialization,
        "_validated_authority",
        _identity_authority_validation,
    )
    inputs = _outcome_context_inputs(
        limitation_ref=limitation_ref,
        outcomes=(("task:source", capability_id, "unavailable"),),
    )

    contexts = build_public_limitation_contexts(*inputs, ())
    context = canonical_value(contexts[limitation_ref])

    assert context["identity"] == [
        {
            "boundary_code": limitation_ref,
        }
    ]
    assert context["outcomes"] == [
        {
            "affected_obligation_ids": ["obligation:comparison-context"],
            "capability_id": capability_id,
            "retryability": "replan_required",
            "status": "unavailable",
        }
    ]
    assert not set(PUBLICATION_FORBIDDEN_FIELDS).intersection(
        _nested_mapping_keys(context)
    )
    with pytest.raises(TypeError):
        contexts[limitation_ref]["identity"][0]["boundary_code"] = "mutated"


def test_context_only_limitation_declares_available_background_evidence_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        materialization,
        "_validated_authority",
        _identity_authority_validation,
    )
    limitation_ref = (
        "contract-gap:dataset:market_dashboard_channel:"
        "evidence_state:context_only:capability:market_channel_context"
    )
    inputs = _outcome_context_inputs(
        limitation_ref=limitation_ref,
        outcomes=(("task:channel", "market_channel_context", "succeeded"),),
    )

    contexts = build_public_limitation_contexts(*inputs, ())
    semantics = canonical_value(contexts[limitation_ref])["business_semantics"]

    assert semantics == [
        {
            "allowed_use": "background_and_candidate_localization",
            "blocked_use": "direct_attribution_or_causal_conclusion",
            "customer_wording_policy": "describe_role_limit_not_missing_data",
            "evidence_role": "background_context",
            "source_availability": "available",
        }
    ]


def test_window_reconciliation_failure_is_projected_as_a_business_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        materialization,
        "_validated_authority",
        _identity_authority_validation,
    )
    limitation_ref = "window_reconciliation_threshold_exceeded"
    inputs = _outcome_context_inputs(
        limitation_ref=limitation_ref,
        outcomes=(("task:reconciliation", "source_reconciliation", "unavailable"),),
    )

    contexts = build_public_limitation_contexts(*inputs, ())
    semantics = canonical_value(contexts[limitation_ref])["business_semantics"]

    assert semantics == [
        {
            "allowed_use": "background_context_only",
            "blocked_use": "partition_contribution_or_ranking",
            "customer_wording_policy": (
                "describe_window_reconciliation_limit_and_keep_other_results"
            ),
            "evidence_role": "unreconciled_partition_context",
            "source_availability": "available",
        }
    ]


def test_limitation_outcome_provenance_order_is_content_stable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        materialization,
        "_validated_authority",
        _identity_authority_validation,
    )
    outcomes = (
        ("task:rolling", "rolling_window_compare", "unavailable"),
        ("task:comparison", "compare_periods", "succeeded"),
    )
    first_inputs = _outcome_context_inputs(
        limitation_ref="no_comparable_periods",
        outcomes=outcomes,
    )
    reordered_inputs = _outcome_context_inputs(
        limitation_ref="no_comparable_periods",
        outcomes=tuple(reversed(outcomes)),
    )

    first = build_public_limitation_contexts(*first_inputs, ())
    reordered = build_public_limitation_contexts(*reordered_inputs, ())
    records = canonical_value(first["no_comparable_periods"])["outcomes"]

    assert reordered == first
    assert [canonical_digest(record) for record in records] == sorted(
        canonical_digest(record) for record in records
    )


def test_limitation_outcome_requires_plan_task_closure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        materialization,
        "_validated_authority",
        _identity_authority_validation,
    )
    execution, bundle, settlement = _outcome_context_inputs(
        limitation_ref="no_comparable_periods",
        outcomes=(("task:missing", "rolling_window_compare", "unavailable"),),
    )
    execution.plan_revision.capability_tasks = ()

    with pytest.raises(
        NarrativeMaterializationContractError,
        match="public_limitation_outcome_task_missing",
    ):
        build_public_limitation_contexts(execution, bundle, settlement, ())


def test_run_scoped_failure_marks_limitation_as_global_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        materialization,
        "_validated_authority",
        _identity_authority_validation,
    )
    execution, bundle, settlement = _context_inputs(
        (("claim-key:change", "comparative_change", "subject:change"),)
    )
    limitation_ref = bundle.limitation_refs[0]
    failure = SimpleNamespace(
        layer="persistence",
        kind="authority_checkpoint_unavailable",
        scope="run",
        integrity_level="shared_authority",
        retryability="never",
        user_actionable=False,
        business_boundary="The run authority could not be established.",
    )
    outcome = SimpleNamespace(
        task_id="task:authority-checkpoint",
        status="technical_failed",
        retryability="never",
        limitation_refs=(limitation_ref,),
        affected_obligation_ids=(),
    )
    execution.plan_revision.capability_tasks = (
        SimpleNamespace(
            task_id=outcome.task_id,
            capability_id="authority_checkpoint",
        ),
    )
    execution.capability_outcome_bundles = ((None, outcome, (), (failure,)),)

    contexts = build_public_limitation_contexts(
        execution,
        bundle,
        settlement,
        (),
    )

    assert canonical_value(contexts[limitation_ref])["applicability"] == [
        {
            "affected_claim_kinds": ["comparative_change"],
            "scope_effect": "global_authority",
        }
    ]


def test_limitation_context_builder_rejects_unexplained_limitation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        materialization,
        "_validated_authority",
        _identity_authority_validation,
    )
    execution, bundle, settlement = _context_inputs(())

    with pytest.raises(
        NarrativeMaterializationContractError,
        match="public_limitation_application_scope_missing",
    ):
        build_public_limitation_contexts(execution, bundle, settlement, ())


def test_boundary_authority_materializes_every_bundle_limitation_context() -> None:
    fixture = _fixture(boundary_only=True)
    semantic = fixture.semantic_result
    policy = PublicationFieldVisibilityPolicy.fixed(
        policy_id="aggregate-answer",
        revision=1,
        restricted_output_policy_ref="test-policy:raw-identifiers",
        restricted_output_policy_version="1",
        restricted_output_fields=("order_id", "user_id"),
    )
    facts = materialize_public_facts(
        authority_bundle=fixture.bundle,
        authority_namespace=semantic.settlement.authority_namespace,
        claims=semantic.settlement.accepted_claims,
        claim_keys=semantic.settlement.accepted_claim_keys,
        support_edges=semantic.settlement.accepted_support_edges,
        evidence_entries=(),
        visibility_policy=policy,
    )
    contexts = build_public_limitation_contexts(
        fixture.execution,
        fixture.bundle,
        semantic.settlement,
        semantic.recommendations,
    )
    reviewed = build_reviewed_public_materialization(
        authority_bundle=fixture.bundle,
        claim_settlement=semantic.settlement,
        public_fact_materialization=facts,
        public_limitation_context_by_ref=contexts,
    )

    assert tuple(contexts) == fixture.bundle.limitation_refs
    assert tuple(item.limitation_ref for item in reviewed.public_limitations) == (
        fixture.bundle.limitation_refs
    )
    assert (
        reviewed.public_limitations[0].public_context
        == contexts["limitation:source-unavailable"]
    )
