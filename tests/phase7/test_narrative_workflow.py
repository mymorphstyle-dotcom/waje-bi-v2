from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import json
from types import SimpleNamespace
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
from bi_agent.runtime.llm_client import LLMOutputError, LLMProviderError, LLMResult
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
    NarrativeQualityAuditResult,
    ReviewedPublicFactMaterialization,
    prepare_narrative_material_projection,
    run_narrative_quality_audit,
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


def _compact_fake_writer_output(
    output: Mapping[str, Any],
) -> dict[str, Any]:
    blocks = output.get("blocks")
    if isinstance(blocks, (str, bytes)) or not isinstance(blocks, Sequence):
        return dict(output)
    field_map = {
        "requirement_handles": "p",
        "claim_handles": "c",
        "recommendation_handles": "r",
        "limitation_handles": "l",
        "material_fact_bindings": "f",
        "statement_role": "s",
        "required": "q",
    }
    compact_blocks: list[Any] = []
    for raw_block in blocks:
        if not isinstance(raw_block, Mapping):
            compact_blocks.append(raw_block)
            continue
        compact: dict[str, Any] = {}
        for key, value in raw_block.items():
            if key == "material_fact_bindings" and isinstance(value, Sequence):
                compact[field_map[key]] = [
                    (
                        [item.get("claim_handle"), item.get("fact_handle")]
                        if isinstance(item, Mapping)
                        and set(item) == {"claim_handle", "fact_handle"}
                        else item
                    )
                    for item in value
                ]
            elif key in field_map:
                compact[field_map[key]] = value
            else:
                compact[key] = value
        compact_blocks.append(compact)
    return {**dict(output), "blocks": compact_blocks}


_TEST_ACCEPTED_INTENT_CONTEXT = {
    "goal_bindings": ({"goal_id": "explain_change", "role": "primary"},),
    "target_metric_refs": ("paid_amount",),
    "scope": {"scope_type": "full_sample", "filters": ()},
    "time_spec": {"mode": "resolved_test_window"},
    "comparison_spec": {"mode": "target_vs_baseline"},
    "direction_premise": "unknown",
    "requested_analysis_axes": ("formula_tree",),
    "requested_factor_refs": ("paid_users",),
    "desired_decisions": (
        {"decision_kind": "driver_priority", "target_ref": "paid_amount"},
    ),
}
_TEST_ACCEPTED_PLAN_CONTEXT = {
    "temporal_authority": {
        "mode": "window_pair",
        "effective_comparison_spec": {
            "kind": "fixed_window",
            "aggregation": "sum_of_complete_days",
        },
    },
    "accepted_question_graph": (),
    "user_required_obligations": (
        {
            "obligation_id": "claim-obligation:test",
            "claim_kind": "comparative_change",
            "subject": {
                "target_metric_ref": "paid_amount",
                "scope": {"scope_type": "full_sample", "filters": ()},
                "outcome_refs": ("direction_and_magnitude",),
                "goal_refs": ("explain_change",),
            },
            "minimum_claim_strength": "directional",
        },
    ),
    "analysis_axes": (
        {
            "axis_id": "formula_tree",
            "role": "required",
            "axis_kind": "accounting",
            "target_metric_refs": ("paid_amount",),
            "metric_refs": ("paid_users", "paid_frequency", "avg_order_amount"),
            "dimension_refs": (),
            "capability_refs": ("formula_decompose",),
            "reconciliation_group": "paid_amount_formula",
            "goal_refs": ("explain_change",),
            "supports_obligation_ids": ("claim-obligation:test",),
        },
    ),
    "capability_route": (
        {
            "capability_id": "formula_decompose",
            "execution_rank": 1,
            "supports_obligation_ids": ("claim-obligation:test",),
            "depends_on_capability_ids": (),
        },
    ),
}


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
        if (
            task.endswith("narrative_writer")
            and payload.get("_wire", {}).get("writer_output_encoding")
            == "compact-narrative-blocks.v2"
        ):
            output = _compact_fake_writer_output(output)
        validator = kwargs.get("output_validator")
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
        accepted_intent_context=_TEST_ACCEPTED_INTENT_CONTEXT,
        accepted_plan_context=_TEST_ACCEPTED_PLAN_CONTEXT,
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


def _run_quality_audit(
    authority: _AuthorityFixture,
    workflow: Any,
    client: _FakeNarrativeLLM,
) -> NarrativeQualityAuditResult:
    return run_narrative_quality_audit(
        source_customer_publication_ref=(
            "customer-publication:sha256:" + "a" * 64
        ),
        authority_bundle=authority.bundle,
        claim_settlement=authority.settlement,
        evidence_entries=authority.evidence_entries,
        recommendations=authority.recommendations,
        narrative_workflow=workflow,
        llm_client=client,
    )


def _provider_material_facts(
    material: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    columns = material.get("fact_columns")
    if columns is None:
        return tuple(dict(item) for item in material["facts"])
    return tuple(
        dict(zip(columns, row, strict=True))
        for row in material["facts"]
    )


def test_requested_factor_comparison_focus_binds_accepted_group_to_fact_handles() -> (
    None
):
    def fact(name: str, value: str) -> SimpleNamespace:
        return SimpleNamespace(
            name=name,
            value=value,
            fact_handle="f_" + name.replace(".", "_").replace("[", "_").replace(
                "]", ""
            ),
        )

    grouped_prefix = "decomposition.grouped_decompositions[3]"
    fields = (
        "metric_id",
        "baseline_value",
        "target_value",
        "delta",
        "contribution",
        "contribution_share",
    )
    values = {
        "paid_users": ("paid_users", "10", "12", "2", "13.38", "0.3464"),
        "paid_amount_per_paid_user": (
            "paid_amount_per_paid_user",
            "10",
            "12",
            "2",
            "25.24",
            "0.6536",
        ),
    }
    facts = [fact(f"{grouped_prefix}.grouping_id", "factor_comparison")]
    for index, factor_ref in enumerate(("paid_users", "paid_amount_per_paid_user")):
        facts.extend(
            fact(
                f"{grouped_prefix}.contributions[{index}].{field}",
                value,
            )
            for field, value in zip(fields, values[factor_ref], strict=True)
        )
    facts.extend(
        (
            fact(f"{grouped_prefix}.contribution_total", "38.62"),
            fact(f"{grouped_prefix}.component_residual", "0"),
        )
    )
    material = SimpleNamespace(
        material_handle="m_formula",
        interpretation_contract={
            "factor_hierarchy": {
                "groupings": (
                    {
                        "grouping_id": "factor_comparison",
                        "method": "grouped_shapley",
                        "factors": (
                            {
                                "factor_ref": "paid_users",
                                "member_metric_refs": ("paid_users",),
                            },
                            {
                                "factor_ref": "paid_amount_per_paid_user",
                                "member_metric_refs": (
                                    "paid_frequency",
                                    "avg_order_amount",
                                ),
                            },
                        ),
                    },
                )
            }
        },
        facts=tuple(facts),
    )
    projection = SimpleNamespace(
        publication_requirements=(
            SimpleNamespace(
                claim_kind="formula_component_contribution",
                status="satisfied",
                claim_handles=("c_formula",),
                requirement_handle="pr_formula",
            ),
        ),
        claims=(
            SimpleNamespace(
                claim_handle="c_formula",
                material_handles=("m_formula",),
            ),
        ),
        evidence_materials=(material,),
    )
    context = SimpleNamespace(
        accepted_intent_context={
            "requested_factor_refs": (
                "paid_amount_per_paid_user",
                "paid_users",
            )
        }
    )

    focus = narrative_workflow_module._requested_factor_comparison_focus(
        answer_context=context,
        material_projection=projection,
    )

    assert focus["status"] == "matched"
    assert focus["requested_factor_refs"] == [
        "paid_amount_per_paid_user",
        "paid_users",
    ]
    assert len(focus["matches"]) == 1
    match = focus["matches"][0]
    assert match["requirement_handles"] == ["pr_formula"]
    assert match["claim_handles"] == ["c_formula"]
    assert match["material_handle"] == "m_formula"
    assert match["grouping_id"] == "factor_comparison"
    assert match["comparison_level"] == "contract_declared_factor_group"
    assert match["cross_level_additivity"] == "forbidden"
    assert [item["factor_ref"] for item in match["factors"]] == [
        "paid_amount_per_paid_user",
        "paid_users",
    ]
    assert match["factors"][0]["member_metric_refs"] == [
        "paid_frequency",
        "avg_order_amount",
    ]
    assert set(match["factors"][0]["fact_handles"]) == set(fields)
    assert set(match["reconciliation_fact_handles"]) == {
        "contribution_total",
        "component_residual",
    }
    assert "25.24" not in json.dumps(focus, ensure_ascii=False)


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
    fact = _provider_material_facts(material)[0]
    return {
        "role": role,
        "text": text,
        "requirement_handles": [],
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
    assert result.delivery_narrative is result.narratives[0]
    assert result.narratives[0].blocks[0].text.endswith("original.  ")
    assert result.writer_attempts[0].attempt_number == 2
    assert [item.attempt_number for item in result.provider_responses] == [1, 2]
    assert len(result.provider_audits) == 1
    assert tuple(item.purpose for item in result.provider_call_inputs) == (
        "narrative_writer",
    )
    assert result.material_projection.palette_ref.startswith(
        "public-claim-palette:sha256:"
    )
    assert not hasattr(result, "palette")
    assert len(client.calls) == 1
    assert set(client.calls[0]["payload"]) == {
        "_wire",
        "material_projection",
        "requirement_limitation_scope",
        "answer_context",
        "requested_factor_comparison",
    }
    assert client.calls[0]["payload"]["_wire"] == {
        "reference_encoding": "short-authority-alias.v1",
        "writer_output_encoding": "compact-narrative-blocks.v2",
    }
    writer_requirements = client.calls[0]["payload"]["material_projection"][
        "publication_requirements"
    ]
    assert len(writer_requirements) == 1
    assert set(writer_requirements[0]) == {
        "requirement_handle",
        "obligation_id",
            "status",
            "coverage_semantics",
            "issue_ref",
            "parent_issue_ref",
            "business_question",
            "question_role",
            "answer_contract",
            "claim_kind",
        "assertion_scope",
            "required_claim_strength",
            "claim_handles",
            "required_fact_handles",
            "limitation_handles",
    }
    assert all(
        call["prompt_version"] == "single-authority-phase05.v33"
        for call in client.calls
    )
    writer_prompt = client.calls[0]["messages"][0]["content"]
    assert "Markdown blank lines" in writer_prompt
    assert "operational meaning" in writer_prompt
    assert "do not impose a fixed length or block count" in writer_prompt
    assert "representative_not_exhaustive" in writer_prompt
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
    assert (
        client.calls[0]["payload"]["material_projection"]["transport_encoding"]
        == "columnar-material-facts.v1"
    )
    assert all(
        item["fact_columns"]
        == ["fact_handle", "name", "fact_kind", "value", "range_end", "unit"]
        for item in client.calls[0]["payload"]["material_projection"][
            "evidence_materials"
        ]
    )
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
        "p",
        "c",
        "r",
        "l",
        "f",
        "s",
        "q",
    }
    provider_binding = client.calls[0]["output"]["blocks"][0]["f"][0]
    quality_audit = _run_quality_audit(authority, result, client)
    assert quality_audit.verifier_report.audit_status == "completed"
    assert len(client.calls) == 2
    verifier_binding = client.calls[1]["payload"]["blocks"][0][
        "material_fact_bindings"
    ][0]
    assert len(provider_binding) == 2
    assert verifier_binding == {
        "claim_handle": provider_binding[0],
        "fact_handle": provider_binding[1],
    }
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
        "_wire",
        "material_projection",
        "answer_context",
        "verification_scope",
        "requirement_limitation_scope",
        "blocks",
    }
    assert client.calls[1]["payload"]["verification_scope"]["mode"] == "full"
    assert "context_blocks" not in client.calls[1]["payload"]
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


def test_provider_profiles_disable_rendering_and_verification_thinking() -> None:
    authority = _authority_fixture()
    client = _FakeNarrativeLLM((_initial_writer, _accept_every_block))
    client.supports_thinking_mode = True

    result = run_narrative_workflow(
        authority_bundle=authority.bundle,
        claim_settlement=authority.settlement,
        evidence_entries=authority.evidence_entries,
        recommendations=authority.recommendations,
        public_materialization=authority.materialization,
        visibility_policy=authority.policy,
        material_projection=_prepared_projection(authority),
        answer_context=_context(user_question="付费金额为什么变化？"),
        llm_client=client,
        sensitive_output_inspector=_NoSensitiveOutput(),
    )

    assert [call["thinking"] for call in client.calls] == ["disabled"]
    _run_quality_audit(authority, result, client)
    assert [call["thinking"] for call in client.calls] == [
        "disabled",
        "disabled",
    ]


def test_fixed_enum_and_accepted_verifier_details_are_normalized_without_retry() -> (
    None
):
    authority = _authority_fixture()

    def writer(_: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        output = deepcopy(_initial_writer("", payload))
        output["blocks"][0]["role"] = "business_overview"
        return output

    def verbose_acceptance(
        _: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return {
            "decisions": [
                {
                    "block_id": block["block_id"],
                    "disposition": "accepted",
                    "reason_code": "reviewed_and_supported",
                    "affected_claim_handles": block["claim_handles"],
                    "affected_recommendation_handles": block[
                        "recommendation_handles"
                    ],
                    "limitation_handles": block["limitation_handles"],
                }
                for block in payload["blocks"]
            ]
        }

    client = _FakeNarrativeLLM((writer, verbose_acceptance))
    result = run_narrative_workflow(
        authority_bundle=authority.bundle,
        claim_settlement=authority.settlement,
        evidence_entries=authority.evidence_entries,
        recommendations=authority.recommendations,
        public_materialization=authority.materialization,
        visibility_policy=authority.policy,
        material_projection=_prepared_projection(authority),
        answer_context=_context(user_question="付费金额为什么变化？"),
        llm_client=client,
        sensitive_output_inspector=_NoSensitiveOutput(),
    )

    assert len(client.calls) == 1
    assert result.delivery_narrative.blocks[0].role == "direction"
    assert "block_role_derived_from_authority_handles" in (
        result.writer_contract_findings
    )
    audit_result = _run_quality_audit(authority, result, client)
    assert audit_result.verifier_report.vetoes == ()
    assert audit_result.provider_audit is not None
    assert audit_result.provider_audit.audit_payload[
        "verifier_contract_findings"
    ] == ("accepted_decision_details_cleared",)


def test_duplicate_fact_bindings_are_normalized_without_provider_retry() -> None:
    authority = _authority_fixture()

    def writer(_: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        output = deepcopy(_initial_writer("", payload))
        output["blocks"][0]["material_fact_bindings"].append(
            dict(output["blocks"][0]["material_fact_bindings"][0])
        )
        return output

    client = _FakeNarrativeLLM((writer, _accept_every_block))
    result = run_narrative_workflow(
        authority_bundle=authority.bundle,
        claim_settlement=authority.settlement,
        evidence_entries=authority.evidence_entries,
        recommendations=authority.recommendations,
        public_materialization=authority.materialization,
        visibility_policy=authority.policy,
        material_projection=_prepared_projection(authority),
        answer_context=_context(user_question="付费金额为什么变化？"),
        llm_client=client,
        sensitive_output_inspector=_NoSensitiveOutput(),
    )

    assert len(client.calls) == 1
    assert len(
        result.delivery_narrative.blocks[0].material_fact_bindings
    ) == 1
    assert result.writer_contract_findings == (
        "fact_binding_duplicate_removed",
    )


def test_unknown_fact_binding_is_a_nonretryable_provenance_error() -> None:
    authority = _authority_fixture()
    responder_calls = 0

    def writer(_: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        nonlocal responder_calls
        responder_calls += 1
        output = deepcopy(_initial_writer("", payload))
        output["blocks"][0]["material_fact_bindings"][0]["fact_handle"] = (
            "f_unknown"
        )
        return output

    client = _FakeNarrativeLLM((writer,))
    with pytest.raises(NarrativeProviderCallError) as captured:
        run_narrative_workflow(
            authority_bundle=authority.bundle,
            claim_settlement=authority.settlement,
            evidence_entries=authority.evidence_entries,
            recommendations=authority.recommendations,
            public_materialization=authority.materialization,
            visibility_policy=authority.policy,
            material_projection=_prepared_projection(authority),
            answer_context=_context(user_question="付费金额为什么变化？"),
            llm_client=client,
            sensitive_output_inspector=_NoSensitiveOutput(),
        )

    assert responder_calls == 1
    assert captured.value.kind == "provider_output_invalid"
    assert captured.value.retryability == "not_retryable"


def test_representative_material_transport_keeps_exact_required_fact_view() -> None:
    def fact(handle: str) -> dict[str, Any]:
        return {
            "fact_handle": handle,
            "name": f"name:{handle}",
            "fact_kind": "number",
            "value": 1,
            "range_end": None,
            "unit": "count",
        }

    encoded = narrative_workflow_module._columnar_material_fact_transport(
        {
            "authority_mode": "claim_bearing",
            "claims": [],
            "publication_requirements": [
                {
                    "required_fact_handles": ["f_required"],
                }
            ],
            "evidence_materials": [
                {
                    "material_handle": "m_test",
                    "evidence_kind": "observed",
                    "evidence_strength": "qualified",
                    "maximum_claim_strength": "directional",
                    "scope": "scope:test",
                    "dimension_path": [],
                    "interpretation_contract": {
                        "dimension_summary_claim_scope": (
                            "representative_not_exhaustive"
                        ),
                        "dimension_summary_selection_policy": (
                            "largest_target_volume"
                        ),
                    },
                    "facts": [
                        fact("f_required"),
                        fact("f_unselected_1"),
                        fact("f_unselected_2"),
                    ],
                }
            ],
            "recommendations": [],
            "limitations": [],
            "boundary_facets": [],
        }
    )

    material = encoded["evidence_materials"][0]
    assert material["facts"] == [
        ["f_required", "name:f_required", "number", 1, None, "count"]
    ]
    assert material["fact_claim_handle_options"] == []
    assert material["fact_selection"] == {
        "mode": "contract_required_representative_view",
        "source_fact_count": 3,
        "selected_fact_count": 1,
        "omitted_fact_count": 2,
        "dimension_summary_claim_scope": "representative_not_exhaustive",
        "dimension_summary_selection_policy": "largest_target_volume",
    }


def test_contract_named_writer_fact_view_keeps_required_and_declared_facts() -> None:
    def fact(handle: str, name: str) -> dict[str, Any]:
        return {
            "fact_handle": handle,
            "name": name,
            "fact_kind": "number",
            "value": 1,
            "range_end": None,
            "unit": "count",
        }

    encoded = narrative_workflow_module._columnar_material_fact_transport(
        {
            "authority_mode": "claim_bearing",
            "claims": [],
            "publication_requirements": [
                {
                    "required_fact_handles": ["f_required"],
                }
            ],
            "evidence_materials": [
                {
                    "material_handle": "m_test",
                    "evidence_kind": "statistical_association",
                    "evidence_strength": "medium",
                    "maximum_claim_strength": "candidate_driver",
                    "scope": "scope:test",
                    "dimension_path": ["region"],
                    "interpretation_contract": {
                        "contract_id": "dimension-localization-interpretation.v1",
                        "writer_fact_selection": {
                            "mode": "named_fact_subset",
                            "fact_names": [
                                "dimension_label",
                                "diagnostic_priority_score",
                            ],
                        },
                    },
                    "facts": [
                        fact("f_label", "dimension_label"),
                        fact("f_score", "diagnostic_priority_score"),
                        fact("f_required", "required_outcome"),
                        fact("f_internal", "score_explanation.formulaVersion"),
                    ],
                }
            ],
            "recommendations": [],
            "limitations": [],
            "boundary_facets": [],
        }
    )

    material = encoded["evidence_materials"][0]
    assert [row[0] for row in material["facts"]] == [
        "f_label",
        "f_score",
        "f_required",
    ]
    assert material["fact_selection"] == {
        "mode": "contract_named_fact_view",
        "contract_id": "dimension-localization-interpretation.v1",
        "source_fact_count": 4,
        "selected_fact_count": 3,
        "omitted_fact_count": 1,
    }


def test_writer_normalizes_a_fact_binding_with_one_legal_block_owner() -> None:
    authority = _authority_fixture()
    projection = _prepared_projection(authority)
    claim = projection.claims[0]
    wrong_owner = "c_wrong_unique_owner"
    projection_with_foreign_claim = replace(
        projection,
        claims=(
            claim,
            replace(
                claim,
                claim_handle=wrong_owner,
                evidence_entry_refs=(),
                material_handles=(),
                limitation_handles=(),
            ),
        ),
    )
    block = _claim_block(
        narrative_workflow_module._columnar_material_fact_transport(
            projection.to_writer_payload()
        ),
        role="executive_answer",
        text="The published fact remains bound to its reviewed material.",
    )
    block["claim_handles"].append(wrong_owner)
    block["material_fact_bindings"][0]["claim_handle"] = wrong_owner

    normalized, findings = (
        narrative_workflow_module._normalize_initial_writer_output_for_delivery(
            {"blocks": [block]},
            authority_mode=authority.bundle.authority_mode,
            material_projection=projection_with_foreign_claim,
        )
    )

    assert normalized["blocks"][0]["material_fact_bindings"][0][
        "claim_handle"
    ] == claim.claim_handle
    assert findings == ("fact_binding_owner_normalized",)


def test_writer_adds_one_global_fact_owner_when_block_omits_it() -> None:
    authority = _authority_fixture()
    projection = _prepared_projection(authority)
    claim = projection.claims[0]
    wrong_owner = "c_wrong_unique_owner"
    projection_with_foreign_claim = replace(
        projection,
        claims=(
            claim,
            replace(
                claim,
                claim_handle=wrong_owner,
                evidence_entry_refs=(),
                material_handles=(),
                limitation_handles=(),
            ),
        ),
    )
    block = _claim_block(
        narrative_workflow_module._columnar_material_fact_transport(
            projection.to_writer_payload()
        ),
        role="executive_answer",
        text="The published fact remains bound to its reviewed material.",
    )
    block["claim_handles"] = [wrong_owner]
    block["material_fact_bindings"][0]["claim_handle"] = wrong_owner

    normalized, findings = (
        narrative_workflow_module._normalize_initial_writer_output_for_delivery(
            {"blocks": [block]},
            authority_mode=authority.bundle.authority_mode,
            material_projection=projection_with_foreign_claim,
        )
    )

    assert normalized["blocks"][0]["material_fact_bindings"][0][
        "claim_handle"
    ] == claim.claim_handle
    assert normalized["blocks"][0]["claim_handles"] == [
        claim.claim_handle,
        wrong_owner,
    ]
    assert findings == (
        "fact_binding_owner_normalized",
        "fact_binding_global_owner_added",
    )


def test_writer_retains_answer_equivalent_claim_handles_in_final_synthesis() -> (
    None
):
    authority = _authority_fixture()
    projection = _prepared_projection(authority)
    claim = projection.claims[0]
    equivalent_payload = canonical_value(claim.verified_claim_payload)
    equivalent_payload["obligation_id"] = "claim-obligation:equivalent"
    equivalent_claim = replace(
        claim,
        claim_handle="c_answer_equivalent",
        verified_claim_payload=equivalent_payload,
    )
    projection_with_equivalent_claim = replace(
        projection,
        claims=(*projection.claims, equivalent_claim),
    )
    block = _claim_block(
        narrative_workflow_module._columnar_material_fact_transport(
            projection.to_writer_payload()
        ),
        role="executive_answer",
        text="One business statement carries both answer-equivalent claim authorities.",
    )

    normalized, findings = (
        narrative_workflow_module._normalize_initial_writer_output_for_delivery(
            {"blocks": [block]},
            authority_mode=authority.bundle.authority_mode,
            material_projection=projection_with_equivalent_claim,
        )
    )

    assert normalized["blocks"][0]["claim_handles"] == sorted(
        [claim.claim_handle, equivalent_claim.claim_handle]
    )
    assert findings == ("answer_equivalent_claim_coverage_closed",)


def test_writer_audits_a_distinct_claim_missing_from_final_synthesis() -> None:
    authority = _authority_fixture()
    projection = _prepared_projection(authority)
    claim = projection.claims[0]
    distinct_payload = canonical_value(claim.verified_claim_payload)
    distinct_payload["claim_kind"] = "different_business_proposition"
    distinct_claim = replace(
        claim,
        claim_handle="c_distinct_missing",
        verified_claim_payload=distinct_payload,
    )
    projection_with_distinct_claim = replace(
        projection,
        claims=(*projection.claims, distinct_claim),
    )
    block = _claim_block(
        narrative_workflow_module._columnar_material_fact_transport(
            projection.to_writer_payload()
        ),
        role="executive_answer",
        text="Only the supplied claim is represented in this final synthesis.",
    )

    normalized, findings = (
        narrative_workflow_module._normalize_initial_writer_output_for_delivery(
            {"blocks": [block]},
            authority_mode=authority.bundle.authority_mode,
            material_projection=projection_with_distinct_claim,
        )
    )

    assert normalized["blocks"][0]["claim_handles"] == [claim.claim_handle]
    assert findings == ("public_claim_coverage_incomplete",)


def test_candidate_claim_without_required_facts_uses_claim_only_transport() -> None:
    def fact(handle: str) -> dict[str, Any]:
        return {
            "fact_handle": handle,
            "name": f"name:{handle}",
            "fact_kind": "number",
            "value": 1,
            "range_end": None,
            "unit": "count",
        }

    encoded = narrative_workflow_module._columnar_material_fact_transport(
        {
            "authority_mode": "claim_bearing",
            "claims": [
                {
                    "claim_handle": "c_candidate",
                    "claim_class": "candidate_mechanism",
                    "material_handles": ["m_candidate"],
                }
            ],
            "publication_requirements": [
                {
                    "claim_handles": ["c_candidate"],
                    "required_fact_handles": [],
                }
            ],
            "evidence_materials": [
                {
                    "material_handle": "m_candidate",
                    "evidence_kind": "observed",
                    "evidence_strength": "low",
                    "maximum_claim_strength": "candidate_mechanism",
                    "scope": "scope:test",
                    "dimension_path": [],
                    "interpretation_contract": {},
                    "facts": [fact("f_1"), fact("f_2")],
                }
            ],
            "recommendations": [],
            "limitations": [],
            "boundary_facets": [],
        }
    )

    material = encoded["evidence_materials"][0]
    assert material["facts"] == []
    assert material["fact_claim_handle_options"] == ["c_candidate"]
    assert material["fact_selection"] == {
        "mode": "accepted_candidate_claim_without_required_facts",
        "source_fact_count": 2,
        "selected_fact_count": 0,
        "omitted_fact_count": 2,
    }


def test_writer_transport_keeps_only_question_graph_publication_requirements() -> None:
    answer_contract = {
        "contract_version": "question-answer-contract.v1",
        "completion_policy": "direct_answer_or_explicitly_unresolved",
        "blocking": False,
    }
    encoded = narrative_workflow_module._columnar_material_fact_transport(
        {
            "authority_mode": "claim_bearing",
            "claims": [
                {
                    "claim_handle": "c_answer",
                    "claim_class": "observed_fact",
                    "material_handles": ["m_answer"],
                }
            ],
            "publication_requirements": [
                {
                    "requirement_handle": "q_answer",
                    "issue_ref": "issue:answer",
                    "business_question": "What changed?",
                    "answer_contract": answer_contract,
                    "claim_handles": ["c_answer"],
                    "required_fact_handles": ["f_answer"],
                },
                {
                    "requirement_handle": "q_auxiliary",
                    "issue_ref": None,
                    "business_question": None,
                    "answer_contract": {},
                    "claim_handles": ["c_answer"],
                    "required_fact_handles": ["f_auxiliary"],
                },
            ],
            "evidence_materials": [
                {
                    "material_handle": "m_answer",
                    "evidence_kind": "observed",
                    "evidence_strength": "high",
                    "maximum_claim_strength": "observed_fact",
                    "scope": "scope:test",
                    "dimension_path": [],
                    "interpretation_contract": {
                        "dimension_summary_claim_scope": (
                            "representative_not_exhaustive"
                        ),
                        "dimension_summary_selection_policy": (
                            "largest_target_volume"
                        ),
                    },
                    "facts": [
                        {
                            "fact_handle": "f_answer",
                            "name": "answer",
                            "fact_kind": "number",
                            "value": 1,
                            "range_end": None,
                            "unit": "count",
                        },
                        {
                            "fact_handle": "f_auxiliary",
                            "name": "auxiliary",
                            "fact_kind": "number",
                            "value": 2,
                            "range_end": None,
                            "unit": "count",
                        },
                    ],
                }
            ],
            "recommendations": [],
            "limitations": [],
            "boundary_facets": [],
        }
    )

    assert [
        item["requirement_handle"]
        for item in encoded["publication_requirements"]
    ] == ["q_answer"]
    facts = _provider_material_facts(encoded["evidence_materials"][0])
    assert [item["fact_handle"] for item in facts] == ["f_answer"]


def test_question_answers_require_one_explicit_block_per_planner_issue() -> None:
    requirements = (
        SimpleNamespace(
            requirement_handle="pr_primary",
            issue_ref="issue:primary",
            status="satisfied",
            claim_handles=("c_primary",),
            required_fact_handles=("f_primary",),
            limitation_handles=(),
        ),
        SimpleNamespace(
            requirement_handle="pr_driver",
            issue_ref="issue:driver",
            status="mixed",
            claim_handles=("c_driver",),
            required_fact_handles=("f_driver",),
            limitation_handles=("l_driver",),
        ),
    )
    projection = SimpleNamespace(publication_requirements=requirements)
    primary_answer = {
        "requirement_handles": ["pr_primary"],
        "claim_handles": ["c_primary"],
        "limitation_handles": [],
        "material_fact_bindings": [
            {"claim_handle": "c_primary", "fact_handle": "f_primary"}
        ],
    }
    driver_answer = {
        "requirement_handles": ["pr_driver"],
        "claim_handles": ["c_driver"],
        "limitation_handles": ["l_driver"],
        "material_fact_bindings": [
            {"claim_handle": "c_driver", "fact_handle": "f_driver"}
        ],
    }
    final_synthesis = {
        "requirement_handles": [],
        "claim_handles": ["c_primary", "c_driver"],
        "limitation_handles": ["l_driver"],
        "material_fact_bindings": [
            {"claim_handle": "c_primary", "fact_handle": "f_primary"},
            {"claim_handle": "c_driver", "fact_handle": "f_driver"},
        ],
    }

    assert narrative_workflow_module._question_answer_requirements_covered(
        material_projection=projection,
        blocks=(primary_answer, driver_answer, final_synthesis),
    )
    assert not narrative_workflow_module._question_answer_requirements_covered(
        material_projection=projection,
        blocks=(final_synthesis,),
    )
    assert not narrative_workflow_module._question_answer_requirements_covered(
        material_projection=projection,
        blocks=(primary_answer, final_synthesis),
    )


def test_question_answer_scope_includes_local_limitations_of_allowed_claims() -> None:
    projection = SimpleNamespace(
        publication_requirements=(
            SimpleNamespace(
                requirement_handle="pr_boundary",
                issue_ref="issue:boundary",
                status="satisfied",
                claim_handles=("c_boundary",),
                required_fact_handles=(),
                limitation_handles=(),
            ),
        ),
        claims=(
            SimpleNamespace(
                claim_handle="c_boundary",
                material_handles=(),
                limitation_handles=("l_local",),
            ),
        ),
        recommendations=(),
        limitations=(SimpleNamespace(limitation_handle="l_local"),),
        evidence_materials=(),
    )
    block = {
        "role": "direction",
        "text": "现有数据支持这条边界结论，并保留该事实自身的数据限制。",
        "requirement_handles": ["pr_boundary"],
        "claim_handles": ["c_boundary"],
        "recommendation_handles": [],
        "limitation_handles": ["l_local"],
        "material_fact_bindings": [],
        "statement_role": "business_answer",
        "required": True,
    }

    narrative_workflow_module._writer_block_shape(
        block,
        material_projection=projection,
    )


def test_soft_question_answer_scope_drift_stays_question_linked_and_is_audited() -> None:
    projection = SimpleNamespace(
        publication_requirements=(
            SimpleNamespace(
                requirement_handle="pr_primary",
                issue_ref="issue:primary",
                status="satisfied",
                claim_handles=("c_primary",),
                required_fact_handles=(),
                limitation_handles=(),
            ),
        ),
        claims=(
            SimpleNamespace(
                claim_handle="c_primary",
                claim_class="observed_fact",
                material_handles=(),
                limitation_handles=(),
            ),
            SimpleNamespace(
                claim_handle="c_other",
                claim_class="observed_fact",
                material_handles=(),
                limitation_handles=(),
            ),
        ),
        recommendations=(),
        limitations=(),
        evidence_materials=(),
    )
    output = {
        "blocks": [
            {
                "role": "direction",
                "text": "这段内容引用了另一个问题的事实。",
                "requirement_handles": ["pr_primary"],
                "claim_handles": ["c_primary", "c_other"],
                "recommendation_handles": [],
                "limitation_handles": [],
                "material_fact_bindings": [],
                "statement_role": "business_answer",
                "required": True,
            }
        ]
    }

    normalized, findings = (
        narrative_workflow_module._normalize_initial_writer_output_for_delivery(
            output,
            authority_mode="claim_bearing",
            material_projection=projection,
        )
    )

    assert normalized["blocks"][0]["requirement_handles"] == ["pr_primary"]
    assert "question_answer_scope_retained_as_partial" in findings
    assert "question_answer_coverage_incomplete" not in findings


def test_writer_resolves_multiple_legal_fact_owners_by_authority_order() -> None:
    authority = _authority_fixture()
    projection = _prepared_projection(authority)
    claim = projection.claims[0]
    projection_with_ambiguous_owners = replace(
        projection,
        claims=(
            claim,
            replace(claim, claim_handle="c_second_legal_owner"),
            replace(
                claim,
                claim_handle="c_wrong_owner",
                evidence_entry_refs=(),
                material_handles=(),
                limitation_handles=(),
            ),
        ),
    )
    block = _claim_block(
        narrative_workflow_module._columnar_material_fact_transport(
            projection.to_writer_payload()
        ),
        role="executive_answer",
        text="The published fact must retain an unambiguous owner.",
    )
    block["claim_handles"].extend(
        ["c_second_legal_owner", "c_wrong_owner"]
    )
    block["material_fact_bindings"][0]["claim_handle"] = "c_wrong_owner"

    normalized, findings = (
        narrative_workflow_module._normalize_initial_writer_output_for_delivery(
            {"blocks": [block]},
            authority_mode=authority.bundle.authority_mode,
            material_projection=projection_with_ambiguous_owners,
        )
    )

    assert normalized["blocks"][0]["material_fact_bindings"][0][
        "claim_handle"
    ] == claim.claim_handle
    assert findings == (
        "fact_binding_owner_normalized",
        "fact_binding_ambiguous_owner_deterministically_resolved",
    )


def test_writer_assembles_ambiguous_global_fact_owner_by_authority_order() -> None:
    authority = _authority_fixture()
    projection = _prepared_projection(authority)
    claim = projection.claims[0]
    projection_with_ambiguous_global_owners = replace(
        projection,
        claims=(
            claim,
            replace(claim, claim_handle="c_second_legal_owner"),
            replace(
                claim,
                claim_handle="c_wrong_owner",
                evidence_entry_refs=(),
                material_handles=(),
                limitation_handles=(),
            ),
        ),
    )
    block = _claim_block(
        narrative_workflow_module._columnar_material_fact_transport(
            projection.to_writer_payload()
        ),
        role="executive_answer",
        text="The original business answer remains intact.",
    )
    block["claim_handles"] = ["c_wrong_owner"]
    block["material_fact_bindings"][0]["claim_handle"] = "c_wrong_owner"

    normalized, findings = (
        narrative_workflow_module._normalize_initial_writer_output_for_delivery(
            {"blocks": [block]},
            authority_mode=authority.bundle.authority_mode,
            material_projection=projection_with_ambiguous_global_owners,
        )
    )

    normalized_block = normalized["blocks"][0]
    assert normalized_block["text"] == "The original business answer remains intact."
    assert normalized_block["material_fact_bindings"][0][
        "claim_handle"
    ] == claim.claim_handle
    assert normalized_block["claim_handles"] == [
        claim.claim_handle,
        "c_second_legal_owner",
        "c_wrong_owner",
    ]
    assert findings == (
        "fact_binding_owner_normalized",
        "fact_binding_ambiguous_owner_deterministically_resolved",
        "fact_binding_global_owner_added",
        "answer_equivalent_claim_coverage_closed",
    )


def test_writer_removes_duplicate_fact_binding_pairs_before_validation() -> None:
    authority = _authority_fixture()
    projection = _prepared_projection(authority)
    block = _claim_block(
        narrative_workflow_module._columnar_material_fact_transport(
            projection.to_writer_payload()
        ),
        role="executive_answer",
        text="The same reviewed number may appear twice in prose with one binding.",
    )
    block["material_fact_bindings"].append(
        dict(block["material_fact_bindings"][0])
    )

    normalized, findings = (
        narrative_workflow_module._normalize_initial_writer_output_for_delivery(
            {"blocks": [block]},
            authority_mode=authority.bundle.authority_mode,
            material_projection=projection,
        )
    )

    assert len(normalized["blocks"][0]["material_fact_bindings"]) == 1
    assert findings == ("fact_binding_duplicate_removed",)


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
        accepted_intent_context=_TEST_ACCEPTED_INTENT_CONTEXT,
        accepted_plan_context=_TEST_ACCEPTED_PLAN_CONTEXT,
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


def test_large_authoritative_fact_set_uses_lossless_columnar_transport() -> None:
    authority = _authority_fixture()
    claim = authority.settlement.accepted_claims[0]
    source_material_ref = claim.support_edge_refs[0]
    additional_facts = tuple(
        PublicFactDescriptor.create(
            claim=claim,
            public_name=f"transport_fact_{index:04d}",
            fact_kind="number",
            value=str(index),
            range_end=None,
            unit="count",
            source_material_ref=source_material_ref,
        )
        for index in range(4_000)
    )
    materialization = ReviewedPublicFactMaterialization.create(
        authority_bundle=authority.bundle,
        claim_settlement=authority.settlement,
        review_ref="public-fact-review:columnar-transport",
        reviewed_by="review-policy:aggregate-public-facts-v1",
        public_facts=authority.materialization.public_facts + additional_facts,
        public_limitations=authority.materialization.public_limitations,
    )
    authority = replace(authority, materialization=materialization)
    projection = _prepared_projection(authority)
    full_material = projection.to_writer_payload()
    compact_material = (
        narrative_workflow_module._columnar_material_fact_transport(full_material)
    )
    full_bytes = len(
        json.dumps(
            full_material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    compact_bytes = len(
        json.dumps(
            compact_material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    original_facts = {
        fact.fact_handle
        for material in projection.evidence_materials
        for fact in material.facts
    }
    transported_facts = {
        fact["fact_handle"]
        for material in compact_material["evidence_materials"]
        for fact in _provider_material_facts(material)
    }

    assert full_bytes > NARRATIVE_MESSAGE_ENVELOPE_BYTE_LIMIT
    assert compact_bytes < NARRATIVE_MESSAGE_ENVELOPE_BYTE_LIMIT
    assert transported_facts == original_facts
    client = _FakeNarrativeLLM((_initial_writer, _accept_every_block))
    result = run_narrative_workflow(
        authority_bundle=authority.bundle,
        claim_settlement=authority.settlement,
        evidence_entries=authority.evidence_entries,
        recommendations=authority.recommendations,
        public_materialization=authority.materialization,
        visibility_policy=authority.policy,
        material_projection=projection,
        answer_context=_context(),
        llm_client=client,
        sensitive_output_inspector=_NoSensitiveOutput(),
    )

    assert result.publication_ready is True
    assert len(client.calls) == 1
    assert all(
        call["payload"]["material_projection"]["transport_encoding"]
        == "columnar-material-facts.v1"
        for call in client.calls
    )
    canonical_writer_bytes = len(
        json.dumps(
            result.provider_call_inputs[0].to_provider_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    provider_writer_bytes = len(
        json.dumps(
            client.calls[0]["payload"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    assert provider_writer_bytes < canonical_writer_bytes * 0.80
    assert client.calls[0]["payload"]["_wire"]["reference_encoding"] == (
        "short-authority-alias.v1"
    )
    provider_fact_aliases = {
        row[0]
        for material in client.calls[0]["payload"]["material_projection"][
            "evidence_materials"
        ]
        for row in material["facts"]
    }
    assert len(provider_fact_aliases) == len(original_facts)
    assert all(alias.startswith("f") for alias in provider_fact_aliases)
    assert original_facts.isdisjoint(provider_fact_aliases)
    transport_audit = result.provider_audits[0].audit_payload[
        "provider_transport"
    ]
    assert transport_audit["reference_alias_count"] >= len(original_facts)
    assert transport_audit["writer_output_encoding"] == (
        "compact-narrative-blocks.v2"
    )


def test_oversized_writer_material_is_recompiled_without_dropping_claims() -> None:
    authority = _authority_fixture()
    claim = authority.settlement.accepted_claims[0]
    source_material_ref = claim.support_edge_refs[0]
    additional_facts = tuple(
        PublicFactDescriptor.create(
            claim=claim,
            public_name=f"oversized_transport_fact_{index:05d}",
            fact_kind="number",
            value=str(index),
            range_end=None,
            unit="count",
            source_material_ref=source_material_ref,
        )
        for index in range(9_000)
    )
    materialization = ReviewedPublicFactMaterialization.create(
        authority_bundle=authority.bundle,
        claim_settlement=authority.settlement,
        review_ref="public-fact-review:oversized-transport-repair",
        reviewed_by="review-policy:aggregate-public-facts-v1",
        public_facts=authority.materialization.public_facts + additional_facts,
        public_limitations=authority.materialization.public_limitations,
    )
    authority = replace(authority, materialization=materialization)
    projection = _prepared_projection(authority)
    material_view = projection.to_writer_payload()
    primary = narrative_workflow_module._columnar_material_fact_transport(
        material_view
    )
    repaired = narrative_workflow_module._narrative_material_fact_transport(
        material_view
    )

    assert len(
        json.dumps(
            primary,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ) > NARRATIVE_MESSAGE_ENVELOPE_BYTE_LIMIT
    assert repaired["transport_repair_mode"] == (
        "required-fact-claim-complete.v1"
    )
    assert {
        item["claim_handle"] for item in repaired["claims"]
    } == {
        item.claim_handle for item in projection.claims
    }
    required_fact_handles = {
        handle
        for requirement in projection.publication_requirements
        for handle in requirement.required_fact_handles
    }
    transported_fact_handles = {
        fact["fact_handle"]
        for material in repaired["evidence_materials"]
        for fact in _provider_material_facts(material)
    }
    assert required_fact_handles <= transported_fact_handles
    assert len(transported_fact_handles) < len(
        {
            fact.fact_handle
            for material in projection.evidence_materials
            for fact in material.facts
        }
    )


def test_oversized_verifier_request_is_audited_after_one_writer_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority_fixture()
    client = _FakeNarrativeLLM((_initial_writer,))
    monkeypatch.setattr(
        narrative_workflow_module,
        "_VERIFIER_SYSTEM_PROMPT",
        "v" * (NARRATIVE_MESSAGE_ENVELOPE_BYTE_LIMIT + 1),
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
    assert tuple(item.purpose for item in result.provider_call_inputs) == (
        "narrative_writer",
    )
    quality_audit = _run_quality_audit(authority, result, client)
    assert quality_audit.verifier_report.audit_status == "unavailable"
    assert (
        quality_audit.verifier_report.failure_kind
        == "narrative_input_budget_exceeded"
    )
    assert quality_audit.verifier_report.retryability == "not_retryable"
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
def test_final_synthesis_may_select_key_findings_without_repeating_every_obligation(
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

    narrative_workflow_module._initial_writer_validator(
        {"blocks": blocks},
        authority_mode="claim_bearing",
        material_projection=projection,
    )


@pytest.mark.parametrize(
    (
        "role",
        "claim_source",
        "include_recommendation",
        "limitation_source",
        "valid",
    ),
    (
        ("direction", "none", True, "claim", True),
        ("direction", "none", False, "none", False),
        ("direction", "none", False, "claim", False),
        ("boundary", "claim", False, "none", True),
        ("boundary", "none", False, "claim", True),
        ("next_action", "claim", False, "claim", False),
        ("next_action", "none", True, "none", True),
    ),
)
def test_writer_validator_and_narrative_block_share_authority_handle_grammar(
    role: str,
    claim_source: str,
    include_recommendation: bool,
    limitation_source: str,
    valid: bool,
) -> None:
    from tests.phase7 import test_narrative_authority as authority_contracts

    palette, _ = authority_contracts._palette_with_recommendation()
    projection = authority_contracts._material_projection(palette)
    projection_payload = projection.to_writer_payload()
    required_block = _claim_block(
        projection_payload,
        role="executive_answer",
        text="The required answer remains covered by verified evidence.",
    )
    claim = projection_payload["claims"][0]
    recommendation_handles = (
        [projection_payload["recommendations"][0]["recommendation_handle"]]
        if include_recommendation
        else []
    )
    candidate = {
        "role": role,
        "text": "This block exercises the shared authority-handle grammar.",
        "requirement_handles": [],
        "claim_handles": ([claim["claim_handle"]] if claim_source == "claim" else []),
        "recommendation_handles": recommendation_handles,
        "limitation_handles": (
            list(claim["limitation_handles"]) if limitation_source == "claim" else []
        ),
        "material_fact_bindings": [],
        "statement_role": (
            "recommendation" if recommendation_handles else "boundary"
        ),
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
                fact_binding_pairs=frozenset(),
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
    assert requirement_scope["required_fact_binding_options"] == []
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
    assert "boundary_facets" not in required_limitation
    assert set(required_limitation["boundary_facet_handles"]).issubset(
        {
            item.boundary_facet_handle
            for item in mixed_projection.boundary_facets
        }
    )
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


def test_requested_factor_publication_requires_fact_binding_to_its_claim() -> None:
    authority = _authority_fixture()
    projection = _prepared_projection(authority)
    source = projection.publication_requirements[0]
    claim_handle = source.claim_handles[0]
    claim = next(
        item for item in projection.claims if item.claim_handle == claim_handle
    )
    material = next(
        item
        for item in projection.evidence_materials
        if item.material_handle in set(claim.material_handles)
    )
    fact_handle = material.facts[0].fact_handle
    requirement = replace(source, required_fact_handles=(fact_handle,))
    required_projection = replace(
        projection,
        publication_requirements=(requirement,),
    )

    assert not narrative_workflow_module._publication_requirements_covered(
        material_projection=required_projection,
        claim_handles=frozenset({claim_handle}),
        fact_binding_pairs=frozenset(),
        limitation_handles=frozenset(),
    )
    assert narrative_workflow_module._publication_requirements_covered(
        material_projection=required_projection,
        claim_handles=frozenset({claim_handle}),
        fact_binding_pairs=frozenset({(claim_handle, fact_handle)}),
        limitation_handles=frozenset(),
    )
    scope = narrative_workflow_module._requirement_limitation_scope(
        material_projection=required_projection,
        blocks=(),
    )
    assert scope[0]["required_fact_binding_options"] == [
        {
            "fact_handle": fact_handle,
            "claim_handle_options": [claim_handle],
        }
    ]


def test_block_verifier_material_view_excludes_unreferenced_authority() -> None:
    from tests.phase7 import test_narrative_material_projection as projection_contracts

    projection = projection_contracts._derive(projection_contracts._fixture())
    full = projection.to_writer_payload()
    requirement = projection.publication_requirements[0]
    block_payload = _claim_block(
        full,
        role="executive_answer",
        text="The verifier only needs the authority closure used by this block.",
        claim_handle=requirement.claim_handles[0],
    )
    block = narrative_workflow_module._block_from_output(
        block_payload,
        writer_attempt_id="writer-attempt:verifier-scope",
        material_projection=projection,
    )

    scoped = narrative_workflow_module._verification_scoped_material_view(
        material_projection=projection,
        blocks=(block,),
    )

    scoped_claims = {item["claim_handle"] for item in scoped["claims"]}
    assert set(requirement.claim_handles).issubset(scoped_claims)
    assert len(scoped["claims"]) < len(full["claims"])
    material_handles = {
        handle for claim in scoped["claims"] for handle in claim["material_handles"]
    }
    assert {
        item["material_handle"] for item in scoped["evidence_materials"]
    } == material_handles
    limitation_handles = {
        handle
        for claim in scoped["claims"]
        for handle in claim["limitation_handles"]
    }.union(requirement.limitation_handles)
    assert limitation_handles.issubset(
        {item["limitation_handle"] for item in scoped["limitations"]}
    )


def test_verifier_findings_are_advisory_and_do_not_trigger_automatic_rewrite() -> None:
    authority = _authority_fixture()
    client = _FakeNarrativeLLM(
        (
            _initial_writer,
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
    assert result.delivery_narrative is result.narratives[0]
    assert len(result.writer_attempts) == 1
    assert len(result.narratives) == 1
    assert len(client.calls) == 1
    assert tuple(item.purpose for item in result.provider_call_inputs) == (
        "narrative_writer",
    )
    quality_audit = _run_quality_audit(authority, result, client)
    assert len(client.calls) == 2
    assert quality_audit.verifier_report.audit_status == "completed"
    assert quality_audit.verifier_report.rejected_block_ids


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
    assert tuple(block.text for block in result.delivery_narrative.blocks) == (
        mixed_language_text,
        ambiguous_direction_text,
    )
    quality_audit = _run_quality_audit(authority, result, client)
    assert {
        veto.reason_code for veto in quality_audit.verifier_report.vetoes
    } == {
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
        call["prompt_version"] == "single-authority-phase05.v33"
        for call in client.calls
    )


def test_prompts_require_precise_typed_limitation_expression_without_metadata_echo() -> (
    None
):
    writer_prompts = (narrative_workflow_module._WRITER_SYSTEM_PROMPT,)
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
    assert result.delivery_narrative is result.narratives[0]
    assert tuple(item.purpose for item in result.provider_call_inputs) == (
        "narrative_writer",
    )
    quality_audit = _run_quality_audit(authority, result, client)
    assert quality_audit.verifier_report.rejected_block_ids
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
    )
    assert result.publication_ready is True
    assert result.completeness_assessments[0].status == "incomplete"
    assert result.delivery_narrative is result.narratives[0]
    assert (
        result.replay(
            authority_bundle=authority.bundle,
            claim_settlement=authority.settlement,
            evidence_entries=authority.evidence_entries,
            recommendations=authority.recommendations,
        )
        == result
    )


def test_claimless_final_synthesis_requests_one_provider_repair() -> None:
    authority = _authority_fixture()

    class RepairingNarrativeLLM:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []
            self.repair_error: str | None = None

        def invoke_json(self, **kwargs: Any) -> LLMResult:
            payload = json.loads(kwargs["messages"][1]["content"])
            projection = payload["material_projection"]
            limitation = projection["limitations"][0]
            first_output = _compact_fake_writer_output(
                {
                    "blocks": [
                        {
                            "role": "boundary",
                            "text": (
                                "The available evidence has a material boundary."
                            ),
                            "requirement_handles": [],
                            "claim_handles": [],
                            "recommendation_handles": [],
                            "limitation_handles": [
                                limitation["limitation_handle"]
                            ],
                            "material_fact_bindings": [],
                            "statement_role": "evidence_boundary",
                            "required": True,
                        }
                    ]
                }
            )
            validator = kwargs["output_validator"]
            try:
                validator(first_output)
            except LLMOutputError as exc:
                self.repair_error = str(exc)
            else:
                raise AssertionError("claimless_synthesis_was_not_rejected_once")

            final_output = _compact_fake_writer_output(
                _initial_writer(kwargs["task"], payload)
            )
            validator(final_output)
            self.calls.append(
                {
                    **kwargs,
                    "payload": payload,
                    "output": final_output,
                }
            )
            final_raw = json.dumps(
                final_output,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            first_raw = json.dumps(
                first_output,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            return LLMResult(
                output=final_output,
                audit={
                    "provider": "provider:test",
                    "model": "model:narrative-test",
                    "prompt_version": kwargs["prompt_version"],
                    "attempt_count": 2,
                    "response_id": "response:repair:final",
                    "raw_response_content": final_raw,
                    "structured_output": final_output,
                    "attempt_failures": (
                        {
                            "attempt": 1,
                            "response_id": "response:repair:first",
                            "raw_response_content": first_raw,
                        },
                    ),
                },
            )

    client = RepairingNarrativeLLM()
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

    assert (
        client.repair_error
        == "narrative_writer_claim_bearing_synthesis_missing"
    )
    assert result.writer_contract_findings == ()
    assert any(
        block.claim_handles
        for block in result.delivery_narrative.blocks
        if not block.requirement_handles
    )
    assert len(result.provider_responses) == 2
    assert result.publication_ready is True


def test_missing_question_answers_are_the_first_provider_repair_target() -> None:
    assert (
        narrative_workflow_module._narrative_writer_repair_finding(
            (
                "public_claim_coverage_incomplete",
                "question_answer_coverage_incomplete",
            )
        )
        == "question_answer_coverage_incomplete"
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
            "s"
        ]
        == ""
    )
    assert result.delivery_narrative.blocks[0].statement_role == (
        result.delivery_narrative.blocks[0].role
    )
    assert result.delivery_narrative.blocks[1].required is True
    assert result.publication_ready is True


def test_writer_contract_findings_remain_audit_only_for_customer_projection() -> None:
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

    assert flow.customer_payload["warnings"] == []
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


def test_writer_rejects_a_synthesized_short_authority_alias() -> None:
    authority = _authority_fixture()

    def synthesized_alias_writer(
        _: str,
        __: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return {
            "blocks": [
                {
                    "role": "executive_answer",
                    "text": "This block uses an alias that was never supplied.",
                    "c": ["c999999"],
                    "r": [],
                    "l": [],
                    "f": [["c999999", "f999999"]],
                    "s": "business_finding",
                    "q": True,
                }
            ]
        }

    client = _FakeNarrativeLLM((synthesized_alias_writer,))
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
def test_writer_rejects_unknown_fact_binding_handle_without_retry(
    binding_field: str,
    unknown_handle: str,
) -> None:
    authority = _authority_fixture()
    responder_calls = 0

    def unknown_handle_writer(
        _: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        nonlocal responder_calls
        responder_calls += 1
        initial = _initial_writer(_, payload)
        initial["blocks"][0]["material_fact_bindings"][0][binding_field] = (
            unknown_handle
        )
        return initial

    client = _FakeNarrativeLLM((unknown_handle_writer,))
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

    assert responder_calls == 1
    assert captured.value.kind == "provider_output_invalid"
    assert captured.value.retryability == "not_retryable"


def test_writer_assembles_missing_block_owner_without_dropping_original_text() -> None:
    authority = _authority_fixture()

    def invalid_block_writer(
        _: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        initial = _initial_writer(_, payload)
        initial["blocks"][0]["claim_handles"] = []
        return initial

    client = _FakeNarrativeLLM((invalid_block_writer,))
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

    assert len(client.calls) == 1
    assert len(result.delivery_narrative.blocks) == 2
    assert result.delivery_narrative.blocks[0].text.endswith("original.  ")
    assert result.delivery_narrative.blocks[0].claim_handles
    assert result.writer_contract_findings == (
        "fact_binding_owner_added_to_block",
    )
    assert result.publication_ready is True


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
    _run_quality_audit(authority, result, client)
    assert len(client.calls[1]["payload"]["blocks"]) == 1
    assert {item.code for item in result.local_reports[0].issues} == {
        "sensitive_output_policy_violation"
    }
    assert len(client.calls) == 2


def test_boundary_only_writer_is_limitation_bound_and_cannot_add_claims() -> None:
    authority = _authority_fixture(boundary_only=True)

    def boundary_writer(_: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        limitation = payload["material_projection"]["limitations"][0]
        return {
            "blocks": [
                {
                    "role": "boundary",
                    "text": "Current authority supports only this explicit limitation.",
                    "requirement_handles": [],
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
    block = result.delivery_narrative.blocks[0]
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


def _result_fixture() -> tuple[_AuthorityFixture, _FakeNarrativeLLM, Any]:
    authority = _authority_fixture()
    client = _FakeNarrativeLLM(
        (
            _initial_writer,
            _veto_role("dimension_localization"),
        ),
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
        answer_context=_context(),
        llm_client=client,
        sensitive_output_inspector=_NoSensitiveOutput(),
    )
    return authority, client, result


def test_result_replay_reconstructs_every_nested_artifact_without_llm() -> None:
    authority, client, result = _result_fixture()
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
    assert len(replayed.provider_responses) == 2
    assert len(replayed.provider_audits) == 1
    assert len(replayed.writer_attempts) == 1
    assert len(replayed.narratives) == 1
    assert len(replayed.local_reports) == 1
    assert tuple(item.purpose for item in replayed.provider_call_inputs) == (
        "narrative_writer",
    )
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


def test_background_quality_audit_replays_without_changing_delivery_workflow() -> None:
    authority, client, workflow = _result_fixture()
    assert tuple(item.purpose for item in workflow.provider_call_inputs) == (
        "narrative_writer",
    )
    assert len(client.calls) == 1

    audit = _run_quality_audit(authority, workflow, client)
    replayed = NarrativeQualityAuditResult.from_dict(
        audit.to_dict(),
        narrative_workflow=workflow,
    )

    assert replayed == audit
    assert replayed.verifier_report.audit_status == "completed"
    assert replayed.verifier_report.rejected_block_ids
    assert tuple(item.purpose for item in workflow.provider_call_inputs) == (
        "narrative_writer",
    )
    assert workflow.delivery_narrative is workflow.narratives[0]
    assert len(client.calls) == 2


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


def _tamper_writer_attempt(payload: dict[str, Any]) -> None:
    payload["writer_attempts"][0]["provider_response"]["content"] = '{"tampered":true}'


def _tamper_narrative(payload: dict[str, Any]) -> None:
    payload["narratives"][0]["blocks"][0]["text"] = "Tampered sibling text."


def _tamper_local_report(payload: dict[str, Any]) -> None:
    payload["local_reports"][0]["accepted_block_ids"] = []


@pytest.mark.parametrize(
    "tamper",
    (
        _tamper_response,
        _tamper_call_input,
        _tamper_materialization,
        _tamper_material_projection,
        _tamper_audit,
        _tamper_writer_attempt,
        _tamper_narrative,
        _tamper_local_report,
    ),
)
def test_result_replay_rejects_nested_tampering(
    tamper: Callable[[dict[str, Any]], None],
) -> None:
    authority, _, result = _result_fixture()
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
