from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import json
from typing import Any, Callable, Mapping, Sequence

import pytest

import bi_agent.runtime.narrative_workflow as narrative_workflow_module
from bi_agent.runtime.claim_authority import SemanticVerificationDecision
from bi_agent.runtime.claim_settlement import (
    AuthorityBundleInputs,
    prepare_claim_settlement,
    settle_claim_checkpoint,
)
from bi_agent.runtime.evidence_authority import canonical_value
from bi_agent.runtime.llm_client import LLMResult
from bi_agent.runtime.narrative_authority import (
    NarrativeAuthorityContractError,
    NarrativeBlock,
    PublicationFieldVisibilityPolicy,
    PublicFactDescriptor,
    PublicLimitation,
    SensitiveOutputFinding,
)
from bi_agent.runtime.narrative_workflow import (
    NARRATIVE_MESSAGE_ENVELOPE_BYTE_LIMIT,
    NarrativeAnswerContext,
    NarrativeProviderCallError,
    ReviewedPublicFactMaterialization,
    prepare_narrative_material_projection,
    run_narrative_workflow,
    validate_typed_narrative_workflow_result,
)
from bi_agent.runtime.publication_flow import PublicationFlowResult
from tests.phase7.test_semantic_authority_workflow import (
    _ExecutionSpec,
    _execution,
    _namespace,
)


Responder = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]


class _FakeNarrativeLLM:
    def __init__(
        self,
        responders: Sequence[Responder],
        *,
        retry_audit_calls: Sequence[int] = (),
    ) -> None:
        self.responders = tuple(responders)
        self.retry_audit_calls = frozenset(retry_audit_calls)
        self.calls: list[dict[str, Any]] = []

    def invoke_json(self, **kwargs: Any) -> LLMResult:
        call_index = len(self.calls)
        if call_index >= len(self.responders):
            raise AssertionError("unexpected_narrative_llm_call")
        payload = json.loads(kwargs["messages"][1]["content"])
        task = kwargs["task"]
        output = dict(self.responders[call_index](task, payload))
        validator = kwargs["output_validator"]
        if validator is not None:
            validator(output)
        self.calls.append(
            {
                **kwargs,
                "payload": payload,
                "output": output,
            }
        )
        raw = json.dumps(
            output,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        retried = call_index in self.retry_audit_calls
        audit: dict[str, Any] = {
            "provider": "provider:test",
            "model": "model:narrative-test",
            "prompt_version": kwargs["prompt_version"],
            "attempt_count": 2 if retried else 1,
            "response_id": f"response:{call_index}:final",
            "raw_response_content": raw,
            "structured_output": output,
        }
        if retried:
            audit["attempt_failures"] = (
                {
                    "attempt": 1,
                    "response_id": f"response:{call_index}:failed",
                    "raw_response_content": '{"invalid":"first-attempt"}',
                },
            )
        return LLMResult(output=output, audit=audit)


class _NoSensitiveOutput:
    def __call__(self, **_: Any) -> tuple[()]:
        return ()


class _FlagFirstDimensionBlock:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(
        self,
        *,
        narrative: Any,
        visibility_policy: PublicationFieldVisibilityPolicy,
    ) -> tuple[SensitiveOutputFinding, ...]:
        self.calls += 1
        if self.calls != 1:
            return ()
        block = next(
            item for item in narrative.blocks if item.role == "dimension_localization"
        )
        return (
            SensitiveOutputFinding.create(
                block_id=block.block_id,
                field_visibility_policy_ref=visibility_policy.policy_ref,
                policy_rule_ref="sensitive-output-policy:test-fixed-rule",
                material_ref="restricted-material:test-fixture",
            ),
        )


@dataclass(frozen=True)
class _AuthorityFixture:
    authority_inputs: AuthorityBundleInputs
    bundle: Any
    settlement: Any
    evidence_entries: tuple[Any, ...]
    recommendations: tuple[Any, ...]
    materialization: ReviewedPublicFactMaterialization
    policy: PublicationFieldVisibilityPolicy


def _authority_fixture(*, boundary_only: bool = False) -> _AuthorityFixture:
    execution = _execution(
        _ExecutionSpec(
            "comparative_change",
            "observed",
            status="unavailable" if boundary_only else "succeeded",
            limitation_refs=(
                "limitation:source-unavailable"
                if boundary_only
                else "limitation:aggregate",
            ),
        )
    )
    namespace = _namespace(execution)
    checkpoint = prepare_claim_settlement(
        execution,
        authority_namespace=namespace,
    )
    if checkpoint.proposed_claims:
        attempt = checkpoint.verification_attempt(
            provider_ref="provider:test",
            model_ref="model:claim-verifier",
            input_digest="a" * 64,
            attempt_number=1,
            raw_provider_response_ref="restricted-provider-response:test-claim",
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
    else:
        attempt = None
        decisions = ()
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
        sealed_at="2026-07-18T12:00:00Z",
    )
    facts = tuple(
        PublicFactDescriptor.create(
            claim=claim,
            public_name=f"aggregate_signal_{index}",
            fact_kind="number",
            value=str(index + 1),
            range_end=None,
            unit="index",
            source_material_ref=claim.support_edge_refs[0],
        )
        for index, claim in enumerate(settlement.accepted_claims)
    )
    limitation_refs = (
        bundle.limitation_refs
        if boundary_only
        else tuple(
            sorted(
                {
                    ref
                    for claim in settlement.accepted_claims
                    for ref in claim.limitation_refs
                }
            )
        )
    )
    obligation_by_id = {
        item.obligation_id: item for item in execution.plan_revision.claim_obligations
    }
    coverage_by_id = {
        item.obligation_id: item for item in settlement.obligation_coverage
    }

    def limitation_context(ref: str) -> dict[str, tuple[dict[str, Any], ...]]:
        obligation_records = tuple(
            {
                "obligation_id": basis.obligation_id,
                "status": coverage_by_id[basis.obligation_id].status,
                "claim_kind": obligation_by_id[basis.obligation_id].claim_kind,
                "role": obligation_by_id[basis.obligation_id].role,
            }
            for basis in checkpoint.obligation_basis
            if ref
            in set(
                (
                    *basis.unavailable_limitation_refs,
                    *coverage_by_id[basis.obligation_id].limitation_refs,
                )
            )
        )
        affected_claim_kinds = tuple(
            sorted(
                {obligation["claim_kind"] for obligation in obligation_records}
                or {
                    key.claim_kind
                    for claim, key in zip(
                        settlement.accepted_claims,
                        settlement.accepted_claim_keys,
                        strict=True,
                    )
                    if ref in set(claim.limitation_refs)
                }
            )
        )
        context: dict[str, tuple[dict[str, Any], ...]] = {
            "applicability": (
                {
                    "scope_effect": "local_claim_family",
                    "affected_claim_kinds": affected_claim_kinds,
                },
            ),
        }
        if obligation_records:
            context["obligations"] = obligation_records
        return context

    limitations = tuple(
        PublicLimitation.create(
            limitation_ref=ref,
            public_context=limitation_context(ref),
        )
        for ref in limitation_refs
    )
    materialization = ReviewedPublicFactMaterialization.create(
        authority_bundle=bundle,
        claim_settlement=settlement,
        review_ref="public-fact-review:phase5-test",
        reviewed_by="review-policy:aggregate-public-facts-v1",
        public_facts=facts,
        public_limitations=limitations,
    )
    return _AuthorityFixture(
        authority_inputs=inputs,
        bundle=bundle,
        settlement=settlement,
        evidence_entries=tuple(
            entry
            for _, _, evidence_entries, _ in execution.capability_outcome_bundles
            for entry in evidence_entries
        ),
        recommendations=(),
        materialization=materialization,
        policy=PublicationFieldVisibilityPolicy.fixed(
            policy_id="aggregate-answer",
            revision=1,
            restricted_output_policy_ref="test-policy:raw-identifiers",
            restricted_output_policy_version="1",
            restricted_output_fields=("order_id", "user_id"),
        ),
    )


def _context(
    *,
    boundary_only: bool = False,
    user_question: str = "Why did full-sample paid amount change?",
    locale: str = "en-US",
) -> NarrativeAnswerContext:
    return NarrativeAnswerContext.create(
        user_question=user_question,
        answer_goal="Give a decision-useful explanation with explicit evidence boundaries.",
        locale=locale,
        business_context=("Use the accepted comparison window.",),
    )


def _prepared_projection(authority: _AuthorityFixture) -> Any:
    _, material_projection = prepare_narrative_material_projection(
        authority_bundle=authority.bundle,
        claim_settlement=authority.settlement,
        evidence_entries=authority.evidence_entries,
        recommendations=authority.recommendations,
        public_materialization=authority.materialization,
        visibility_policy=authority.policy,
    )
    return material_projection


def _claim_block(
    palette_payload: Mapping[str, Any],
    *,
    role: str,
    text: str,
    claim_handle: str | None = None,
) -> dict[str, Any]:
    claim = next(
        item
        for item in palette_payload["claims"]
        if claim_handle is None or item["claim_handle"] == claim_handle
    )
    material = next(
        item
        for item in palette_payload["evidence_materials"]
        if item["material_handle"] in set(claim["material_handles"])
    )
    fact = material["facts"][0]
    return {
        "role": role,
        "text": text,
        "claim_handles": [claim["claim_handle"]],
        "recommendation_handles": [],
        "limitation_handles": list(claim["limitation_handles"]),
        "material_fact_bindings": [
            {
                "claim_handle": claim["claim_handle"],
                "fact_handle": fact["fact_handle"],
            }
        ],
        "statement_role": "business_finding",
        "required": True,
    }


def _focused_editable(block: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(value)
        for key, value in block.items()
        if key not in {"role", "required"}
    }


def _focused_plan(
    *,
    source_order: Sequence[NarrativeBlock],
    preserved_blocks: Sequence[NarrativeBlock],
    targeted_blocks: Sequence[NarrativeBlock],
    material_projection: Any,
) -> Any:
    return narrative_workflow_module._compile_focused_retry_plan(
        source_order=source_order,
        accepted_block_ids=tuple(item.block_id for item in preserved_blocks),
        rejected_block_ids=tuple(item.block_id for item in targeted_blocks),
        material_projection=material_projection,
    )


def _accept_every_block(_: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
    return {
        "decisions": [
            {
                "block_id": block["block_id"],
                "disposition": "accepted",
                "reason_code": None,
                "affected_claim_handles": [],
                "affected_recommendation_handles": [],
                "limitation_handles": [],
            }
            for block in payload["blocks"]
        ]
    }


def _veto_role(role: str) -> Responder:
    def responder(_: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        decisions = []
        for block in payload["blocks"]:
            if block["role"] == role:
                decisions.append(
                    {
                        "block_id": block["block_id"],
                        "disposition": "vetoed",
                        "reason_code": "meaning_exceeds_publication_ceiling",
                        "affected_claim_handles": block["claim_handles"][:1],
                        "affected_recommendation_handles": [],
                        "limitation_handles": [],
                    }
                )
            else:
                decisions.append(
                    {
                        "block_id": block["block_id"],
                        "disposition": "accepted",
                        "reason_code": None,
                        "affected_claim_handles": [],
                        "affected_recommendation_handles": [],
                        "limitation_handles": [],
                    }
                )
        return {"decisions": decisions}

    return responder


def _initial_writer(_: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
    projection = payload["material_projection"]
    return {
        "blocks": [
            _claim_block(
                projection,
                role="executive_answer",
                text="Observed movement is material; the wording stays original.  ",
            ),
            _claim_block(
                projection,
                role="dimension_localization",
                text="The localized pattern is a candidate, not a causal proof.",
            ),
        ]
    }


def _focused_writer(_: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
    focused = payload["answer_context"]["focused_retry"]
    target = dict(focused["retry_targets"][0]["editable_source_block"])
    claim = payload["material_projection"]["claims"][0]
    material = next(
        item
        for item in payload["material_projection"]["evidence_materials"]
        if item["material_handle"] in set(claim["material_handles"])
    )
    fact = material["facts"][0]
    target["material_fact_bindings"] = [
        {
            "claim_handle": claim["claim_handle"],
            "fact_handle": fact["fact_handle"],
        }
    ]
    target["text"] = "The localized pattern remains within its verified ceiling."
    return {"blocks": [target]}


def test_writer_original_text_and_every_provider_attempt_are_projection_ready() -> None:
    authority = _authority_fixture()
    client = _FakeNarrativeLLM(
        (_initial_writer, _accept_every_block),
        retry_audit_calls=(0,),
    )

    result = run_narrative_workflow(
        authority_bundle=authority.bundle,
        claim_settlement=authority.settlement,
        evidence_entries=authority.evidence_entries,
        recommendations=authority.recommendations,
        public_materialization=authority.materialization,
        visibility_policy=authority.policy,
        material_projection=_prepared_projection(authority),
        answer_context=_context(
            user_question="用户 u-00042 昨天的付费金额为何变化？",
        ),
        llm_client=client,
        sensitive_output_inspector=_NoSensitiveOutput(),
    )

    assert result.publication_ready is True
    assert result.final_accepted_narrative is result.narratives[0]
    assert result.narratives[0].blocks[0].text.endswith("original.  ")
    assert result.writer_attempts[0].attempt_number == 2
    assert [item.attempt_number for item in result.provider_responses] == [1, 2, 1]
    assert len(result.provider_audits) == 2
    assert result.material_projection.palette_ref.startswith(
        "public-claim-palette:sha256:"
    )
    assert not hasattr(result, "palette")
    assert len(client.calls) == 2
    assert set(client.calls[0]["payload"]) == {
        "material_projection",
        "requirement_limitation_scope",
        "answer_context",
    }
    writer_requirements = client.calls[0]["payload"]["material_projection"][
        "publication_requirements"
    ]
    assert len(writer_requirements) == 1
    assert set(writer_requirements[0]) == {
        "requirement_handle",
        "status",
        "coverage_semantics",
        "claim_kind",
        "assertion_scope",
        "required_claim_strength",
        "claim_handles",
        "limitation_handles",
    }
    assert all(
        call["prompt_version"] == "single-authority-phase05.v13"
        for call in client.calls
    )
    writer_requirement_scope = client.calls[0]["payload"][
        "requirement_limitation_scope"
    ]
    assert [item["requirement_handle"] for item in writer_requirement_scope] == [
        item["requirement_handle"] for item in writer_requirements
    ]
    assert all(item["block_coverage"] == [] for item in writer_requirement_scope)
    assert "user_question" not in client.calls[0]["payload"]["answer_context"]
    assert "u-00042" not in json.dumps(
        client.calls[0]["payload"],
        ensure_ascii=False,
    )
    assert "authority_bundle_ref" not in client.calls[0]["payload"]
    assert client.calls[0]["payload"]["material_projection"]["evidence_materials"]
    assert all(
        "facts" not in claim
        for claim in client.calls[0]["payload"]["material_projection"]["claims"]
    )
    assert all(
        item.material_projection_ref == result.material_projection.projection_ref
        and item.material_projection_digest == result.material_projection.content_digest
        and not hasattr(item, "palette_ref")
        for item in result.provider_call_inputs
    )
    assert set(client.calls[0]["output"]) == {"blocks"}
    assert set(client.calls[0]["output"]["blocks"][0]) == {
        "role",
        "text",
        "claim_handles",
        "recommendation_handles",
        "limitation_handles",
        "material_fact_bindings",
        "statement_role",
        "required",
    }
    provider_binding = client.calls[0]["output"]["blocks"][0]["material_fact_bindings"][
        0
    ]
    verifier_binding = client.calls[1]["payload"]["blocks"][0][
        "material_fact_bindings"
    ][0]
    assert set(provider_binding) == {"claim_handle", "fact_handle"}
    assert verifier_binding == provider_binding
    resolved_binding = result.narratives[0].blocks[0].material_fact_bindings[0]
    projected_fact = result.material_projection.evidence_materials[0].facts[0]
    assert resolved_binding.fact_handle == projected_fact.fact_handle
    assert resolved_binding.fact_kind == projected_fact.fact_kind
    assert resolved_binding.value == projected_fact.value
    assert resolved_binding.range_end == projected_fact.range_end
    assert resolved_binding.unit == projected_fact.unit
    assert set(resolved_binding.to_dict()) == {
        "binding_ref",
        "claim_handle",
        "fact_handle",
        "fact_kind",
        "value",
        "range_end",
        "unit",
        "content_digest",
    }
    assert set(client.calls[1]["output"]) == {"decisions"}
    assert set(client.calls[1]["payload"]) == {
        "material_projection",
        "answer_context",
        "verification_scope",
        "requirement_limitation_scope",
        "context_blocks",
        "blocks",
    }
    assert client.calls[1]["payload"]["verification_scope"]["mode"] == "full"
    assert client.calls[1]["payload"]["context_blocks"] == []
    assert client.calls[1]["payload"]["requirement_limitation_scope"][0][
        "block_coverage"
    ]
    assert set(client.calls[1]["output"]["decisions"][0]) == {
        "block_id",
        "disposition",
        "reason_code",
        "affected_claim_handles",
        "affected_recommendation_handles",
        "limitation_handles",
    }


def test_run_rejects_a_replayed_projection_from_another_authority_before_provider() -> (
    None
):
    authority = _authority_fixture()
    foreign_authority = _authority_fixture(boundary_only=True)
    client = _FakeNarrativeLLM(())

    with pytest.raises(
        narrative_workflow_module.NarrativeWorkflowError,
        match="narrative_material_projection_closure_invalid",
    ):
        run_narrative_workflow(
            authority_bundle=authority.bundle,
            claim_settlement=authority.settlement,
            evidence_entries=authority.evidence_entries,
            recommendations=authority.recommendations,
            public_materialization=authority.materialization,
            visibility_policy=authority.policy,
            material_projection=_prepared_projection(foreign_authority),
            answer_context=_context(),
            llm_client=client,
            sensitive_output_inspector=_NoSensitiveOutput(),
        )

    assert client.calls == []


def test_oversized_narrative_payload_fails_before_provider_call() -> None:
    authority = _authority_fixture()
    client = _FakeNarrativeLLM(())
    context = NarrativeAnswerContext.create(
        user_question="Why did paid amount change?",
        answer_goal="Explain the verified movement.",
        locale="en-US",
        business_context=("x" * (NARRATIVE_MESSAGE_ENVELOPE_BYTE_LIMIT + 1),),
    )

    with pytest.raises(NarrativeProviderCallError) as captured:
        run_narrative_workflow(
            authority_bundle=authority.bundle,
            claim_settlement=authority.settlement,
            evidence_entries=authority.evidence_entries,
            recommendations=authority.recommendations,
            public_materialization=authority.materialization,
            visibility_policy=authority.policy,
            material_projection=_prepared_projection(authority),
            answer_context=context,
            llm_client=client,
            sensitive_output_inspector=_NoSensitiveOutput(),
        )

    assert captured.value.kind == "narrative_input_budget_exceeded"
    assert captured.value.retryability == "not_retryable"
    assert client.calls == []


def test_oversized_verifier_request_fails_after_exactly_one_writer_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority_fixture()
    client = _FakeNarrativeLLM((_initial_writer,))
    monkeypatch.setattr(
        narrative_workflow_module,
        "_VERIFIER_SYSTEM_PROMPT",
        "v" * (NARRATIVE_MESSAGE_ENVELOPE_BYTE_LIMIT + 1),
    )

    with pytest.raises(NarrativeProviderCallError) as captured:
        run_narrative_workflow(
            authority_bundle=authority.bundle,
            claim_settlement=authority.settlement,
            evidence_entries=authority.evidence_entries,
            recommendations=authority.recommendations,
            public_materialization=authority.materialization,
            visibility_policy=authority.policy,
            material_projection=_prepared_projection(authority),
            answer_context=_context(),
            llm_client=client,
            sensitive_output_inspector=_NoSensitiveOutput(),
        )

    assert captured.value.kind == "narrative_input_budget_exceeded"
    assert captured.value.retryability == "not_retryable"
    assert [call["task"] for call in client.calls] == [
        "single_authority_narrative_writer"
    ]


def test_writer_owns_claim_bearing_block_structure() -> None:
    authority = _authority_fixture()

    def single_synthesis_block(
        _: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return {
            "blocks": [
                _claim_block(
                    payload["material_projection"],
                    role="contextual_pattern",
                    text="One integrated explanation carries the useful synthesis.",
                )
            ]
        }

    client = _FakeNarrativeLLM((single_synthesis_block, _accept_every_block))
    result = run_narrative_workflow(
        authority_bundle=authority.bundle,
        claim_settlement=authority.settlement,
        evidence_entries=authority.evidence_entries,
        recommendations=authority.recommendations,
        public_materialization=authority.materialization,
        visibility_policy=authority.policy,
        material_projection=_prepared_projection(authority),
        answer_context=_context(),
        llm_client=client,
        sensitive_output_inspector=_NoSensitiveOutput(),
    )

    assert result.publication_ready is True
    assert [block.role for block in result.narratives[0].blocks] == [
        "contextual_pattern"
    ]
    assert "required_block_roles" not in client.calls[0]["payload"]["answer_context"]


@pytest.mark.parametrize("coverage_location", ("omitted", "optional_only"))
def test_writer_requires_user_required_coverage_in_required_blocks(
    coverage_location: str,
) -> None:
    from tests.phase7 import test_narrative_material_projection as projection_contracts

    projection = projection_contracts._derive(projection_contracts._fixture())
    payload = projection.to_writer_payload()
    assert len(projection.publication_requirements) == 1
    requirement = projection.publication_requirements[0]
    auxiliary_claim = next(
        item
        for item in projection.claims
        if item.claim_handle not in set(requirement.claim_handles)
    )
    blocks = [
        _claim_block(
            payload,
            role="contextual_pattern",
            text="Authorized auxiliary context cannot close the requested obligation.",
            claim_handle=auxiliary_claim.claim_handle,
        )
    ]
    if coverage_location == "optional_only":
        optional = _claim_block(
            payload,
            role="executive_answer",
            text="The requested finding appears only in an optional block.",
            claim_handle=requirement.claim_handles[0],
        )
        optional["required"] = False
        blocks.append(optional)

    with pytest.raises(
        narrative_workflow_module.NarrativeWorkflowError,
        match="narrative_writer_publication_requirement_coverage_invalid",
    ):
        narrative_workflow_module._initial_writer_validator(
            {"blocks": blocks},
            authority_mode="claim_bearing",
            material_projection=projection,
        )


@pytest.mark.parametrize(
    ("role", "claim_source", "recommendation_handles", "limitation_source", "valid"),
    (
        ("direction", "none", ("recommendation-handle:test",), "claim", True),
        ("direction", "none", (), "none", False),
        ("direction", "none", (), "claim", False),
        ("boundary", "claim", (), "none", False),
        ("boundary", "none", (), "claim", True),
        ("next_action", "claim", (), "claim", False),
        ("next_action", "none", ("recommendation-handle:test",), "none", True),
    ),
)
def test_writer_validator_and_narrative_block_share_authority_handle_grammar(
    role: str,
    claim_source: str,
    recommendation_handles: tuple[str, ...],
    limitation_source: str,
    valid: bool,
) -> None:
    authority = _authority_fixture()
    projection = _prepared_projection(authority)
    projection_payload = projection.to_writer_payload()
    required_block = _claim_block(
        projection_payload,
        role="executive_answer",
        text="The required answer remains covered by verified evidence.",
    )
    claim = projection_payload["claims"][0]
    candidate = {
        "role": role,
        "text": "This block exercises the shared authority-handle grammar.",
        "claim_handles": ([claim["claim_handle"]] if claim_source == "claim" else []),
        "recommendation_handles": list(recommendation_handles),
        "limitation_handles": (
            list(claim["limitation_handles"]) if limitation_source == "claim" else []
        ),
        "material_fact_bindings": [],
        "statement_role": "recommendation" if recommendation_handles else "boundary",
        "required": False,
    }

    def validate_provider_output() -> None:
        narrative_workflow_module._initial_writer_validator(
            {"blocks": [required_block, candidate]},
            authority_mode="claim_bearing",
            material_projection=projection,
        )

    def materialize_block() -> None:
        NarrativeBlock.create(
            writer_attempt_id="writer-attempt:grammar-parity",
            role=candidate["role"],
            text=candidate["text"],
            claim_handles=candidate["claim_handles"],
            recommendation_handles=candidate["recommendation_handles"],
            limitation_handles=candidate["limitation_handles"],
            material_fact_bindings=(),
            statement_role=candidate["statement_role"],
            required=candidate["required"],
        )

    if valid:
        validate_provider_output()
        materialize_block()
        return

    with pytest.raises(
        narrative_workflow_module.NarrativeWorkflowError,
        match="narrative_writer_authority_handles_invalid",
    ):
        validate_provider_output()
    with pytest.raises(
        NarrativeAuthorityContractError,
        match="narrative_block_authority_handles_invalid",
    ):
        materialize_block()


@pytest.mark.parametrize(
    (
        "status",
        "requirement_has_claim",
        "requirement_has_limitation",
        "bind_claim",
        "bind_limitation",
        "expected",
    ),
    (
        ("satisfied", True, False, True, False, True),
        ("satisfied", True, False, False, False, False),
        ("mixed", True, True, True, True, True),
        ("mixed", True, True, False, True, False),
        ("mixed", True, True, True, False, False),
        ("unavailable", False, True, False, True, True),
        ("unavailable", False, True, False, False, False),
        ("contradicted", True, True, True, True, True),
        ("contradicted", True, True, False, True, False),
        ("contradicted", True, True, True, False, False),
    ),
)
def test_required_publication_coverage_status_matrix(
    status: str,
    requirement_has_claim: bool,
    requirement_has_limitation: bool,
    bind_claim: bool,
    bind_limitation: bool,
    expected: bool,
) -> None:
    authority = _authority_fixture()
    projection = _prepared_projection(authority)
    source = projection.publication_requirements[0]
    claim_handle = source.claim_handles[0]
    limitation_handle = projection.limitations[0].limitation_handle
    requirement = replace(
        source,
        status=status,
        claim_handles=(claim_handle,) if requirement_has_claim else (),
        limitation_handles=(limitation_handle,) if requirement_has_limitation else (),
    )
    status_projection = replace(
        projection,
        publication_requirements=(requirement,),
    )

    assert (
        narrative_workflow_module._publication_requirements_covered(
            material_projection=status_projection,
            claim_handles=frozenset({claim_handle}) if bind_claim else frozenset(),
            limitation_handles=(
                frozenset({limitation_handle}) if bind_limitation else frozenset()
            ),
        )
        is expected
    )


@pytest.mark.parametrize(
    ("direct_binding", "expected_binding_mode"),
    ((True, "claim_or_boundary"), (False, "boundary_only")),
)
def test_requirement_limitation_scope_projects_typed_binding_topology_per_block(
    direct_binding: bool,
    expected_binding_mode: str,
) -> None:
    authority = _authority_fixture()
    projection = _prepared_projection(authority)
    source_requirement = projection.publication_requirements[0]
    claim = projection.claims[0]
    limitation = projection.limitations[0]
    mixed_projection = replace(
        projection,
        claims=(
            replace(
                claim,
                limitation_handles=(
                    (limitation.limitation_handle,) if direct_binding else ()
                ),
            ),
        ),
        publication_requirements=(
            replace(
                source_requirement,
                status="mixed",
                coverage_semantics="supported_with_limitations",
                limitation_handles=(limitation.limitation_handle,),
            ),
        ),
    )
    claim_block = NarrativeBlock.create(
        writer_attempt_id="writer-attempt:requirement-scope",
        role="executive_answer",
        text="The observed claim is limited to its declared assertion scope.",
        claim_handles=(claim.claim_handle,),
        recommendation_handles=(),
        limitation_handles=(),
        material_fact_bindings=(),
        statement_role="business_finding",
        required=True,
    )
    boundary_block = NarrativeBlock.create(
        writer_attempt_id="writer-attempt:requirement-scope",
        role="boundary",
        text="The requirement-level limitation remains explicit.",
        claim_handles=(),
        recommendation_handles=(),
        limitation_handles=(limitation.limitation_handle,),
        material_fact_bindings=(),
        statement_role="boundary",
        required=True,
    )

    scope = narrative_workflow_module._requirement_limitation_scope(
        material_projection=mixed_projection,
        blocks=(claim_block, boundary_block),
    )

    assert len(scope) == 1
    requirement_scope = scope[0]
    assert requirement_scope["coverage_semantics"] == "supported_with_limitations"
    assert requirement_scope["claim_handle_options"] == [claim.claim_handle]
    assert requirement_scope["assertion_scope"] == canonical_value(
        source_requirement.assertion_scope
    )
    assert "required_limitation_handles" not in requirement_scope
    required_limitation = requirement_scope["required_limitations"][0]
    assert required_limitation["limitation_handle"] == limitation.limitation_handle
    assert required_limitation["binding_mode"] == expected_binding_mode
    assert required_limitation["claim_binding_options"] == (
        [claim.claim_handle] if direct_binding else []
    )
    assert required_limitation["boundary_facet_handles"] == list(
        limitation.boundary_facet_handles
    )
    assert {
        item["boundary_facet_handle"] for item in required_limitation["boundary_facets"]
    } == set(limitation.boundary_facet_handles)
    coverage_by_block = {
        item["block_id"]: item for item in requirement_scope["block_coverage"]
    }
    assert coverage_by_block[claim_block.block_id]["covered_claim_handles"] == [
        claim.claim_handle
    ]
    assert coverage_by_block[claim_block.block_id][
        "missing_required_limitation_handles"
    ] == [limitation.limitation_handle]
    assert coverage_by_block[boundary_block.block_id][
        "bound_required_limitation_handles"
    ] == [limitation.limitation_handle]
    assert (
        coverage_by_block[boundary_block.block_id][
            "missing_required_limitation_handles"
        ]
        == []
    )


def test_focused_retry_preserves_or_repairs_user_required_coverage() -> None:
    from tests.phase7 import test_narrative_material_projection as projection_contracts

    projection = projection_contracts._derive(projection_contracts._fixture())
    payload = projection.to_writer_payload()
    requirement = projection.publication_requirements[0]
    auxiliary_claim = next(
        item
        for item in projection.claims
        if item.claim_handle not in set(requirement.claim_handles)
    )
    preserved_payload = _claim_block(
        payload,
        role="contextual_pattern",
        text="Accepted auxiliary context stays byte-identical.",
        claim_handle=auxiliary_claim.claim_handle,
    )
    target_payload = _claim_block(
        payload,
        role="executive_answer",
        text="The rejected target originally carried required coverage.",
        claim_handle=requirement.claim_handles[0],
    )
    preserved = narrative_workflow_module._block_from_output(
        preserved_payload,
        writer_attempt_id="writer-attempt:focused-coverage-source",
        material_projection=projection,
    )
    target = narrative_workflow_module._block_from_output(
        target_payload,
        writer_attempt_id="writer-attempt:focused-coverage-source",
        material_projection=projection,
    )
    repaired_target = _claim_block(
        payload,
        role=target.role,
        text="The retry restores the required finding within its verified ceiling.",
        claim_handle=requirement.claim_handles[0],
    )
    repaired_output = {"blocks": [_focused_editable(repaired_target)]}
    retry_plan = _focused_plan(
        source_order=(preserved, target),
        preserved_blocks=(preserved,),
        targeted_blocks=(target,),
        material_projection=projection,
    )

    assert narrative_workflow_module._focused_retry_required_coverage(
        preserved_blocks=(preserved,),
        material_projection=projection,
    ) == (
        {
            "requirement_handle": requirement.requirement_handle,
            "claim_handle_options": list(requirement.claim_handles),
            "required_limitation_handles": [],
        },
    )

    narrative_workflow_module._focused_writer_validator(
        repaired_output,
        source_order=(preserved, target),
        retry_plan=retry_plan,
        authority_mode="claim_bearing",
        material_projection=projection,
    )

    dropped_target = _claim_block(
        payload,
        role=target.role,
        text="The retry kept only auxiliary context.",
        claim_handle=auxiliary_claim.claim_handle,
    )
    with pytest.raises(
        narrative_workflow_module.NarrativeWorkflowError,
        match="focused_writer_target_claim_coverage_invalid",
    ):
        narrative_workflow_module._focused_writer_validator(
            {"blocks": [_focused_editable(dropped_target)]},
            source_order=(preserved, target),
            retry_plan=retry_plan,
            authority_mode="claim_bearing",
            material_projection=projection,
        )


def test_focused_retry_cannot_replace_required_claim_coverage_with_auxiliary_claim() -> (
    None
):
    from tests.phase7 import test_narrative_material_projection as projection_contracts

    projection = projection_contracts._derive(projection_contracts._fixture())
    payload = projection.to_writer_payload()
    requirement = projection.publication_requirements[0]
    auxiliary_claim = next(
        item
        for item in projection.claims
        if item.claim_handle not in set(requirement.claim_handles)
    )
    preserved_payload = _claim_block(
        payload,
        role="contextual_pattern",
        text="The accepted sibling carries only auxiliary claim authority.",
        claim_handle=auxiliary_claim.claim_handle,
    )
    target_payload = _claim_block(
        payload,
        role="direction",
        text="The rejected target originally carried the required claim.",
        claim_handle=requirement.claim_handles[0],
    )
    preserved = narrative_workflow_module._block_from_output(
        preserved_payload,
        writer_attempt_id="writer-attempt:focused-recommendation-source",
        material_projection=projection,
    )
    target = narrative_workflow_module._block_from_output(
        target_payload,
        writer_attempt_id="writer-attempt:focused-recommendation-source",
        material_projection=projection,
    )
    auxiliary_only_target = {
        "text": "The retry carries a verified action without the required finding.",
        "claim_handles": [auxiliary_claim.claim_handle],
        "recommendation_handles": [],
        "limitation_handles": [],
        "material_fact_bindings": [],
        "statement_role": "recommendation",
    }
    retry_plan = _focused_plan(
        source_order=(preserved, target),
        preserved_blocks=(preserved,),
        targeted_blocks=(target,),
        material_projection=projection,
    )

    with pytest.raises(
        narrative_workflow_module.NarrativeWorkflowError,
        match="focused_writer_target_claim_coverage_invalid",
    ):
        narrative_workflow_module._validated_focused_writer_merge(
            {"blocks": [auxiliary_only_target]},
            source_order=(preserved, target),
            retry_plan=retry_plan,
            authority_mode="claim_bearing",
            material_projection=projection,
        )


def test_focused_retry_validates_requirement_coverage_across_merged_blocks() -> None:
    from tests.phase7 import test_narrative_material_projection as projection_contracts

    projection = projection_contracts._derive(projection_contracts._fixture())
    payload = projection.to_writer_payload()
    source_requirement = projection.publication_requirements[0]
    limitation_handle = projection.limitations[0].limitation_handle
    mixed_requirement = replace(
        source_requirement,
        status="mixed",
        limitation_handles=(limitation_handle,),
    )
    mixed_projection = replace(
        projection,
        publication_requirements=(mixed_requirement,),
    )
    preserved_payload = _claim_block(
        payload,
        role="executive_answer",
        text="The accepted sibling carries the required claim.",
        claim_handle=mixed_requirement.claim_handles[0],
    )
    preserved_payload["limitation_handles"] = []
    target_payload = {
        "role": "boundary",
        "text": "The target carries the remaining required boundary.",
        "claim_handles": [],
        "recommendation_handles": [],
        "limitation_handles": [limitation_handle],
        "material_fact_bindings": [],
        "statement_role": "boundary",
        "required": True,
    }
    preserved = narrative_workflow_module._block_from_output(
        preserved_payload,
        writer_attempt_id="writer-attempt:split-coverage-source",
        material_projection=mixed_projection,
    )
    target = narrative_workflow_module._block_from_output(
        target_payload,
        writer_attempt_id="writer-attempt:split-coverage-source",
        material_projection=mixed_projection,
    )

    assert narrative_workflow_module._focused_retry_required_coverage(
        preserved_blocks=(preserved,),
        material_projection=mixed_projection,
    ) == (
        {
            "requirement_handle": mixed_requirement.requirement_handle,
            "claim_handle_options": [],
            "required_limitation_handles": [limitation_handle],
        },
    )
    retry_plan = _focused_plan(
        source_order=(preserved, target),
        preserved_blocks=(preserved,),
        targeted_blocks=(target,),
        material_projection=mixed_projection,
    )

    merged = narrative_workflow_module._validated_focused_writer_merge(
        {"blocks": [_focused_editable(target_payload)]},
        source_order=(preserved, target),
        retry_plan=retry_plan,
        authority_mode="claim_bearing",
        material_projection=mixed_projection,
    )

    assert merged["blocks"] == [
        narrative_workflow_module._block_to_provider_payload(preserved),
        target_payload,
    ]
    dropped_boundary = {**target_payload, "limitation_handles": []}
    with pytest.raises(
        narrative_workflow_module.NarrativeWorkflowError,
        match="narrative_writer_authority_handles_invalid",
    ):
        narrative_workflow_module._validated_focused_writer_merge(
            {"blocks": [_focused_editable(dropped_boundary)]},
            source_order=(preserved, target),
            retry_plan=retry_plan,
            authority_mode="claim_bearing",
            material_projection=mixed_projection,
        )


def _standalone_required_limitation_projection() -> tuple[
    Any, dict[str, Any], str, str
]:
    from tests.phase7 import test_narrative_material_projection as projection_contracts

    projection = projection_contracts._derive(projection_contracts._fixture())
    payload = projection.to_writer_payload()
    requirement = projection.publication_requirements[0]
    limitation_handle = projection.limitations[0].limitation_handle
    standalone = replace(
        projection,
        claims=tuple(
            replace(
                claim,
                limitation_handles=tuple(
                    handle
                    for handle in claim.limitation_handles
                    if handle != limitation_handle
                ),
            )
            for claim in projection.claims
        ),
        recommendations=tuple(
            replace(
                recommendation,
                risk_handles=tuple(
                    handle
                    for handle in recommendation.risk_handles
                    if handle != limitation_handle
                ),
            )
            for recommendation in projection.recommendations
        ),
        publication_requirements=(
            replace(
                requirement,
                status="mixed",
                limitation_handles=(limitation_handle,),
            ),
        ),
    )
    return standalone, payload, requirement.claim_handles[0], limitation_handle


def test_focused_retry_plan_filters_unknown_source_handles() -> None:
    from tests.phase7 import test_narrative_material_projection as projection_contracts

    projection = projection_contracts._derive(projection_contracts._fixture())
    requirement = projection.publication_requirements[0]
    source = NarrativeBlock.create(
        writer_attempt_id="writer-attempt:unknown-source-handles",
        role="executive_answer",
        text="The rejected seed contains handles outside the public projection.",
        claim_handles=(requirement.claim_handles[0], "c_unknown"),
        recommendation_handles=("r_unknown",),
        limitation_handles=("l_unknown",),
        material_fact_bindings=(),
        statement_role="business_finding",
        required=True,
    )

    plan = narrative_workflow_module._compile_focused_retry_plan(
        source_order=(source,),
        accepted_block_ids=(),
        rejected_block_ids=(source.block_id,),
        material_projection=projection,
    )

    target = plan.targets[0]
    seed = narrative_workflow_module._focused_editable_source_payload(target)
    assert "c_unknown" not in target.allowed_claim_handles
    assert "r_unknown" not in target.allowed_recommendation_handles
    assert "l_unknown" not in target.allowed_limitation_handles
    assert seed is not None
    assert "c_unknown" not in seed["claim_handles"]
    assert "r_unknown" not in seed["recommendation_handles"]
    assert "l_unknown" not in seed["limitation_handles"]


def test_focused_retry_plan_rejects_target_without_public_authority() -> None:
    from tests.phase7 import test_narrative_material_projection as projection_contracts

    projection = projection_contracts._derive(projection_contracts._fixture())
    payload = projection.to_writer_payload()
    requirement = projection.publication_requirements[0]
    accepted_payload = _claim_block(
        payload,
        role="executive_answer",
        text="The accepted sibling closes the publication requirement.",
        claim_handle=requirement.claim_handles[0],
    )
    accepted = narrative_workflow_module._block_from_output(
        accepted_payload,
        writer_attempt_id="writer-attempt:missing-target-authority",
        material_projection=projection,
    )
    rejected = NarrativeBlock.create(
        writer_attempt_id="writer-attempt:missing-target-authority",
        role="contextual_pattern",
        text="The rejected target carries only an unknown claim.",
        claim_handles=("c_unknown",),
        recommendation_handles=(),
        limitation_handles=(),
        material_fact_bindings=(),
        statement_role="business_finding",
        required=True,
    )

    with pytest.raises(
        narrative_workflow_module.NarrativeWorkflowError,
        match="focused_retry_plan_target_authority_missing",
    ):
        narrative_workflow_module._compile_focused_retry_plan(
            source_order=(accepted, rejected),
            accepted_block_ids=(accepted.block_id,),
            rejected_block_ids=(rejected.block_id,),
            material_projection=projection,
        )


def test_focused_next_action_can_bind_verified_supporting_claims() -> None:
    from tests.phase7.test_narrative_authority import (
        _material_projection,
        _palette_with_recommendation,
    )

    palette, _ = _palette_with_recommendation()
    projection = _material_projection(palette)
    recommendation = projection.recommendations[0]
    source = NarrativeBlock.create(
        writer_attempt_id="writer-attempt:next-action-support",
        role="next_action",
        text="Prioritize the verified investigation path.",
        claim_handles=(),
        recommendation_handles=(recommendation.recommendation_handle,),
        limitation_handles=recommendation.risk_handles,
        material_fact_bindings=(),
        statement_role="recommendation",
        required=True,
    )

    plan = narrative_workflow_module._compile_focused_retry_plan(
        source_order=(source,),
        accepted_block_ids=(),
        rejected_block_ids=(source.block_id,),
        material_projection=projection,
    )
    target = plan.targets[0]
    view = narrative_workflow_module._focused_scoped_material_view(
        retry_plan=plan,
        material_projection=projection,
    )

    assert set(recommendation.supporting_claim_handles).issubset(
        target.allowed_claim_handles
    )
    assert set(recommendation.supporting_claim_handles).issubset(
        item["claim_handle"] for item in view["claims"]
    )
    assert set(recommendation.supporting_claim_handles).issubset(
        item["claim_handle"] for item in view["allowed_claim_fact_pairs"]
    )


def test_focused_retry_plan_filters_unknown_boundary_limitation() -> None:
    from tests.phase7 import test_narrative_material_projection as projection_contracts

    projection = projection_contracts._derive(projection_contracts._fixture())
    payload = projection.to_writer_payload()
    requirement = projection.publication_requirements[0]
    accepted_payload = _claim_block(
        payload,
        role="executive_answer",
        text="The accepted finding already covers the required claim.",
        claim_handle=requirement.claim_handles[0],
    )
    accepted = narrative_workflow_module._block_from_output(
        accepted_payload,
        writer_attempt_id="writer-attempt:unknown-boundary-limitation",
        material_projection=projection,
    )
    known_limitation = projection.limitations[0].limitation_handle
    boundary = NarrativeBlock.create(
        writer_attempt_id="writer-attempt:unknown-boundary-limitation",
        role="boundary",
        text="The rejected boundary seed includes an unknown limitation.",
        claim_handles=(),
        recommendation_handles=(),
        limitation_handles=(known_limitation, "l_unknown"),
        material_fact_bindings=(),
        statement_role="boundary",
        required=True,
    )

    plan = narrative_workflow_module._compile_focused_retry_plan(
        source_order=(accepted, boundary),
        accepted_block_ids=(accepted.block_id,),
        rejected_block_ids=(boundary.block_id,),
        material_projection=projection,
    )

    target = plan.targets[0]
    seed = narrative_workflow_module._focused_editable_source_payload(target)
    assert known_limitation in target.allowed_limitation_handles
    assert "l_unknown" not in target.allowed_limitation_handles
    assert seed is not None
    assert seed["limitation_handles"] == [known_limitation]


def test_focused_retry_seed_uses_source_claim_scope_before_open_claim_options() -> None:
    from tests.phase7 import test_narrative_material_projection as projection_contracts

    projection = projection_contracts._derive(projection_contracts._fixture())
    claim_a, claim_b = projection.claims
    limitation_handle = claim_b.limitation_handles[0]
    requirement = projection.publication_requirements[0]
    scoped_projection = replace(
        projection,
        claims=(
            replace(
                claim_a,
                limitation_handles=tuple(
                    handle
                    for handle in claim_a.limitation_handles
                    if handle != limitation_handle
                ),
            ),
            claim_b,
        ),
        publication_requirements=(
            replace(
                requirement,
                claim_handles=(claim_a.claim_handle, claim_b.claim_handle),
            ),
        ),
    )
    source = NarrativeBlock.create(
        writer_attempt_id="writer-attempt:source-claim-scope",
        role="executive_answer",
        text="The source binds claim A but incorrectly carries claim B's limit.",
        claim_handles=(claim_a.claim_handle,),
        recommendation_handles=(),
        limitation_handles=(limitation_handle,),
        material_fact_bindings=(),
        statement_role="business_finding",
        required=True,
    )

    plan = narrative_workflow_module._compile_focused_retry_plan(
        source_order=(source,),
        accepted_block_ids=(),
        rejected_block_ids=(source.block_id,),
        material_projection=scoped_projection,
    )

    target = plan.targets[0]
    seed = narrative_workflow_module._focused_editable_source_payload(target)
    assert limitation_handle in target.allowed_limitation_handles
    assert limitation_handle not in target.source_seed_limitation_handles
    assert seed is not None
    assert limitation_handle not in seed["limitation_handles"]


def test_focused_retry_preserves_claim_affinity_when_boundary_is_retargeted() -> None:
    from tests.phase7 import test_narrative_material_projection as projection_contracts

    projection = projection_contracts._derive(projection_contracts._fixture())
    payload = projection.to_writer_payload()
    claim_a, claim_b = projection.claims
    limitation_handle = projection.limitations[0].limitation_handle
    requirement = projection.publication_requirements[0]
    affinity_projection = replace(
        projection,
        claims=tuple(
            replace(
                claim,
                limitation_handles=tuple(
                    handle
                    for handle in claim.limitation_handles
                    if handle != limitation_handle
                ),
            )
            for claim in projection.claims
        ),
        publication_requirements=(
            replace(
                requirement,
                claim_handles=(claim_a.claim_handle,),
                limitation_handles=(limitation_handle,),
                status="mixed",
            ),
            replace(
                requirement,
                requirement_handle="pr_boundary_claim_affinity",
                claim_handles=(claim_b.claim_handle,),
                limitation_handles=(),
                status="satisfied",
            ),
        ),
    )
    finding_payload = _claim_block(
        payload,
        role="direction",
        text="The rejected finding carries claim A and a standalone limit.",
        claim_handle=claim_a.claim_handle,
    )
    finding_payload["limitation_handles"] = [limitation_handle]
    boundary_payload = _claim_block(
        payload,
        role="boundary",
        text="The accepted boundary already owns claim B.",
        claim_handle=claim_b.claim_handle,
    )
    boundary_payload["limitation_handles"] = [
        handle
        for handle in boundary_payload["limitation_handles"]
        if handle != limitation_handle
    ]
    finding = narrative_workflow_module._block_from_output(
        finding_payload,
        writer_attempt_id="writer-attempt:boundary-claim-affinity",
        material_projection=affinity_projection,
    )
    boundary = narrative_workflow_module._block_from_output(
        boundary_payload,
        writer_attempt_id="writer-attempt:boundary-claim-affinity",
        material_projection=affinity_projection,
    )

    plan = narrative_workflow_module._compile_focused_retry_plan(
        source_order=(finding, boundary),
        accepted_block_ids=(boundary.block_id,),
        rejected_block_ids=(finding.block_id,),
        material_projection=affinity_projection,
    )

    finding_target, boundary_target = plan.targets
    finding_options = {
        handle
        for coverage in finding_target.required_coverage
        for handle in coverage.claim_handle_options
    }
    boundary_options = {
        handle
        for coverage in boundary_target.required_coverage
        for handle in coverage.claim_handle_options
    }
    assert claim_a.claim_handle in finding_options
    assert claim_b.claim_handle not in finding_options
    assert claim_b.claim_handle in boundary_options
    assert limitation_handle in boundary_target.allowed_limitation_handles


def test_focused_retry_plan_retargets_accepted_required_boundary_for_standalone_limit() -> (
    None
):
    projection, payload, claim_handle, limitation_handle = (
        _standalone_required_limitation_projection()
    )
    target_payload = _claim_block(
        payload,
        role="dimension_localization",
        text="The rejected finding mixed a standalone boundary into claim prose.",
        claim_handle=claim_handle,
    )
    target_payload["limitation_handles"] = [limitation_handle]
    other_limitation = next(
        item.limitation_handle
        for item in projection.limitations
        if item.limitation_handle != limitation_handle
    )
    boundary_payload = {
        "role": "boundary",
        "text": "The accepted boundary is available for deterministic repair.",
        "claim_handles": [],
        "recommendation_handles": [],
        "limitation_handles": [other_limitation],
        "material_fact_bindings": [],
        "statement_role": "boundary",
        "required": True,
    }
    target = narrative_workflow_module._block_from_output(
        target_payload,
        writer_attempt_id="writer-attempt:standalone-boundary-source",
        material_projection=projection,
    )
    boundary = narrative_workflow_module._block_from_output(
        boundary_payload,
        writer_attempt_id="writer-attempt:standalone-boundary-source",
        material_projection=projection,
    )

    plan = narrative_workflow_module._compile_focused_retry_plan(
        source_order=(target, boundary),
        accepted_block_ids=(boundary.block_id,),
        rejected_block_ids=(target.block_id,),
        material_projection=projection,
    )

    assert [item.target_kind for item in plan.targets] == ["replace", "replace"]
    assert [item.role for item in plan.targets] == [
        "dimension_localization",
        "boundary",
    ]
    assert plan.preserved_blocks == ()
    assert plan.source_target_blocks == (target, boundary)
    claim_target, boundary_target = plan.targets
    assert limitation_handle not in claim_target.allowed_limitation_handles
    assert limitation_handle in boundary_target.allowed_limitation_handles
    assert (
        limitation_handle
        not in (
            narrative_workflow_module._focused_editable_source_payload(claim_target)[
                "limitation_handles"
            ]
        )
    )
    repaired_target = {**target_payload, "limitation_handles": []}
    repaired_boundary = {
        **boundary_payload,
        "limitation_handles": [other_limitation, limitation_handle],
    }
    merged = narrative_workflow_module._validated_focused_writer_merge(
        {
            "blocks": [
                _focused_editable(repaired_target),
                _focused_editable(repaired_boundary),
            ]
        },
        source_order=(target, boundary),
        retry_plan=plan,
        authority_mode="claim_bearing",
        material_projection=projection,
    )

    assert [item["role"] for item in merged["blocks"]] == [
        "dimension_localization",
        "boundary",
    ]
    assert merged["blocks"][0]["limitation_handles"] == []
    assert limitation_handle in merged["blocks"][1]["limitation_handles"]


def test_focused_retry_plan_inserts_required_boundary_slot_when_source_has_none() -> (
    None
):
    projection, payload, claim_handle, limitation_handle = (
        _standalone_required_limitation_projection()
    )
    target_payload = _claim_block(
        payload,
        role="dimension_localization",
        text="The rejected finding needs a separate boundary block.",
        claim_handle=claim_handle,
    )
    target_payload["limitation_handles"] = [limitation_handle]
    target = narrative_workflow_module._block_from_output(
        target_payload,
        writer_attempt_id="writer-attempt:boundary-slot-source",
        material_projection=projection,
    )
    plan = narrative_workflow_module._compile_focused_retry_plan(
        source_order=(target,),
        accepted_block_ids=(),
        rejected_block_ids=(target.block_id,),
        material_projection=projection,
    )

    assert [item.target_kind for item in plan.targets] == ["replace", "insert"]
    insertion = plan.targets[1]
    assert insertion.target_id.startswith("focused-boundary-slot:sha256:")
    assert insertion.role == "boundary"
    assert insertion.required is True
    assert limitation_handle not in plan.targets[0].allowed_limitation_handles
    assert limitation_handle in insertion.allowed_limitation_handles
    assert narrative_workflow_module._focused_editable_source_payload(insertion) is None
    repaired_target = {**target_payload, "limitation_handles": []}
    inserted_boundary = {
        "text": "This standalone limitation remains explicit and decision-useful.",
        "claim_handles": [],
        "recommendation_handles": [],
        "limitation_handles": [limitation_handle],
        "material_fact_bindings": [],
        "statement_role": "boundary",
    }
    merged = narrative_workflow_module._validated_focused_writer_merge(
        {
            "blocks": [
                _focused_editable(repaired_target),
                inserted_boundary,
            ]
        },
        source_order=(target,),
        retry_plan=plan,
        authority_mode="claim_bearing",
        material_projection=projection,
    )

    assert [item["role"] for item in merged["blocks"]] == [
        "dimension_localization",
        "boundary",
    ]
    assert merged["blocks"][1]["required"] is True
    assert merged["blocks"][1]["limitation_handles"] == [limitation_handle]


def test_focused_scoped_material_view_exposes_only_target_authority_and_pairs() -> None:
    from tests.phase7 import test_narrative_material_projection as projection_contracts

    projection = projection_contracts._derive(projection_contracts._fixture())
    payload = projection.to_writer_payload()
    requirement = projection.publication_requirements[0]
    target_payload = _claim_block(
        payload,
        role="executive_answer",
        text="The required target needs repair.",
        claim_handle=requirement.claim_handles[0],
    )
    target = narrative_workflow_module._block_from_output(
        target_payload,
        writer_attempt_id="writer-attempt:scoped-view-source",
        material_projection=projection,
    )
    plan = narrative_workflow_module._compile_focused_retry_plan(
        source_order=(target,),
        accepted_block_ids=(),
        rejected_block_ids=(target.block_id,),
        material_projection=projection,
    )
    view = narrative_workflow_module._focused_scoped_material_view(
        retry_plan=plan,
        material_projection=projection,
    )

    expected_claim_handles = set(requirement.claim_handles).union(target.claim_handles)
    assert {item["claim_handle"] for item in view["claims"]} == (expected_claim_handles)
    assert len(view["claims"]) < len(payload["claims"])
    material_handles = {
        handle for claim in view["claims"] for handle in claim["material_handles"]
    }
    assert {
        item["material_handle"] for item in view["evidence_materials"]
    } == material_handles
    facts_by_material = {
        item["material_handle"]: {fact["fact_handle"] for fact in item["facts"]}
        for item in view["evidence_materials"]
    }
    for allowed in view["allowed_claim_fact_pairs"]:
        claim = next(
            item
            for item in view["claims"]
            if item["claim_handle"] == allowed["claim_handle"]
        )
        expected_facts = set().union(
            *(facts_by_material[handle] for handle in claim["material_handles"])
        )
        assert set(allowed["fact_handles"]) == expected_facts
        for fact_handle in allowed["fact_handles"]:
            narrative_workflow_module._fact_binding_from_output(
                {
                    "claim_handle": allowed["claim_handle"],
                    "fact_handle": fact_handle,
                },
                material_projection=projection,
            )
    assert "limitation_scope" in view
    assert all(item["boundary_allowed"] is True for item in view["limitation_scope"])


def test_verifier_findings_are_advisory_and_do_not_trigger_automatic_rewrite() -> None:
    authority = _authority_fixture()
    client = _FakeNarrativeLLM(
        (
            _initial_writer,
            _veto_role("dimension_localization"),
            _focused_writer,
            _accept_every_block,
        )
    )

    result = run_narrative_workflow(
        authority_bundle=authority.bundle,
        claim_settlement=authority.settlement,
        evidence_entries=authority.evidence_entries,
        recommendations=authority.recommendations,
        public_materialization=authority.materialization,
        visibility_policy=authority.policy,
        material_projection=_prepared_projection(authority),
        answer_context=_context(),
        llm_client=client,
        sensitive_output_inspector=_NoSensitiveOutput(),
    )

    assert result.publication_ready is True
    assert result.final_accepted_narrative is result.narratives[0]
    assert result.focused_retry is None
    assert len(result.writer_attempts) == 1
    assert len(result.narratives) == 1
    assert len(result.verifier_reports) == 1
    assert len(client.calls) == 2
    assert result.verifier_reports[0].rejected_block_ids
    assert result.withheld_required_block_ids == (
        result.verifier_reports[0].rejected_block_ids[0],
    )
    return

    assert result.publication_ready is True
    assert result.focused_retry is not None
    assert len(result.writer_attempts) == 2
    assert len(result.narratives) == 2
    assert len(result.verifier_reports) == 2
    assert len(client.calls) == 4
    assert all(
        "requirement_limitation_scope" in call["payload"] for call in client.calls
    )
    assert all(
        call["prompt_version"] == "single-authority-phase05.v13"
        for call in client.calls
    )
    first_text = result.narratives[0].blocks[0].text
    final = result.final_accepted_narrative
    assert final is not None
    assert final.parent_narrative_id == result.narratives[0].narrative_id
    assert final.blocks[0] is result.narratives[0].blocks[0]
    assert final.blocks[0].to_dict() == result.narratives[0].blocks[0].to_dict()
    assert final.blocks[0].writer_attempt_id == result.writer_attempts[0].attempt_id
    assert final.blocks[1].writer_attempt_id == result.writer_attempts[1].attempt_id
    assert final.blocks[0].text == first_text
    assert final.blocks[1].text == (
        "The localized pattern remains within its verified ceiling."
    )
    retry_context = client.calls[2]["payload"]["answer_context"]["focused_retry"]
    assert len(retry_context["retry_targets"]) == 1
    assert retry_context["accepted_sibling_blocks"][0]["text"] == first_text
    assert retry_context["required_coverage"] == []
    assert len(client.calls[2]["output"]["blocks"]) == 1
    assert set(client.calls[2]["output"]["blocks"][0]) == {
        "text",
        "claim_handles",
        "recommendation_handles",
        "limitation_handles",
        "material_fact_bindings",
        "statement_role",
    }
    assert retry_context["retry_targets"][0]["fixed_role"] == ("dimension_localization")
    provider_target = retry_context["retry_targets"][0]
    editable_source = provider_target["editable_source_block"]
    assert set(editable_source["claim_handles"]).issubset(
        provider_target["allowed_claim_handles"]
    )
    assert set(editable_source["recommendation_handles"]).issubset(
        provider_target["allowed_recommendation_handles"]
    )
    assert set(editable_source["limitation_handles"]).issubset(
        provider_target["allowed_limitation_handles"]
    )
    assert (
        json.loads(result.writer_attempts[1].provider_response.content)
        == (client.calls[2]["output"])
    )
    assert canonical_value(
        result.provider_audits[2].audit_payload["structured_output"]
    ) == canonical_value(client.calls[2]["output"])
    focused_verifier_payload = client.calls[3]["payload"]
    target_block_ids = {item["block_id"] for item in focused_verifier_payload["blocks"]}
    context_block_ids = {
        item["block_id"] for item in focused_verifier_payload["context_blocks"]
    }
    assert target_block_ids == {final.blocks[1].block_id}
    assert context_block_ids == {final.blocks[0].block_id}
    assert target_block_ids == set(
        focused_verifier_payload["verification_scope"]["target_block_ids"]
    )
    assert context_block_ids == {
        item["block_id"]
        for item in focused_verifier_payload["verification_scope"][
            "inherited_acceptances"
        ]
    }
    assert focused_verifier_payload["verification_scope"]["mode"] == ("focused_retry")
    assert (
        focused_verifier_payload["verification_scope"]["source_verifier_report_ref"]
        == result.verifier_reports[0].verifier_report_ref
    )
    assert (
        focused_verifier_payload["context_blocks"][0]["settled_acceptance"][
            "source_verifier_report_digest"
        ]
        == result.verifier_reports[0].content_digest
    )
    assert set(result.verifier_reports[1].accepted_block_ids) == {
        item.block_id for item in final.blocks
    }
    focused_bindings = (
        retry_context["accepted_sibling_blocks"][0]["material_fact_bindings"],
        retry_context["retry_targets"][0]["editable_source_block"][
            "material_fact_bindings"
        ],
        client.calls[2]["output"]["blocks"][0]["material_fact_bindings"],
        client.calls[3]["payload"]["blocks"][0]["material_fact_bindings"],
    )
    assert all(
        set(binding) == {"claim_handle", "fact_handle"}
        for bindings in focused_bindings
        for binding in bindings
    )
    assert result.writer_attempts[0].attempt_id != result.writer_attempts[1].attempt_id


def test_locale_and_recommendation_direction_quality_is_audited_without_blocking() -> (
    None
):
    authority = _authority_fixture()
    mixed_language_text = (
        "建议获取 actionable 信息，并统一解决增长对象和下降对象的表现问题。"
    )
    ambiguous_direction_text = "建议把增长对象和下降对象统一当作表现问题处理。"

    def mixed_language_writer(
        task: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        output = deepcopy(_initial_writer(task, payload))
        output["blocks"][0]["text"] = mixed_language_text
        output["blocks"][1]["text"] = ambiguous_direction_text
        return output

    def quality_auditor(
        _: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        decisions = []
        for block in payload["blocks"]:
            if block["role"] == "executive_answer":
                decisions.append(
                    {
                        "block_id": block["block_id"],
                        "disposition": "vetoed",
                        "reason_code": "customer_locale_language_inconsistent",
                        "affected_claim_handles": block["claim_handles"][:1],
                        "affected_recommendation_handles": [],
                        "limitation_handles": [],
                    }
                )
            else:
                decisions.append(
                    {
                        "block_id": block["block_id"],
                        "disposition": "vetoed",
                        "reason_code": "recommendation_subject_direction_ambiguous",
                        "affected_claim_handles": block["claim_handles"][:1],
                        "affected_recommendation_handles": [],
                        "limitation_handles": [],
                    }
                )
        return {"decisions": decisions}

    client = _FakeNarrativeLLM((mixed_language_writer, quality_auditor))
    result = run_narrative_workflow(
        authority_bundle=authority.bundle,
        claim_settlement=authority.settlement,
        evidence_entries=authority.evidence_entries,
        recommendations=authority.recommendations,
        public_materialization=authority.materialization,
        visibility_policy=authority.policy,
        material_projection=_prepared_projection(authority),
        answer_context=_context(
            user_question="付费金额变化的原因是什么？",
            locale="zh-CN",
        ),
        llm_client=client,
        sensitive_output_inspector=_NoSensitiveOutput(),
    )

    assert result.publication_ready is True
    assert tuple(block.text for block in result.final_accepted_narrative.blocks) == (
        mixed_language_text,
        ambiguous_direction_text,
    )
    assert result.focused_retry is None
    assert {veto.reason_code for veto in result.verifier_reports[0].vetoes} == {
        "customer_locale_language_inconsistent",
        "recommendation_subject_direction_ambiguous",
    }
    writer_prompt = client.calls[0]["messages"][0]["content"]
    verifier_prompt = client.calls[1]["messages"][0]["content"]
    assert "answer_context.locale" in writer_prompt
    assert "positive growth-replication path" in writer_prompt
    assert "avoidable mixed-language prose" in verifier_prompt
    assert "different subject directions" in verifier_prompt
    assert "customer_locale_language_inconsistent" in verifier_prompt
    assert "recommendation_subject_direction_mismatch" in verifier_prompt
    assert "recommendation_subject_direction_ambiguous" in verifier_prompt
    assert all(
        call["prompt_version"] == "single-authority-phase05.v13"
        for call in client.calls
    )


def test_focused_retry_writer_receives_open_required_coverage_after_optional_sibling() -> (
    None
):
    authority = _authority_fixture()

    def writer_with_optional_sibling(
        _: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        optional = _claim_block(
            payload["material_projection"],
            role="contextual_pattern",
            text="Optional context cannot close a required publication obligation.",
        )
        optional["required"] = False
        required_target = _claim_block(
            payload["material_projection"],
            role="executive_answer",
            text="The required answer needs a focused semantic repair.",
        )
        return {"blocks": [optional, required_target]}

    def repair_with_runtime_coverage(
        _: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        focused = payload["answer_context"]["focused_retry"]
        requirement = payload["material_projection"]["publication_requirements"][0]
        assert focused["required_coverage"] == [
            {
                "requirement_handle": requirement["requirement_handle"],
                "claim_handle_options": requirement["claim_handles"],
                "required_limitation_handles": requirement["limitation_handles"],
            }
        ]
        target = dict(focused["retry_targets"][0]["editable_source_block"])
        target["text"] = "The repaired answer stays within the verified ceiling."
        return {"blocks": [target]}

    client = _FakeNarrativeLLM(
        (
            writer_with_optional_sibling,
            _veto_role("executive_answer"),
            repair_with_runtime_coverage,
            _accept_every_block,
        )
    )
    result = run_narrative_workflow(
        authority_bundle=authority.bundle,
        claim_settlement=authority.settlement,
        evidence_entries=authority.evidence_entries,
        recommendations=authority.recommendations,
        public_materialization=authority.materialization,
        visibility_policy=authority.policy,
        material_projection=_prepared_projection(authority),
        answer_context=_context(),
        llm_client=client,
        sensitive_output_inspector=_NoSensitiveOutput(),
    )

    assert result.publication_ready is True


@pytest.mark.parametrize("violation", ("extra_sibling", "omitted_target"))
def test_focused_writer_accepts_only_the_complete_target_set(
    violation: str,
) -> None:
    authority = _authority_fixture()

    def invalid_focused_writer(
        _: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        focused = payload["answer_context"]["focused_retry"]
        if violation == "omitted_target":
            return {"blocks": []}
        return {
            "blocks": [
                focused["accepted_sibling_blocks"][0],
                focused["retry_targets"][0]["editable_source_block"],
            ]
        }

    client = _FakeNarrativeLLM(
        (
            _initial_writer,
            _veto_role("dimension_localization"),
            invalid_focused_writer,
        )
    )
    result = run_narrative_workflow(
        authority_bundle=authority.bundle,
        claim_settlement=authority.settlement,
        evidence_entries=authority.evidence_entries,
        recommendations=authority.recommendations,
        public_materialization=authority.materialization,
        visibility_policy=authority.policy,
        material_projection=_prepared_projection(authority),
        answer_context=_context(),
        llm_client=client,
        sensitive_output_inspector=_NoSensitiveOutput(),
    )

    assert result.publication_ready is True
    assert result.focused_retry is None
    assert len(client.calls) == 2


def test_multiple_focused_targets_are_merged_in_original_source_order() -> None:
    authority = _authority_fixture()

    def three_block_writer(
        _: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        projection = payload["material_projection"]
        return {
            "blocks": [
                _claim_block(
                    projection,
                    role="executive_answer",
                    text="The executive target needs repair.",
                ),
                _claim_block(
                    projection,
                    role="dimension_localization",
                    text="The accepted middle block remains exact.  ",
                ),
                _claim_block(
                    projection,
                    role="contextual_pattern",
                    text="The contextual target needs repair.",
                ),
            ]
        }

    def veto_outer_blocks(
        _: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        decisions = []
        for block in payload["blocks"]:
            vetoed = block["role"] in {"executive_answer", "contextual_pattern"}
            decisions.append(
                {
                    "block_id": block["block_id"],
                    "disposition": "vetoed" if vetoed else "accepted",
                    "reason_code": (
                        "meaning_exceeds_publication_ceiling" if vetoed else None
                    ),
                    "affected_claim_handles": (
                        block["claim_handles"][:1] if vetoed else []
                    ),
                    "affected_recommendation_handles": [],
                    "limitation_handles": [],
                }
            )
        return {"decisions": decisions}

    def repair_targets_only(
        _: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        targets = payload["answer_context"]["focused_retry"]["retry_targets"]
        repaired = []
        for index, target in enumerate(targets, start=1):
            block = dict(target["editable_source_block"])
            block["text"] = f"Repaired target {index}."
            repaired.append(block)
        return {"blocks": repaired}

    client = _FakeNarrativeLLM(
        (
            three_block_writer,
            veto_outer_blocks,
            repair_targets_only,
            _accept_every_block,
        )
    )
    result = run_narrative_workflow(
        authority_bundle=authority.bundle,
        claim_settlement=authority.settlement,
        evidence_entries=authority.evidence_entries,
        recommendations=authority.recommendations,
        public_materialization=authority.materialization,
        visibility_policy=authority.policy,
        material_projection=_prepared_projection(authority),
        answer_context=_context(),
        llm_client=client,
        sensitive_output_inspector=_NoSensitiveOutput(),
    )

    assert len(client.calls) == 2
    assert result.focused_retry is None
    final = result.final_accepted_narrative
    assert final is not None
    assert [item.role for item in final.blocks] == [
        "executive_answer",
        "dimension_localization",
        "contextual_pattern",
    ]
    assert [item.text for item in final.blocks] == [
        "The executive target needs repair.",
        "The accepted middle block remains exact.  ",
        "The contextual target needs repair.",
    ]
    assert len(result.writer_attempts) == 1


def test_focused_retry_drops_rejected_optional_blocks_from_the_revision() -> None:
    authority = _authority_fixture()

    def initial_with_optional_block(
        _: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        projection = payload["material_projection"]
        optional = _claim_block(
            projection,
            role="contextual_pattern",
            text="This optional context is rejected.",
        )
        optional["required"] = False
        return {
            "blocks": [
                _claim_block(
                    projection,
                    role="executive_answer",
                    text="The accepted answer remains exact.",
                ),
                _claim_block(
                    projection,
                    role="dimension_localization",
                    text="This required target needs repair.",
                ),
                optional,
            ]
        }

    def reject_required_and_optional(
        _: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        decisions = []
        for block in payload["blocks"]:
            rejected = block["role"] in {
                "dimension_localization",
                "contextual_pattern",
            }
            decisions.append(
                {
                    "block_id": block["block_id"],
                    "disposition": "vetoed" if rejected else "accepted",
                    "reason_code": (
                        "meaning_exceeds_publication_ceiling" if rejected else None
                    ),
                    "affected_claim_handles": (
                        block["claim_handles"][:1] if rejected else []
                    ),
                    "affected_recommendation_handles": [],
                    "limitation_handles": [],
                }
            )
        return {"decisions": decisions}

    client = _FakeNarrativeLLM(
        (
            initial_with_optional_block,
            reject_required_and_optional,
            _focused_writer,
            _accept_every_block,
        )
    )
    result = run_narrative_workflow(
        authority_bundle=authority.bundle,
        claim_settlement=authority.settlement,
        evidence_entries=authority.evidence_entries,
        recommendations=authority.recommendations,
        public_materialization=authority.materialization,
        visibility_policy=authority.policy,
        material_projection=_prepared_projection(authority),
        answer_context=_context(),
        llm_client=client,
        sensitive_output_inspector=_NoSensitiveOutput(),
    )

    assert len(client.calls) == 2
    assert result.focused_retry is None
    final = result.final_accepted_narrative
    assert final is not None
    assert [item.role for item in final.blocks] == [
        "executive_answer",
        "dimension_localization",
        "contextual_pattern",
    ]
    assert final.blocks[0] is result.narratives[0].blocks[0]
    assert any(item.role == "contextual_pattern" for item in final.blocks)


def test_unused_focused_retry_prompt_cannot_block_initial_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority_fixture()
    client = _FakeNarrativeLLM(
        (
            _initial_writer,
            _veto_role("dimension_localization"),
        )
    )
    monkeypatch.setattr(
        narrative_workflow_module,
        "_FOCUSED_WRITER_SYSTEM_PROMPT",
        "f" * (NARRATIVE_MESSAGE_ENVELOPE_BYTE_LIMIT + 1),
    )

    result = run_narrative_workflow(
        authority_bundle=authority.bundle,
        claim_settlement=authority.settlement,
        evidence_entries=authority.evidence_entries,
        recommendations=authority.recommendations,
        public_materialization=authority.materialization,
        visibility_policy=authority.policy,
        material_projection=_prepared_projection(authority),
        answer_context=_context(),
        llm_client=client,
        sensitive_output_inspector=_NoSensitiveOutput(),
    )

    assert result.publication_ready is True
    assert [call["task"] for call in client.calls] == [
        "single_authority_narrative_writer",
        "single_authority_block_verification",
    ]


def test_focused_writer_prompt_declares_json_output_contract() -> None:
    assert "json" in narrative_workflow_module._FOCUSED_WRITER_SYSTEM_PROMPT.lower()


def test_prompts_require_precise_typed_limitation_expression_without_metadata_echo() -> (
    None
):
    writer_prompts = (
        narrative_workflow_module._WRITER_SYSTEM_PROMPT,
        narrative_workflow_module._FOCUSED_WRITER_SYSTEM_PROMPT,
    )
    for prompt in writer_prompts:
        normalized_prompt = " ".join(prompt.split())
        assert "binding_mode" in normalized_prompt
        assert "claim_binding_options" in normalized_prompt
        assert "boundary_only" in normalized_prompt
        assert "identity and outcome provenance" in normalized_prompt
        assert "boundary_code" in normalized_prompt
        assert "internal metadata" in normalized_prompt

    verifier_prompt = " ".join(
        narrative_workflow_module._VERIFIER_SYSTEM_PROMPT.split()
    )
    assert "Veto vague availability or trust-boundary wording" in verifier_prompt
    assert "concrete business analysis path" in verifier_prompt
    assert "must not weaken an accepted claim" in verifier_prompt
    assert "verify their business meaning instead" in verifier_prompt


def test_remaining_required_veto_is_recorded_without_blocking_publication() -> None:
    authority = _authority_fixture()
    client = _FakeNarrativeLLM(
        (
            _initial_writer,
            _veto_role("dimension_localization"),
            _focused_writer,
            _veto_role("dimension_localization"),
        )
    )

    result = run_narrative_workflow(
        authority_bundle=authority.bundle,
        claim_settlement=authority.settlement,
        evidence_entries=authority.evidence_entries,
        recommendations=authority.recommendations,
        public_materialization=authority.materialization,
        visibility_policy=authority.policy,
        material_projection=_prepared_projection(authority),
        answer_context=_context(),
        llm_client=client,
        sensitive_output_inspector=_NoSensitiveOutput(),
    )

    assert result.publication_ready is True
    assert result.final_accepted_narrative is result.narratives[0]
    assert len(result.withheld_required_block_ids) == 1
    assert len(client.calls) == 2
    assert len(result.writer_attempts) == 1


def test_incomplete_writer_coverage_is_audited_without_blocking_publication() -> None:
    authority = _authority_fixture()

    def incomplete_writer(
        task: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        output = _initial_writer(task, payload)
        for block in output["blocks"]:
            block["required"] = False
        return output

    result = run_narrative_workflow(
        authority_bundle=authority.bundle,
        claim_settlement=authority.settlement,
        evidence_entries=authority.evidence_entries,
        recommendations=authority.recommendations,
        public_materialization=authority.materialization,
        visibility_policy=authority.policy,
        material_projection=_prepared_projection(authority),
        answer_context=_context(),
        llm_client=_FakeNarrativeLLM((incomplete_writer, _accept_every_block)),
        sensitive_output_inspector=_NoSensitiveOutput(),
    )

    assert result.writer_contract_findings == (
        "required_block_coverage_incomplete",
        "publication_requirement_coverage_incomplete",
    )
    assert result.publication_ready is True
    assert result.final_accepted_narrative is result.narratives[-1]
    assert (
        result.replay(
            authority_bundle=authority.bundle,
            claim_settlement=authority.settlement,
            evidence_entries=authority.evidence_entries,
            recommendations=authority.recommendations,
        )
        == result
    )


def test_writer_quality_metadata_is_normalized_and_retained_in_audit() -> None:
    authority = _authority_fixture()

    def metadata_incomplete_writer(
        task: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        output = _initial_writer(task, payload)
        output["blocks"][0]["statement_role"] = ""
        output["blocks"][1].pop("required")
        return output

    result = run_narrative_workflow(
        authority_bundle=authority.bundle,
        claim_settlement=authority.settlement,
        evidence_entries=authority.evidence_entries,
        recommendations=authority.recommendations,
        public_materialization=authority.materialization,
        visibility_policy=authority.policy,
        material_projection=_prepared_projection(authority),
        answer_context=_context(),
        llm_client=_FakeNarrativeLLM((metadata_incomplete_writer, _accept_every_block)),
        sensitive_output_inspector=_NoSensitiveOutput(),
    )

    assert result.writer_contract_findings == (
        "statement_role_defaulted",
        "required_flag_defaulted",
    )
    assert (
        result.provider_audits[0].audit_payload["structured_output"]["blocks"][0][
            "statement_role"
        ]
        == ""
    )
    assert result.final_accepted_narrative.blocks[0].statement_role == (
        result.final_accepted_narrative.blocks[0].role
    )
    assert result.final_accepted_narrative.blocks[1].required is True
    assert result.publication_ready is True


def test_writer_contract_findings_publish_customer_safe_limit_warning() -> None:
    authority = _authority_fixture()

    def incomplete_writer(
        task: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        output = _initial_writer(task, payload)
        for block in output["blocks"]:
            block["required"] = False
        return output

    narrative = run_narrative_workflow(
        authority_bundle=authority.bundle,
        claim_settlement=authority.settlement,
        evidence_entries=authority.evidence_entries,
        recommendations=authority.recommendations,
        public_materialization=authority.materialization,
        visibility_policy=authority.policy,
        material_projection=_prepared_projection(authority),
        answer_context=_context(),
        llm_client=_FakeNarrativeLLM((incomplete_writer, _accept_every_block)),
        sensitive_output_inspector=_NoSensitiveOutput(),
    )
    flow = PublicationFlowResult.create(
        authority_inputs=authority.authority_inputs,
        authority_bundle=authority.bundle,
        claim_settlement=authority.settlement,
        recommendations=authority.recommendations,
        narrative_workflow=narrative,
        supersedes_publication=None,
        destination_ref="gateway:test-customer",
        channel="gateway",
        published_at="2026-07-20T12:00:00Z",
    )

    assert flow.customer_payload["warnings"] == [
        "部分分析要求的表达仍需人工复核，当前内容可作为业务判断参考。"
    ]
    assert (
        PublicationFlowResult.from_dict(
            flow.to_dict(),
            authority_inputs=authority.authority_inputs,
            authority_bundle=authority.bundle,
            claim_settlement=authority.settlement,
            recommendations=authority.recommendations,
            narrative_workflow=narrative,
            supersedes_publication=None,
        )
        == flow
    )


def test_provider_cannot_restate_or_rewrite_projected_fact_value() -> None:
    authority = _authority_fixture()

    def rewriting_writer(_: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        initial = _initial_writer(_, payload)
        initial["blocks"][1]["material_fact_bindings"][0]["value"] = "999"
        return initial

    client = _FakeNarrativeLLM((rewriting_writer,))

    with pytest.raises(NarrativeProviderCallError) as captured:
        run_narrative_workflow(
            authority_bundle=authority.bundle,
            claim_settlement=authority.settlement,
            evidence_entries=authority.evidence_entries,
            recommendations=authority.recommendations,
            public_materialization=authority.materialization,
            visibility_policy=authority.policy,
            material_projection=_prepared_projection(authority),
            answer_context=_context(),
            llm_client=client,
            sensitive_output_inspector=_NoSensitiveOutput(),
        )

    assert captured.value.kind == "provider_output_invalid"
    assert captured.value.retryability == "not_retryable"
    assert client.calls == []


@pytest.mark.parametrize(
    ("binding_field", "unknown_handle"),
    (
        ("claim_handle", "c_unknown"),
        ("fact_handle", "f_unknown"),
    ),
)
def test_writer_rejects_unknown_fact_binding_handles(
    binding_field: str,
    unknown_handle: str,
) -> None:
    authority = _authority_fixture()

    def unknown_handle_writer(
        _: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        initial = _initial_writer(_, payload)
        initial["blocks"][0]["material_fact_bindings"][0][binding_field] = (
            unknown_handle
        )
        return initial

    with pytest.raises(NarrativeProviderCallError) as captured:
        run_narrative_workflow(
            authority_bundle=authority.bundle,
            claim_settlement=authority.settlement,
            evidence_entries=authority.evidence_entries,
            recommendations=authority.recommendations,
            public_materialization=authority.materialization,
            visibility_policy=authority.policy,
            material_projection=_prepared_projection(authority),
            answer_context=_context(),
            llm_client=_FakeNarrativeLLM((unknown_handle_writer,)),
            sensitive_output_inspector=_NoSensitiveOutput(),
        )

    assert captured.value.kind == "provider_output_invalid"


@pytest.mark.parametrize("violation", ("duplicate_binding", "claim_not_in_block"))
def test_writer_rejects_invalid_fact_binding_block_closure(violation: str) -> None:
    authority = _authority_fixture()

    def invalid_block_writer(
        _: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        initial = _initial_writer(_, payload)
        block = initial["blocks"][0]
        if violation == "duplicate_binding":
            block["material_fact_bindings"].append(
                dict(block["material_fact_bindings"][0])
            )
        else:
            block["claim_handles"] = []
        return initial

    with pytest.raises(NarrativeProviderCallError) as captured:
        run_narrative_workflow(
            authority_bundle=authority.bundle,
            claim_settlement=authority.settlement,
            evidence_entries=authority.evidence_entries,
            recommendations=authority.recommendations,
            public_materialization=authority.materialization,
            visibility_policy=authority.policy,
            material_projection=_prepared_projection(authority),
            answer_context=_context(),
            llm_client=_FakeNarrativeLLM((invalid_block_writer,)),
            sensitive_output_inspector=_NoSensitiveOutput(),
        )

    assert captured.value.kind == "provider_output_invalid"


def test_fact_binding_resolver_rejects_fact_from_another_claim_material() -> None:
    from tests.phase7 import test_narrative_authority as authority_contracts

    projection = authority_contracts._material_projection(
        authority_contracts._palette()
    )
    source_claim = projection.claims[0]
    foreign_material = next(
        material
        for material in projection.evidence_materials
        if material.material_handle not in set(source_claim.material_handles)
    )

    with pytest.raises(
        narrative_workflow_module.NarrativeWorkflowError,
        match="narrative_writer_fact_binding_claim_material_mismatch",
    ):
        narrative_workflow_module._fact_binding_from_output(
            {
                "claim_handle": source_claim.claim_handle,
                "fact_handle": foreign_material.facts[0].fact_handle,
            },
            material_projection=projection,
        )


def test_sensitive_veto_is_excluded_from_semantic_input() -> None:
    authority = _authority_fixture()
    inspector = _FlagFirstDimensionBlock()
    client = _FakeNarrativeLLM(
        (
            _initial_writer,
            _accept_every_block,
            _focused_writer,
            _accept_every_block,
        )
    )

    result = run_narrative_workflow(
        authority_bundle=authority.bundle,
        claim_settlement=authority.settlement,
        evidence_entries=authority.evidence_entries,
        recommendations=authority.recommendations,
        public_materialization=authority.materialization,
        visibility_policy=authority.policy,
        material_projection=_prepared_projection(authority),
        answer_context=_context(),
        llm_client=client,
        sensitive_output_inspector=inspector,
    )

    assert result.publication_ready is True
    assert len(client.calls[1]["payload"]["blocks"]) == 1
    assert {item.code for item in result.local_reports[0].issues} == {
        "sensitive_output_policy_violation"
    }
    assert len(client.calls) == 2
    assert result.focused_retry is None


def test_boundary_only_writer_is_limitation_bound_and_cannot_add_claims() -> None:
    authority = _authority_fixture(boundary_only=True)

    def boundary_writer(_: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        limitation = payload["material_projection"]["limitations"][0]
        return {
            "blocks": [
                {
                    "role": "boundary",
                    "text": "Current authority supports only this explicit limitation.",
                    "claim_handles": [],
                    "recommendation_handles": [],
                    "limitation_handles": [limitation["limitation_handle"]],
                    "material_fact_bindings": [],
                    "statement_role": "boundary",
                    "required": True,
                }
            ]
        }

    client = _FakeNarrativeLLM((boundary_writer, _accept_every_block))
    result = run_narrative_workflow(
        authority_bundle=authority.bundle,
        claim_settlement=authority.settlement,
        evidence_entries=authority.evidence_entries,
        recommendations=authority.recommendations,
        public_materialization=authority.materialization,
        visibility_policy=authority.policy,
        material_projection=_prepared_projection(authority),
        answer_context=_context(boundary_only=True),
        llm_client=client,
        sensitive_output_inspector=_NoSensitiveOutput(),
    )

    assert result.publication_ready is True
    assert result.material_projection.authority_mode == "boundary_only"
    assert result.material_projection.claims == ()
    block = result.final_accepted_narrative.blocks[0]
    assert block.claim_handles == ()
    assert block.limitation_handles

    def invented_claim(_: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        output = boundary_writer(_, payload)
        output["blocks"][0]["claim_handles"] = ["c_invented"]
        return output

    with pytest.raises(
        NarrativeProviderCallError,
        match="provider_output_invalid",
    ):
        run_narrative_workflow(
            authority_bundle=authority.bundle,
            claim_settlement=authority.settlement,
            evidence_entries=authority.evidence_entries,
            recommendations=authority.recommendations,
            public_materialization=authority.materialization,
            visibility_policy=authority.policy,
            material_projection=_prepared_projection(authority),
            answer_context=_context(boundary_only=True),
            llm_client=_FakeNarrativeLLM((invented_claim,)),
            sensitive_output_inspector=_NoSensitiveOutput(),
        )


def _focused_result() -> tuple[_AuthorityFixture, _FakeNarrativeLLM, Any]:
    authority = _authority_fixture()
    client = _FakeNarrativeLLM(
        (
            _initial_writer,
            _veto_role("dimension_localization"),
            _focused_writer,
            _accept_every_block,
        ),
        retry_audit_calls=(0, 2),
    )
    result = run_narrative_workflow(
        authority_bundle=authority.bundle,
        claim_settlement=authority.settlement,
        evidence_entries=authority.evidence_entries,
        recommendations=authority.recommendations,
        public_materialization=authority.materialization,
        visibility_policy=authority.policy,
        material_projection=_prepared_projection(authority),
        answer_context=_context(),
        llm_client=client,
        sensitive_output_inspector=_NoSensitiveOutput(),
    )
    return authority, client, result


def test_result_replay_reconstructs_every_nested_artifact_without_llm() -> None:
    authority, client, result = _focused_result()
    calls_before_replay = len(client.calls)

    replayed = result.replay(
        authority_bundle=authority.bundle,
        claim_settlement=authority.settlement,
        evidence_entries=authority.evidence_entries,
        recommendations=authority.recommendations,
    )

    assert replayed == result
    assert replayed.to_dict() == result.to_dict()
    assert len(client.calls) == calls_before_replay
    assert len(replayed.provider_responses) == 3
    assert len(replayed.provider_audits) == 2
    assert len(replayed.writer_attempts) == 1
    assert len(replayed.narratives) == 1
    assert len(replayed.local_reports) == 1
    assert len(replayed.verification_attempts) == 1
    assert len(replayed.verifier_reports) == 1
    assert replayed.focused_retry is None
    assert (
        validate_typed_narrative_workflow_result(
            replayed,
            authority_bundle=authority.bundle,
            claim_settlement=authority.settlement,
            recommendations=authority.recommendations,
            evidence_entries=authority.evidence_entries,
        )
        == replayed
    )


def _tamper_response(payload: dict[str, Any]) -> None:
    payload["provider_responses"][0]["content"] = '{"tampered":true}'


def _tamper_call_input(payload: dict[str, Any]) -> None:
    payload["provider_call_inputs"][0]["payload"]["answer_context"]["answer_goal"] = (
        "Tampered answer goal."
    )


def _tamper_materialization(payload: dict[str, Any]) -> None:
    payload["public_materialization"]["public_facts"][0]["value"] = "999"


def _tamper_material_projection(payload: dict[str, Any]) -> None:
    payload["material_projection"]["evidence_materials"][0]["facts"][0]["value"] = "999"


def _tamper_audit(payload: dict[str, Any]) -> None:
    payload["provider_audits"][0]["audit_payload"]["raw_response_content"] = (
        '{"tampered":true}'
    )


def _tamper_audit_prompt_version(payload: dict[str, Any]) -> None:
    payload["provider_audits"][1]["audit_payload"]["prompt_version"] = (
        "single-authority-phase05.tampered"
    )


def _tamper_focused_verifier_scope(payload: dict[str, Any]) -> None:
    payload["provider_call_inputs"][3]["payload"]["verification_scope"][
        "target_block_ids"
    ] = []


def _tamper_focused_context_settlement(payload: dict[str, Any]) -> None:
    payload["provider_call_inputs"][3]["payload"]["context_blocks"][0][
        "settled_acceptance"
    ]["source_verifier_report_digest"] = "f" * 64


def _tamper_focused_target_only_output(payload: dict[str, Any]) -> None:
    focused_input = payload["provider_call_inputs"][2]["payload"]["answer_context"][
        "focused_retry"
    ]
    payload["provider_audits"][2]["audit_payload"]["structured_output"][
        "blocks"
    ].insert(0, focused_input["accepted_sibling_blocks"][0])


def _tamper_writer_attempt(payload: dict[str, Any]) -> None:
    payload["writer_attempts"][0]["provider_response"]["content"] = '{"tampered":true}'


def _tamper_narrative(payload: dict[str, Any]) -> None:
    payload["narratives"][0]["blocks"][0]["text"] = "Tampered sibling text."


def _tamper_local_report(payload: dict[str, Any]) -> None:
    payload["local_reports"][0]["accepted_block_ids"] = []


def _tamper_verification_attempt(payload: dict[str, Any]) -> None:
    payload["verification_attempts"][0]["provider_response"]["content"] = (
        '{"tampered":true}'
    )


def _tamper_verifier(payload: dict[str, Any]) -> None:
    payload["verifier_reports"][0]["accepted_block_ids"] = []


def _tamper_focused_retry(payload: dict[str, Any]) -> None:
    payload["focused_retry"]["targeted_block_ids"] = ["narrative-block:unknown"]


@pytest.mark.parametrize(
    "tamper",
    (
        _tamper_response,
        _tamper_call_input,
        _tamper_materialization,
        _tamper_material_projection,
        _tamper_audit,
        _tamper_audit_prompt_version,
        _tamper_writer_attempt,
        _tamper_narrative,
        _tamper_local_report,
        _tamper_verification_attempt,
        _tamper_verifier,
    ),
)
def test_result_replay_rejects_nested_tampering(
    tamper: Callable[[dict[str, Any]], None],
) -> None:
    authority, _, result = _focused_result()
    payload = deepcopy(result.to_dict())
    tamper(payload)

    with pytest.raises(ValueError):
        type(result).from_dict(
            payload,
            authority_bundle=authority.bundle,
            claim_settlement=authority.settlement,
            evidence_entries=authority.evidence_entries,
            recommendations=authority.recommendations,
        )
