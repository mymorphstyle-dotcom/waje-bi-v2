import json

from bi_agent.runtime.wording import wording_warnings
from bi_agent.runtime.langgraph_workflow import _semantic_audit_requires_revision
from bi_agent.runtime import langgraph_workflow as workflow
from bi_agent.runtime.llm_prompts import build_prompt


def _accounting_evidence():
    return {
        "driver:reconciled": {
            "evidence_type": "accounting_contribution",
            "strength": "high",
            "wording_limit": "quantified",
        }
    }


def _formula_claim(text: str):
    return {
        "text": text,
        "claim_type": "formula_component_contribution",
        "evidence_refs": ["driver:reconciled"],
    }


def test_reconciled_accounting_contribution_allows_driver_and_offset_wording():
    warnings = wording_warnings(
        [
            _formula_claim(
                "驱动分解显示，单笔付费金额是主要贡献项，付费频次形成负向抵消。"
            )
        ],
        _accounting_evidence(),
    )

    assert not any(
        warning["code"] == "causal_wording_without_causal_evidence"
        for warning in warnings
    )


def test_accounting_contribution_still_rejects_unverified_mechanism_wording():
    warnings = wording_warnings(
        [_formula_claim("单笔付费金额提升导致了用户增加付费。")],
        _accounting_evidence(),
    )

    assert any(
        warning["code"] == "causal_wording_without_causal_evidence"
        for warning in warnings
    )


def test_semantic_audit_warning_only_cannot_trigger_answer_repair():
    audit = {
        "audit_status": "needs_revision",
        "issues": [
            {
                "severity": "warning",
                "description": "可以补充更自然的业务表达。",
            }
        ],
    }

    assert _semantic_audit_requires_revision(audit) is False


def test_warning_only_semantic_audit_is_normalized_to_passed_with_provider_status_kept():
    normalized = workflow._normalize_semantic_audit_decision(
        {
            "audit_status": "needs_revision",
            "issues": [
                {
                    "severity": "info",
                    "description": "可以补充一个表达提示。",
                }
            ],
        }
    )

    assert normalized["audit_status"] == "passed"
    assert normalized["provider_audit_status"] == "needs_revision"


def test_semantic_audit_normalization_drops_provider_claim_material():
    normalized = workflow._normalize_semantic_audit_decision(
        {
            "audit_status": "passed",
            "issues": [],
            "display_summary": "当前文案符合业务证据边界。",
            "extracted_claims": ["模型自行提取的声明"],
            "provider_private_field": "不应传播",
        }
    )

    assert normalized == {
        "audit_status": "passed",
        "provider_audit_status": "passed",
        "issues": [],
        "display_summary": "答案与当前业务证据一致，可以进入下一步。",
    }


def test_interpretation_prompt_separates_observation_neutral_assumption_and_contribution():
    text = "\n".join(
        message["content"]
        for message in build_prompt("evidence_interpretation", {}).messages
    )

    assert "observed side metric" in text
    assert "neutral calculation assumption" in text
    assert "contribution has not been quantified" in text
    assert "must not erase the reconciled core-factor ranking" in text
    assert "do not create an uncovered or incomplete accounting contribution" in text
    assert "target-vs-baseline" not in text
    assert "English token vs" not in text


def test_audit_prompts_allow_reconciled_accounting_contribution_language():
    for task in ("semantic_audit", "final_answer_audit"):
        text = "\n".join(
            message["content"] for message in build_prompt(task, {}).messages
        )
        assert "reconciled accounting contribution" in text
        assert "main contribution item" in text
        assert "mechanism causality" in text


def test_business_evidence_projection_exposes_factor_states_without_internal_ids():
    state = {
        "request": {"run_mode": "production"},
        "analysis_route": {},
        "intent": {
            "target_metric": "paid_amount",
            "pattern_family": "custom_baseline",
            "scope": "full_sample",
            "time_window": "2026-06-01",
            "required_claim_intents": ["formula_component_contribution"],
            "candidate_claim_intents": [],
            "baseline": {"label": "前一天"},
            "target": {"label": "目标日"},
        },
        "evidence": [
            {
                "evidence_ref": "driver:ready",
                "capability_id": "driver_decomposition",
                "claim_type": "formula_component_contribution",
                "claim_input_ready": True,
                "binding_manifest_ref": "binding:driver",
                "evidence_type": "accounting_contribution",
                "supported_evidence_types": ["accounting_contribution"],
                "supported_claim_types": ["formula_component_contribution"],
                "maximum_claim_strength": "quantified_contribution",
                "maximum_claim_strength_rank": 3,
                "strength": "high",
                "wording_limit": "quantified",
                "limitations": [],
                "numeric_facts": {
                    "avg_order_amount_contribution": 22.0,
                    "avg_order_amount_contribution_share": 1.1,
                    "paid_frequency_contribution": -4.0,
                    "paid_frequency_contribution_share": -0.2,
                    "paid_users_contribution": 2.0,
                    "paid_users_contribution_share": 0.1,
                    "formula_contribution_total": 20.0,
                },
                "typed_payload": {
                    "decompositions": [
                        {
                            "primary_core_driver": "avg_order_amount",
                            "core_reconciliation_status": "reconciled",
                            "core_factor_contributions": [
                                {
                                    "component_id": "avg_order_amount",
                                    "contribution": 22.0,
                                    "contribution_share": 1.1,
                                },
                                {
                                    "component_id": "paid_frequency",
                                    "contribution": -4.0,
                                    "contribution_share": -0.2,
                                },
                                {
                                    "component_id": "paid_users",
                                    "contribution": 2.0,
                                    "contribution_share": 0.1,
                                },
                            ],
                            "component_changes": [
                                {
                                    "component_id": "avg_order_amount",
                                    "business_name": "单笔付费金额",
                                    "observed": True,
                                    "baseline_value": 100.0,
                                    "target_value": 110.0,
                                    "delta": 10.0,
                                },
                                {
                                    "component_id": "first_paid_users",
                                    "business_name": "首充人数",
                                    "observed": True,
                                    "baseline_value": 20.0,
                                    "target_value": 18.0,
                                    "delta": -2.0,
                                },
                                {
                                    "component_id": "payment_success_rate",
                                    "business_name": "支付成功率",
                                    "observed": False,
                                    "baseline_value": 1.0,
                                    "target_value": 1.0,
                                    "delta": 0.0,
                                },
                            ],
                        }
                    ],
                    "scope": "full_sample",
                    "time_window": "2026-06-01",
                },
            }
        ],
    }

    projection = workflow._business_evidence_context(state)
    visible = json.dumps(projection, ensure_ascii=False)

    assert "单笔付费金额" in visible
    assert "已量化贡献" in visible
    assert "首充人数" in visible
    assert "贡献尚未量化" in visible
    assert "支付成功率" in visible
    assert "缺少独立观测，本轮按不变处理" in visible
    for internal in (
        "driver_decomposition",
        "formula_component_contribution",
        "avg_order_amount",
        "first_paid_users",
        "payment_success_rate",
        "evidence_ref",
    ):
        assert internal not in visible
