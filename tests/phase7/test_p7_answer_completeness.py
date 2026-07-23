from __future__ import annotations

from typing import Any, Mapping

from bi_agent.runtime.llm_client import LLMProviderError
from bi_agent.runtime.narrative_workflow import run_narrative_workflow
from bi_agent.runtime.publication_flow import PublicationFlowResult
from tests.phase7.test_narrative_workflow import (
    _FakeNarrativeLLM,
    _NoSensitiveOutput,
    _accept_every_block,
    _authority_fixture,
    _claim_block,
    _context,
    _initial_writer,
    _prepared_projection,
    _provider_material_facts,
)


def _incomplete_writer(
    task: str,
    payload: Mapping[str, Any],
) -> Mapping[str, Any]:
    output = _initial_writer(task, payload)
    for block in output["blocks"]:
        block["required"] = False
    return output


def _completion_writer(
    _: str,
    payload: Mapping[str, Any],
) -> Mapping[str, Any]:
    focused = payload["answer_context"]["focused_retry"]
    material = payload["material_projection"]
    claims_by_handle = {
        item["claim_handle"]: item for item in material["claims"]
    }
    materials_by_handle = {
        item["material_handle"]: item for item in material["evidence_materials"]
    }
    blocks: list[dict[str, Any]] = []
    for target in focused["retry_targets"]:
        required_coverage = target["required_coverage"]
        claim_handles = list(
            dict.fromkeys(
                handle
                for item in required_coverage
                for handle in item["claim_handle_options"][:1]
            )
        )
        limitation_handles = list(
            dict.fromkeys(
                handle
                for item in required_coverage
                for handle in item["required_limitation_handles"]
            )
        )
        bindings = []
        for claim_handle in claim_handles:
            claim = claims_by_handle[claim_handle]
            evidence = materials_by_handle[claim["material_handles"][0]]
            bindings.append(
                {
                    "claim_handle": claim_handle,
                    "fact_handle": _provider_material_facts(evidence)[0][
                        "fact_handle"
                    ],
                }
            )
        blocks.append(
            {
                "text": "补充已验证但首稿遗漏的必答分析材料。",
                "claim_handles": claim_handles,
                "recommendation_handles": [],
                "limitation_handles": limitation_handles,
                "material_fact_bindings": bindings,
                "statement_role": "business_finding",
            }
        )
    return {"blocks": blocks}


class _CompletionFailureLLM(_FakeNarrativeLLM):
    def invoke_json(self, **kwargs: Any):
        if len(self.calls) == 2:
            raise LLMProviderError(
                kind="provider_unavailable",
                retryability="retryable",
            )
        return super().invoke_json(**kwargs)


def _run(client: Any):
    authority = _authority_fixture()
    return authority, run_narrative_workflow(
        authority_bundle=authority.bundle,
        claim_settlement=authority.settlement,
        evidence_entries=authority.evidence_entries,
        recommendations=authority.recommendations,
        public_materialization=authority.materialization,
        visibility_policy=authority.policy,
        material_projection=_prepared_projection(authority),
        answer_context=_context(locale="zh-CN"),
        llm_client=client,
        sensitive_output_inspector=_NoSensitiveOutput(),
    )


def test_incomplete_required_handles_trigger_one_additive_completion_revision() -> None:
    client = _FakeNarrativeLLM(
        (
            _incomplete_writer,
            _accept_every_block,
            _completion_writer,
            _accept_every_block,
        )
    )

    authority, result = _run(client)

    assert result.completion_repair_status == "completed"
    assert result.completion_repair_failure_kind is None
    assert [item.status for item in result.completeness_assessments] == [
        "incomplete",
        "complete",
    ]
    assert len(result.narratives) == 2
    assert result.focused_retry is not None
    assert result.narratives[1].parent_narrative_id == result.narratives[0].narrative_id
    assert tuple(result.narratives[1].blocks[:2]) == result.narratives[0].blocks
    assert len(client.calls) == 4
    assert client.calls[2]["payload"]["answer_context"]["focused_retry"][
        "accepted_sibling_blocks"
    ]
    assert result.replay(
        authority_bundle=authority.bundle,
        claim_settlement=authority.settlement,
        evidence_entries=authority.evidence_entries,
        recommendations=authority.recommendations,
    ) == result


def test_completion_provider_failure_keeps_initial_answer_publishable_with_limit() -> None:
    client = _CompletionFailureLLM(
        (
            _incomplete_writer,
            _accept_every_block,
        )
    )

    authority, result = _run(client)

    assert result.completion_repair_status == "exhausted"
    assert result.completion_repair_failure_kind == "provider_unavailable"
    assert [item.status for item in result.completeness_assessments] == ["incomplete"]
    assert len(result.narratives) == 1
    assert result.focused_retry is None
    assert result.publication_ready is True

    flow = PublicationFlowResult.create(
        authority_inputs=authority.authority_inputs,
        authority_bundle=authority.bundle,
        claim_settlement=authority.settlement,
        recommendations=authority.recommendations,
        narrative_workflow=result,
        supersedes_publication=None,
        destination_ref="gateway:test-customer",
        channel="gateway",
        published_at="2026-07-23T12:00:00Z",
    )
    assert flow.customer_payload["blocks"]
    assert flow.customer_payload["warnings"] == [
        "部分分析要求的表达仍需人工复核，当前内容可作为业务判断参考。"
    ]


def test_complete_initial_narrative_does_not_open_completion_repair() -> None:
    client = _FakeNarrativeLLM((_initial_writer, _accept_every_block))

    _, result = _run(client)

    assert result.completion_repair_status == "not_required"
    assert result.completion_repair_failure_kind is None
    assert [item.status for item in result.completeness_assessments] == ["complete"]
    assert result.focused_retry is None
    assert len(client.calls) == 2
