from __future__ import annotations

from copy import deepcopy
import json
from unittest.mock import patch

from bi_agent.runtime import langgraph_workflow as workflow


def _authority_claim() -> dict:
    return {
        "text": "2026-06-01 付费金额较前一天上涨。",
        "evidence_refs": ["compare_periods:case-b"],
        "numbers": {
            "target_value": 308_240_309,
            "baseline_value": 304_142_630,
            "absolute_change": 4_097_679,
        },
        "scope": "全量用户",
        "time_window": "2026-05-31 至 2026-06-01",
        "claim_type": "comparative_change",
        "claim_strength": "observed",
    }


def _enriched_claim() -> dict:
    return {
        **_authority_claim(),
        "context_manifest_ref": "context-manifest:case-b",
        "reuse_decisions": [{"decision_ref": "reuse-decision:case-b"}],
        "provenance_record_ref": "claim-provenance:case-b",
    }


def _state(*, authority_top_level_context: bool = True) -> dict:
    authority = {
        "evidence_ref": "compare_periods:case-b",
        "capability_id": "compare_periods",
        "evidence_type": "statistical_association",
        "strength": "directional",
        "wording_limit": "quantified",
        "claim_input_ready": True,
        "input_status": "ready",
        "binding_manifest_ref": "capability-binding:compare-periods:case-b",
        "claim_type": "comparative_change",
        "supported_claim_types": ("comparative_change",),
        "supported_evidence_types": ("statistical_association",),
        "maximum_claim_strength": "directional",
        "numeric_facts": dict(_authority_claim()["numbers"]),
        "typed_payload": {
            **_authority_claim()["numbers"],
            "scope": "全量用户",
            "time_window": "2026-05-31 至 2026-06-01",
        },
        "limitations": (),
    }
    if authority_top_level_context:
        authority.update(
            {
                "scope": "全量用户",
                "time_window": "2026-05-31 至 2026-06-01",
            }
        )
    return {
        "request": {
            "run_mode": "production",
            "context_manifest": {"manifest_id": "context-manifest:case-b"},
            "reuse_decisions": [{"decision_ref": "reuse-decision:case-b"}],
        },
        "run_id": "run-claim-provider-boundary",
        "intent": {
            "question_family": "paid_amount_change_explanation",
            "target_metric": "paid_amount",
            "pattern_family": "custom_baseline",
            "scope": "全量用户",
            "time_window": "2026-05-31 至 2026-06-01",
            "target": {"label": "2026-06-01"},
            "baseline": {"label": "2026-05-31"},
        },
        "evidence": [authority],
        "evidence_brief": {},
        "evidence_interpretation": {},
        "answer_text": "待审计答案。",
        "draft_claims": [_enriched_claim()],
        "semantic_audit": {
            "audit_status": "needs_revision",
            "issues": ["需要收敛归因措辞。"],
        },
        "verifier": {},
        "retry_context": {},
        "answer_repair_attempts": 0,
        "checkpoint_events": [],
        "analysis_route": {},
    }


def test_semantic_audit_receives_business_projection_without_claim_authority():
    state = _state()
    captured = {}

    def invoke(_state, task, payload, **_kwargs):
        assert task == "semantic_audit"
        captured.update(deepcopy(payload))
        return {
            "audit_status": "passed",
            "issues": [],
            "display_summary": "当前文案符合业务证据边界。",
        }

    with patch(
        "bi_agent.runtime.langgraph_workflow._invoke_llm",
        side_effect=invoke,
    ):
        workflow._semantic_audit(state)

    assert set(captured) == {"answerText", "businessContext", "displayReview"}
    visible = json.dumps(captured, ensure_ascii=False)
    for internal in (
        "draft_claims",
        "evidence_ref",
        "capability_id",
        "context_manifest_ref",
        "provenance_record_ref",
    ):
        assert internal not in visible


def test_answer_repair_receives_business_projection_and_preserves_local_claims():
    state = _state()
    captured = {}
    original_claims = deepcopy(state["draft_claims"])
    output = {
        "answer_text": "2026-06-01 付费金额较前一天上涨。",
        "display_summary": "已修正业务文案。",
    }

    def invoke(_state, task, payload, **_kwargs):
        assert task == "answer_repair"
        captured.update(deepcopy(payload))
        return deepcopy(output)

    with patch(
        "bi_agent.runtime.langgraph_workflow._invoke_llm",
        side_effect=invoke,
    ):
        workflow._repair_answer(state)

    assert set(captured) == {"answerText", "businessContext", "displayReview"}
    assert state["draft_claims"] == original_claims
    assert "claims" not in output


def test_delivery_verifier_failure_does_not_call_prose_repair():
    state = _state()
    failed = {
        "status": "failed",
        "admin_audit": {
            "verifier": {
                "status": "failed",
                "errors": [{"code": "number_mismatch"}],
            }
        },
    }
    with patch(
        "bi_agent.runtime.langgraph_workflow.reverify_answer_package_for_delivery",
        return_value=failed,
    ), patch(
        "bi_agent.runtime.langgraph_workflow._build_answer_package_from_state",
        return_value={"status": "draft", "package_ref": "before-repair"},
    ), patch(
        "bi_agent.runtime.langgraph_workflow._invoke_llm",
    ) as invoke:
        delivered = workflow._delivery_reverify_with_answer_repair(state)

    assert delivered == failed
    assert state["workflow_status"] == "failed"
    invoke.assert_not_called()


def test_final_summary_payload_receives_business_projection_without_claim_authority():
    payload = workflow._final_business_summary_payload(_state())

    assert set(payload) == {"draftAnswer", "businessContext", "displayReview"}
    visible = json.dumps(payload, ensure_ascii=False)
    for internal in (
        "draft_claims",
        "evidence_ref",
        "capability_id",
        "context_manifest_ref",
        "provenance_record_ref",
    ):
        assert internal not in visible


def test_final_summary_binding_failure_projects_verified_authority_and_keeps_audit():
    state = _state()
    state["llm_calls"] = []
    state["final_business_summary"] = "促销活动导致付费金额上涨50%。"
    summary_payload = workflow._final_business_summary_payload(state)
    authority_record = workflow._prepublication_narrative_authority_record(state)

    workflow._apply_authority_safe_final_summary(
        state,
        authority_record=authority_record,
        summary_payload=summary_payload,
        reason="final_narrative_binding_provider_failed:test",
    )

    assert state["rejected_final_business_summary"] == (
        "促销活动导致付费金额上涨50%。"
    )
    assert _authority_claim()["text"] in state["final_business_summary"]
    assert "促销活动" not in state["final_business_summary"]
    assert state["final_summary_publication_repair"]["status"] == (
        "authority_projected"
    )
    assert [call["provider"] for call in state["llm_calls"]] == [
        "local_deterministic",
        "local_deterministic",
    ]
    assert state["final_narrative_statement_bindings"]


def test_authority_context_falls_back_to_typed_payload_before_claim_writeback():
    state = _state(authority_top_level_context=False)

    claims = workflow._normalize_authority_claim_candidates(
        [_authority_claim()],
        state,
    )

    assert claims[0]["scope"] == "全量用户"
    assert claims[0]["time_window"] == "2026-05-31 至 2026-06-01"
    assert claims[0]["context_manifest_ref"] == "context-manifest:case-b"
    assert claims[0]["reuse_decisions"] == [
        {"decision_ref": "reuse-decision:case-b"}
    ]
