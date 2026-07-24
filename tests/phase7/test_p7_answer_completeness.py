from __future__ import annotations

from typing import Any, Mapping

import pytest

import bi_agent.runtime.narrative_workflow as narrative_workflow_module
from bi_agent.runtime.llm_client import LLMProviderError
from bi_agent.runtime.narrative_authority import BlockVerifierReport
from bi_agent.runtime.narrative_workflow import (
    NarrativeWorkflowResult,
    run_narrative_workflow,
)
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
    _run_quality_audit,
)


def _incomplete_writer(
    task: str,
    payload: Mapping[str, Any],
) -> Mapping[str, Any]:
    output = _initial_writer(task, payload)
    for block in output["blocks"]:
        block["required"] = False
    return output


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


def test_incomplete_required_handles_are_audited_without_quality_revision() -> None:
    client = _FakeNarrativeLLM((_incomplete_writer, _accept_every_block))

    authority, result = _run(client)

    assert [item.status for item in result.completeness_assessments] == ["incomplete"]
    assert len(result.narratives) == 1
    assert result.delivery_narrative is result.narratives[0]
    assert tuple(item.purpose for item in result.provider_call_inputs) == (
        "narrative_writer",
    )
    assert len(client.calls) == 1
    quality_audit = _run_quality_audit(authority, result, client)
    assert quality_audit.verifier_report.audit_status == "completed"
    assert len(client.calls) == 2
    assert result.publication_ready is True
    assert result.replay(
        authority_bundle=authority.bundle,
        claim_settlement=authority.settlement,
        evidence_entries=authority.evidence_entries,
        recommendations=authority.recommendations,
    ) == result


def test_delivery_workflow_contract_has_no_quality_audit_dependency() -> None:
    assert not hasattr(BlockVerifierReport, "pending")
    assert {
        "verification_attempts",
        "verifier_reports",
        "quality_audit_report",
    }.isdisjoint(NarrativeWorkflowResult.__dataclass_fields__)


def test_incomplete_quality_observation_does_not_add_customer_warning() -> None:
    client = _FakeNarrativeLLM((_incomplete_writer, _accept_every_block))
    authority, result = _run(client)
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
    assert flow.customer_payload["warnings"] == []


def test_complete_initial_narrative_remains_a_single_delivery_version() -> None:
    client = _FakeNarrativeLLM((_initial_writer, _accept_every_block))

    _, result = _run(client)

    assert [item.status for item in result.completeness_assessments] == ["complete"]
    assert result.delivery_narrative is result.narratives[0]
    assert tuple(item.purpose for item in result.provider_call_inputs) == (
        "narrative_writer",
    )
    assert len(client.calls) == 1


def test_quality_verifier_provider_failure_is_audited_without_blocking_delivery() -> (
    None
):
    def unavailable_verifier(
        _: str,
        __: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        raise LLMProviderError(
            kind="provider_unavailable",
            retryability="retryable",
        )

    client = _FakeNarrativeLLM((_incomplete_writer, unavailable_verifier))
    authority, result = _run(client)

    assert result.publication_ready is True
    assert result.delivery_narrative is result.narratives[0]
    assert len(result.provider_audits) == 1
    assert tuple(item.purpose for item in result.provider_call_inputs) == (
        "narrative_writer",
    )
    quality_audit = _run_quality_audit(authority, result, client)
    assert quality_audit.verifier_report.audit_status == "unavailable"
    assert quality_audit.verifier_report.failure_kind == "provider_unavailable"
    assert quality_audit.verifier_report.retryability == "retryable"
    assert quality_audit.verifier_report.technical_detail_ref.startswith(
        "technical-detail:sha256:"
    )
    assert result.replay(
        authority_bundle=authority.bundle,
        claim_settlement=authority.settlement,
        evidence_entries=authority.evidence_entries,
        recommendations=authority.recommendations,
    ) == result

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
    assert flow.customer_payload["warnings"] == []


def test_quality_audit_setup_failure_cannot_change_completed_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeNarrativeLLM((_initial_writer,))
    authority, result = _run(client)
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
    before = flow.to_dict()

    def fail_audit_setup(**_: Any) -> Any:
        raise RuntimeError("injected_quality_audit_setup_failure")

    monkeypatch.setattr(
        narrative_workflow_module,
        "_prepare_verifier_call",
        fail_audit_setup,
    )
    with pytest.raises(
        RuntimeError,
        match="injected_quality_audit_setup_failure",
    ):
        _run_quality_audit(authority, result, client)

    assert flow.to_dict() == before
    assert flow.customer_payload["blocks"]
    assert len(client.calls) == 1
