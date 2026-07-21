from __future__ import annotations

import pytest

from bi_agent.runtime.narrative_authority import (
    NarrativeBlock,
    NarrativeDocument,
    NarrativeAuthorityContractError,
    NarrativeWriterAttempt,
    PublicationFieldVisibilityPolicy,
    RestrictedProviderResponse,
)
from bi_agent.runtime.publication_safety import (
    FixedSensitiveOutputInspector,
    RestrictedLiteral,
)


def _narrative(text: str) -> NarrativeDocument:
    response = RestrictedProviderResponse.create(
        attempt_id="provider-attempt-1",
        purpose="narrative_writer",
        provider_ref="provider",
        model_ref="model",
        input_ref="narrative-input",
        input_digest="1" * 64,
        attempt_number=1,
        content='{"blocks":[]}',
    )
    attempt = NarrativeWriterAttempt.create(
        authority_bundle_ref="authority-bundle",
        material_projection_ref="narrative-material-projection:test",
        material_projection_digest="2" * 64,
        input_ref="narrative-input",
        input_digest="1" * 64,
        attempt_number=1,
        provider_response=response,
    )
    block = NarrativeBlock.create(
        writer_attempt_id=attempt.attempt_id,
        role="executive_answer",
        text=text,
        claim_handles=("claim-handle",),
        recommendation_handles=(),
        limitation_handles=(),
        material_fact_bindings=(),
        statement_role="answer",
        required=True,
    )
    return NarrativeDocument.create(
        authority_bundle_ref="authority-bundle",
        material_projection_ref="narrative-material-projection:test",
        material_projection_digest="2" * 64,
        writer_attempt=attempt,
        parent_narrative_id=None,
        blocks=(block,),
    )


def _policy() -> PublicationFieldVisibilityPolicy:
    return PublicationFieldVisibilityPolicy.fixed(
        policy_id="aggregate-answer",
        revision=1,
        restricted_output_policy_ref="test-policy:raw-identifiers",
        restricted_output_policy_version="1",
        restricted_output_fields=("order_id", "user_id"),
    )


def test_fixed_inspector_flags_only_exact_registered_material() -> None:
    restricted = RestrictedLiteral.create(
        policy_rule_ref="sensitive-output-policy:source-identifier",
        value="player-9f3a1",
    )
    policy = _policy()
    inspector = FixedSensitiveOutputInspector.create(
        (restricted,),
        visibility_policy=policy,
    )

    findings = inspector(
        narrative=_narrative("The aggregate changed for player-9f3a1."),
        visibility_policy=policy,
    )
    assert len(findings) == 1
    assert findings[0].material_ref == restricted.literal_ref
    assert findings[0].policy_rule_ref == restricted.policy_rule_ref

    assert (
        inspector(
            narrative=_narrative("The aggregate changed for the reviewed cohort."),
            visibility_policy=policy,
        )
        == ()
    )


def test_policy_restricted_fields_are_executable_text_literals() -> None:
    policy = _policy()
    with pytest.raises(
        ValueError,
        match="^restricted_literal_registry_empty$",
    ):
        FixedSensitiveOutputInspector.create((), visibility_policy=policy)

    inspector = FixedSensitiveOutputInspector.from_visibility_policy(policy)
    assert inspector.registry_digest
    assert (
        inspector(
            narrative=_narrative("自由表达仍受 typed claim 和 fact binding 约束。"),
            visibility_policy=policy,
        )
        == ()
    )
    findings = inspector(
        narrative=_narrative("用户 user_id=u-00042 的付费金额为 99 元。"),
        visibility_policy=policy,
    )
    assert len(findings) == 1
    assert findings[0].policy_rule_ref == policy.restricted_output_policy_ref


def test_runtime_restricted_fields_are_hard_public_fact_names() -> None:
    policy = _policy()

    for field in ("order_id", "user_id"):
        with pytest.raises(
            NarrativeAuthorityContractError,
            match="^public_fact_name_forbidden$",
        ):
            policy.assert_public_name(field)


def test_customer_projection_rejects_restricted_fields_inside_text() -> None:
    policy = _policy()
    payload = {
        "blocks": [
            {
                "claim_refs": [],
                "limitation_refs": [],
                "material_fact_bindings": [],
                "recommendation_refs": [],
                "role": "executive_answer",
                "statement_role": "answer",
                "text": "用户 user_id=u-00042 的付费金额为 99 元。",
            }
        ],
        "claim_refs": [],
        "field_visibility_policy_ref": policy.policy_ref,
        "limitation_refs": [],
        "recommendation_refs": [],
        "visualization_refs": [],
        "warnings": [],
    }

    with pytest.raises(
        NarrativeAuthorityContractError,
        match="^publication_customer_payload_restricted_literal$",
    ):
        policy.validate_customer_payload(payload)
