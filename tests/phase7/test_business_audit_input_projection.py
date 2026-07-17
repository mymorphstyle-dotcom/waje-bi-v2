from __future__ import annotations

import json
from copy import deepcopy
from unittest.mock import patch

import pytest

from bi_agent.runtime import langgraph_workflow as workflow
from bi_agent.runtime.llm_prompts import build_prompt


_INTERNAL_TOKENS = (
    "paid_amount",
    "driver_decomposition",
    "formula_component_contribution",
    "paid_amount_factor_formula",
    "dwd_paid_order",
    "evidence:paid-amount:driver",
    "result:paid-amount:internal",
    "sql:paid-amount:internal",
    "capability-binding:paid-amount:internal",
    "internal_visible_token",
)


def test_redundant_trace_summaries_are_not_provider_contract_fields() -> None:
    evidence = build_prompt("evidence_interpretation", {})
    semantic = build_prompt("semantic_audit", {})
    final_audit = build_prompt("final_answer_audit", {})

    assert evidence.required_keys == (
        "interpretation",
        "decision_summary",
        "evidence_boundary",
    )
    assert semantic.required_keys == ("audit_status", "issues")
    assert final_audit.required_keys == ("material_findings",)
    assert "Do not add a second short-summary field" in evidence.messages[-1][
        "content"
    ]
    assert "Return exactly one top-level field: material_findings" in (
        final_audit.messages[-1]["content"]
    )


def test_semantic_audit_display_is_derived_locally() -> None:
    normalized = workflow._normalize_semantic_audit_decision(
        {
            "audit_status": "passed",
            "issues": [],
            "display_summary": "业务文案一致性审计已通过。",
        }
    )

    assert normalized["display_summary"] == (
        "答案与当前业务证据一致，可以进入下一步。"
    )


def _audit_state() -> dict:
    evidence = {
        "evidence_ref": "evidence:paid-amount:driver",
        "capability_id": "driver_decomposition",
        "metric_id": "paid_amount",
        "formula_id": "paid_amount_factor_formula",
        "dataset_id": "dwd_paid_order",
        "claim_type": "formula_component_contribution",
        "claim_input_ready": True,
        "input_status": "ready",
        "binding_manifest_ref": "capability-binding:paid-amount:internal",
        "evidence_type": "accounting_contribution",
        "supported_evidence_types": ["accounting_contribution"],
        "supported_claim_types": ["formula_component_contribution"],
        "maximum_claim_strength": "quantified_contribution",
        "maximum_claim_strength_rank": 3,
        "strength": "high",
        "wording_limit": "quantified",
        "scope": "全量用户",
        "time_window": "2026-05-31 至 2026-06-01",
        "numeric_facts": {
            "paid_amount_change": 4_097_679,
            "avg_order_amount_contribution": 5_100_000,
        },
        "typed_payload": {
            "formula_id": "paid_amount_factor_formula",
            "dataset_id": "dwd_paid_order",
            "scope": "全量用户",
            "time_window": "2026-05-31 至 2026-06-01",
            "decompositions": [
                {
                    "core_reconciliation_status": "reconciled",
                    "core_factor_contributions": [
                        {
                            "component_id": "avg_order_amount",
                            "contribution": 5_100_000,
                            "contribution_share": 1.24,
                        }
                    ],
                    "component_changes": [
                        {
                            "component_id": "avg_order_amount",
                            "business_name": "单笔付费金额",
                            "observed": True,
                            "baseline_value": 63.2,
                            "target_value": 64.1,
                            "delta": 0.9,
                        }
                    ],
                }
            ],
        },
        "limitations": [],
        "result_refs": ["result:paid-amount:internal"],
        "sql_hashes": ["sql:paid-amount:internal"],
    }
    claim = {
        "text": "付费金额较前一天上涨，单笔付费金额是主要正向贡献项。",
        "evidence_refs": ["evidence:paid-amount:driver"],
        "numbers": {"paid_amount_change": 4_097_679},
        "scope": "全量用户",
        "time_window": "2026-05-31 至 2026-06-01",
        "claim_type": "formula_component_contribution",
        "claim_strength": "observed",
    }
    return {
        "request": {
            "run_mode": "production",
            "question": "2026年6月1日付费金额为什么上涨？",
            "compiler_runtime_plan": {
                "capability_id": "driver_decomposition",
                "metric_id": "paid_amount",
                "formula_id": "paid_amount_factor_formula",
                "dataset_id": "dwd_paid_order",
            },
        },
        "intent": {
            "question_family": "paid_amount_change_explanation",
            "target_metric": "paid_amount",
            "pattern_family": "custom_baseline",
            "scope": "全量用户",
            "time_window": "2026-05-31 至 2026-06-01",
            "target": {"label": "2026-06-01"},
            "baseline": {"label": "2026-05-31"},
            "required_claim_intents": ["formula_component_contribution"],
            "candidate_claim_intents": [],
        },
        "analysis_route": {},
        "evidence": [evidence],
        "evidence_brief": {
            "primary_capability": "driver_decomposition",
            "evidence_refs": ["evidence:paid-amount:driver"],
            "result_refs": ["result:paid-amount:internal"],
        },
        "evidence_interpretation": {
            "summary": "单笔付费金额形成主要正向贡献。"
        },
        "answer_text": "付费金额上涨，单笔付费金额是主要正向贡献项。",
        "final_business_summary": (
            "最终结论：付费金额上涨，单笔付费金额是主要正向贡献项。"
        ),
        "draft_claims": [claim],
        "authority_verified_claims": [claim],
        "follow_up_questions": ["要继续查看渠道差异吗？"],
        "validator_results": [
            {"validator": "sensitive_output_policy", "ok": True},
            {"validator": "sql_safety", "ok": True},
        ],
        "verifier": {
            "status": "passed_with_warnings",
            "errors": [],
            "claim_evidence_refs": ["evidence:paid-amount:driver"],
        },
        "semantic_audit": {
            "audit_status": "passed",
            "issues": [
                {
                    "code": "internal_visible_token",
                    "detail": "metric_id=paid_amount",
                }
            ],
        },
        "final_summary_display_warnings": ["internal_visible_token"],
        "retry_context": {},
    }


def _assert_business_audit_payload(payload: dict, *, answer_field: str) -> None:
    visible = json.dumps(payload, ensure_ascii=False)
    leaked = [token for token in _INTERNAL_TOKENS if token in visible]

    assert leaked == [], f"audit payload leaked internal tokens: {leaked}"
    assert set(payload) == {answer_field, "businessContext", "displayReview"}
    assert isinstance(payload["businessContext"], dict)
    assert isinstance(payload["displayReview"], dict)
    assert "付费金额" in visible


def test_semantic_audit_receives_only_business_context_and_display_review() -> None:
    state = _audit_state()
    captured: dict = {}

    def invoke(_state, task, payload, **_kwargs):
        assert task == "semantic_audit"
        captured.update(deepcopy(payload))
        return {"audit_status": "passed", "issues": []}

    with patch(
        "bi_agent.runtime.langgraph_workflow._invoke_llm",
        side_effect=invoke,
    ):
        workflow._semantic_audit(state)

    _assert_business_audit_payload(captured, answer_field="answerText")


def test_final_answer_audit_receives_only_business_context_and_display_review() -> None:
    state = _audit_state()
    captured: dict = {}

    def invoke(_state, task, payload, **_kwargs):
        assert task == "final_answer_audit"
        captured.update(deepcopy(payload))
        return {"material_findings": []}

    with patch(
        "bi_agent.runtime.langgraph_workflow._invoke_llm",
        side_effect=invoke,
    ):
        workflow._final_answer_audit(state)

    _assert_business_audit_payload(captured, answer_field="finalAnswer")
    assert captured["businessContext"]["reviewAnchors"]


def test_final_business_summary_receives_only_business_material() -> None:
    state = _audit_state()

    with patch(
        "bi_agent.runtime.langgraph_workflow._refresh_contract_gap_diagnostics",
        return_value=[],
    ):
        payload = workflow._final_business_summary_payload(state)

    visible = json.dumps(payload, ensure_ascii=False)
    leaked = [token for token in _INTERNAL_TOKENS if token in visible]
    assert leaked == [], f"summary payload leaked internal tokens: {leaked}"
    assert set(payload) == {"draftAnswer", "businessContext", "displayReview"}
    assert "付费金额" in visible


def test_causal_audit_receives_business_projection_only() -> None:
    state = _audit_state()
    state.update({"llm_calls": [], "run_id": "causal-business-projection"})
    captured: dict = {}

    def invoke(_state, task, payload, **_kwargs):
        assert task == "causal_audit"
        captured.update(deepcopy(payload))
        return {
            "causal_assessment": "not_supported",
            "publishable_wording": "会计贡献已经量化，深层业务机制尚未验证。",
            "supporting_reasons": [],
            "evidence_limit": "当前没有独立机制证据。",
            "display_summary": "会计贡献可用，深层业务机制尚未验证。",
        }

    with patch(
        "bi_agent.runtime.langgraph_workflow._invoke_llm",
        side_effect=invoke,
    ):
        workflow._audit_causal_implications(state)

    visible = json.dumps(captured, ensure_ascii=False)
    leaked = [token for token in _INTERNAL_TOKENS if token in visible]
    assert leaked == [], f"causal payload leaked internal tokens: {leaked}"
    assert set(captured) == {"businessContext", "causalReview"}
    assert "会计贡献" in visible
    assert "业务机制" in visible


def test_causal_audit_provider_failure_does_not_block_verified_accounting_answer() -> None:
    state = _audit_state()
    state.update({"llm_calls": [], "run_id": "causal-advisory-failure"})

    with patch(
        "bi_agent.runtime.langgraph_workflow._invoke_llm",
        side_effect=workflow.WorkflowFailure(
            "provider output invalid",
            failure_type="llm",
        ),
    ):
        workflow._audit_causal_implications(state)

    assert state["causal_audit"]["status"] == "unavailable"
    assert "不影响已验证的会计贡献" in state["causal_audit"]["business_boundary"]


def test_causal_audit_normalizes_one_supporting_reason_string() -> None:
    state = _audit_state()
    state.update({"llm_calls": [], "run_id": "causal-string-reason"})

    with patch(
        "bi_agent.runtime.langgraph_workflow._invoke_llm",
        return_value={
            "causal_assessment": "not_supported",
            "publishable_wording": "会计贡献已经量化，深层业务机制尚未验证。",
            "supporting_reasons": "三项会计贡献已经对账。",
            "evidence_limit": "当前没有独立机制证据。",
            "display_summary": "会计贡献可用，深层业务机制尚未验证。",
        },
    ):
        workflow._audit_causal_implications(state)

    assert state["causal_audit"]["supporting_reasons"] == [
        "三项会计贡献已经对账。"
    ]


def test_business_projection_does_not_expose_neutral_assumption_as_observation() -> None:
    state = _audit_state()
    changes = state["evidence"][0]["typed_payload"]["decompositions"][0][
        "component_changes"
    ]
    changes.extend(
        [
            {
                "component_id": "first_paid_users",
                "business_name": "首充人数",
                "observed": True,
                "baseline_value": 100,
                "target_value": 90,
                "delta": -10,
                "delta_ratio": -0.1,
            },
            {
                "component_id": "payment_success_rate",
                "business_name": "支付成功率",
                "observed": False,
                "baseline_value": 1.0,
                "target_value": 1.0,
                "delta": 0.0,
                "delta_ratio": 0.0,
            },
        ]
    )

    factor_states = workflow._business_evidence_context(state)["factorStates"]
    by_factor = {item["factor"]: item for item in factor_states}

    assert by_factor["首充人数"]["baseline"] == 100
    assert by_factor["首充人数"]["target"] == 90
    assert by_factor["支付成功率"] == {
        "factor": "支付成功率",
        "state": "缺少独立观测，本轮按不变处理",
    }


def test_causal_audit_rejects_observed_factor_recast_as_unobserved() -> None:
    state = _audit_state()
    changes = state["evidence"][0]["typed_payload"]["decompositions"][0][
        "component_changes"
    ]
    changes.extend(
        [
            {
                "component_id": "first_paid_users",
                "business_name": "首充人数",
                "observed": True,
                "baseline_value": 100,
                "target_value": 90,
                "delta": -10,
            },
            {
                "component_id": "payment_success_rate",
                "business_name": "支付成功率",
                "observed": False,
                "baseline_value": 1.0,
                "target_value": 1.0,
                "delta": 0.0,
            },
        ]
    )
    state.update({"llm_calls": [], "run_id": "causal-state-drift"})

    with patch(
        "bi_agent.runtime.langgraph_workflow._invoke_llm",
        return_value={
            "causal_assessment": "not_supported",
            "publishable_wording": "会计贡献已经量化，深层业务机制尚未验证。",
            "supporting_reasons": "首充人数和支付成功率缺少独立观测。",
            "evidence_limit": "当前没有独立机制证据。",
            "display_summary": "会计贡献可用，深层业务机制尚未验证。",
        },
    ):
        workflow._audit_causal_implications(state)

    assert state["causal_audit"]["status"] == "unavailable"
    assert "不影响已验证的会计贡献" in state["causal_audit"]["business_boundary"]


def test_evidence_interpretation_state_drift_degrades_only_the_narrative() -> None:
    state = _audit_state()
    changes = state["evidence"][0]["typed_payload"]["decompositions"][0][
        "component_changes"
    ]
    changes.extend(
        [
            {
                "component_id": "first_paid_users",
                "business_name": "首充人数",
                "observed": True,
                "baseline_value": 100,
                "target_value": 90,
                "delta": -10,
            },
            {
                "component_id": "payment_success_rate",
                "business_name": "支付成功率",
                "observed": False,
                "baseline_value": 1.0,
                "target_value": 1.0,
                "delta": 0.0,
            },
        ]
    )
    original_evidence = deepcopy(state["evidence"])

    with patch(
        "bi_agent.runtime.langgraph_workflow._invoke_llm",
        return_value={
            "interpretation": "首充人数和支付成功率的变化未量化。",
            "decision_summary": "核心贡献结论保留。",
            "evidence_boundary": "支付成功率按不变处理。",
            "display_summary": "首充人数和支付成功率的变化未量化。",
        },
    ):
        workflow._interpret_evidence(state)

    assert state["evidence"] == original_evidence
    assert state["evidence_interpretation"]["status"] == "unavailable"
    assert "不影响已验证" in state["evidence_interpretation"]["business_boundary"]


def test_evidence_interpretation_rejects_shared_predicate_across_factor_states() -> None:
    state = _audit_state()
    changes = state["evidence"][0]["typed_payload"]["decompositions"][0][
        "component_changes"
    ]
    changes.extend(
        [
            {
                "component_id": "first_paid_users",
                "business_name": "首充人数",
                "observed": True,
                "baseline_value": 100,
                "target_value": 90,
                "delta": -10,
            },
            {
                "component_id": "payment_success_rate",
                "business_name": "支付成功率",
                "observed": False,
            },
        ]
    )

    with patch(
        "bi_agent.runtime.langgraph_workflow._invoke_llm",
        return_value={
            "interpretation": "首充人数和支付成功率的变化贡献未量化或按不变处理。",
            "decision_summary": "核心贡献结论保留。",
            "evidence_boundary": "当前仅支持目标日与基准日对比。",
        },
    ):
        workflow._interpret_evidence(state)

    assert state["evidence_interpretation"]["status"] == "unavailable"


def test_evidence_interpretation_rejects_generic_mixed_state_summary() -> None:
    state = _audit_state()
    changes = state["evidence"][0]["typed_payload"]["decompositions"][0][
        "component_changes"
    ]
    changes.extend(
        [
            {
                "component_id": "first_paid_users",
                "business_name": "首充人数",
                "observed": True,
                "baseline_value": 100,
                "target_value": 90,
                "delta": -10,
            },
            {
                "component_id": "payment_success_rate",
                "business_name": "支付成功率",
                "observed": False,
            },
        ]
    )

    with patch(
        "bi_agent.runtime.langgraph_workflow._invoke_llm",
        return_value={
            "interpretation": "其他因素贡献未量化或未观测。",
            "decision_summary": "核心贡献结论保留。",
            "evidence_boundary": "当前仅支持目标日与基准日对比。",
        },
    ):
        workflow._interpret_evidence(state)

    assert state["evidence_interpretation"]["status"] == "unavailable"


def test_reconciled_core_contribution_cannot_be_called_partly_uncovered() -> None:
    payload = {
        "businessContext": {
            "factorStates": [
                {
                    "factor": "付费人数",
                    "state": "已量化贡献",
                    "contributionShare": 0.02,
                },
                {
                    "factor": "付费频次",
                    "state": "已量化贡献",
                    "contributionShare": -0.282,
                },
                {
                    "factor": "单笔付费金额",
                    "state": "已量化贡献",
                    "contributionShare": 1.262,
                },
                {
                    "factor": "首充人数",
                    "state": "已观察变化，贡献尚未量化",
                },
                {
                    "factor": "支付成功率",
                    "state": "缺少独立观测，本轮按不变处理",
                },
            ]
        }
    }

    with pytest.raises(
        workflow.LLMOutputError,
        match="accounting_reconciliation_narrative_conflict",
    ):
        workflow._validate_business_factor_state_narrative(
            {
                "evidence_boundary": (
                    "三项贡献份额之和为100%，但首充人数贡献未量化，"
                    "支付成功率按不变处理，因此整体贡献归因存在未覆盖部分。"
                )
            },
            payload,
            fields=("evidence_boundary",),
        )


def test_reconciled_core_contribution_allows_mechanism_evidence_gap() -> None:
    payload = {
        "businessContext": {
            "factorStates": [
                {
                    "factor": "付费人数",
                    "state": "已量化贡献",
                    "contributionShare": 0.02,
                },
                {
                    "factor": "付费频次",
                    "state": "已量化贡献",
                    "contributionShare": -0.282,
                },
                {
                    "factor": "单笔付费金额",
                    "state": "已量化贡献",
                    "contributionShare": 1.262,
                },
            ]
        }
    }

    workflow._validate_business_factor_state_narrative(
        {
            "evidence_boundary": (
                "三项会计贡献已经完整对账，深层业务机制仍缺少独立证据。"
            )
        },
        payload,
        fields=("evidence_boundary",),
    )


@pytest.mark.parametrize(
    ("state_label", "narrative"),
    [
        ("已观察变化，贡献尚未量化", "首充人数本轮按不变处理。"),
        ("已观察变化，贡献尚未量化", "首充人数已观察到变化，按不变处理。"),
        ("已量化贡献", "付费人数的贡献尚未量化。"),
        ("缺少独立观测，本轮按不变处理", "支付成功率变化尚未量化。"),
    ],
)
def test_factor_state_predicates_must_match_authoritative_state(
    state_label: str,
    narrative: str,
) -> None:
    payload = {
        "businessContext": {
            "factorStates": [
                {"factor": "首充人数", "state": state_label}
                if "首充人数" in narrative
                else {"factor": "付费人数", "state": state_label}
                if "付费人数" in narrative
                else {"factor": "支付成功率", "state": state_label}
            ]
        }
    }

    with pytest.raises(workflow.LLMOutputError, match="factor_state_narrative_conflict"):
        workflow._validate_business_factor_state_narrative(
            {"interpretation": narrative},
            payload,
            fields=("interpretation",),
        )


def test_factor_state_validator_accepts_separate_states_and_omissions() -> None:
    payload = {
        "businessContext": {
            "factorStates": [
                {"factor": "首充人数", "state": "已观察变化，贡献尚未量化"},
                {
                    "factor": "支付成功率",
                    "state": "缺少独立观测，本轮按不变处理",
                },
            ]
        }
    }

    workflow._validate_business_factor_state_narrative(
        {
            "interpretation": (
                "首充人数已观察到变化，贡献尚未量化。"
                "支付成功率缺少独立观测，本轮按不变处理。"
            ),
            "decision_summary": "核心贡献结论保留。",
        },
        payload,
        fields=("interpretation", "decision_summary"),
    )


def test_factor_state_validator_accepts_explicit_mixed_states_in_comma_clauses() -> None:
    payload = {
        "businessContext": {
            "factorStates": [
                {"factor": "首充人数", "state": "已观察变化，贡献尚未量化"},
                {
                    "factor": "支付成功率",
                    "state": "缺少独立观测，本轮按不变处理",
                },
            ]
        }
    }

    workflow._validate_business_factor_state_narrative(
        {
            "interpretation": (
                "首充人数已观察到变化，贡献尚未量化，"
                "支付成功率缺少独立观测，本轮按不变处理。"
            )
        },
        payload,
        fields=("interpretation",),
    )


def test_factor_state_validator_resolves_demonstrative_to_same_state_antecedent() -> None:
    payload = {
        "businessContext": {
            "factorStates": [
                {"factor": "付费订单数", "state": "已观察变化，贡献尚未量化"},
                {"factor": "首充人数", "state": "已观察变化，贡献尚未量化"},
                {
                    "factor": "支付成功率",
                    "state": "缺少独立观测，本轮按不变处理",
                },
            ]
        }
    }

    workflow._validate_business_factor_state_narrative(
        {
            "interpretation": (
                "付费订单数变化-513，首充人数变化-364，"
                "这些因素的贡献尚未量化。"
                "支付成功率缺少独立观测，本轮按不变处理。"
            )
        },
        payload,
        fields=("interpretation",),
    )


def test_factor_state_validator_allows_observed_quantified_wording() -> None:
    payload = {
        "businessContext": {
            "factorStates": [
                {"factor": "付费人数", "state": "已量化贡献"},
                {"factor": "付费频次", "state": "已量化贡献"},
            ]
        }
    }

    workflow._validate_business_factor_state_narrative(
        {
            "interpretation": (
                "付费人数和付费频次均已观察到变化，贡献已经量化。"
            )
        },
        payload,
        fields=("interpretation",),
    )


def test_factor_state_validator_does_not_rebind_preserved_core_decomposition() -> None:
    payload = {
        "businessContext": {
            "factorStates": [
                {
                    "factor": "首充人数",
                    "state": "已观察变化，贡献尚未量化",
                },
                {
                    "factor": "支付成功率",
                    "state": "缺少独立观测，本轮按不变处理",
                },
            ]
        }
    }

    workflow._validate_business_factor_state_narrative(
        {
            "evidence_boundary": (
                "首充人数的贡献尚未量化，支付成功率缺少独立观测，"
                "这些因素不改变已量化的三因素贡献合计100%的结论。"
            )
        },
        payload,
        fields=("evidence_boundary",),
    )


def test_factor_state_validator_still_rejects_unobserved_factor_as_quantified() -> None:
    payload = {
        "businessContext": {
            "factorStates": [
                {
                    "factor": "支付成功率",
                    "state": "缺少独立观测，本轮按不变处理",
                }
            ]
        }
    }

    with pytest.raises(
        workflow.LLMOutputError,
        match="factor_state_narrative_conflict:支付成功率",
    ):
        workflow._validate_business_factor_state_narrative(
            {"interpretation": "支付成功率的贡献已经量化。"},
            payload,
            fields=("interpretation",),
        )


def test_factor_state_validator_resolves_parallel_owned_predicates() -> None:
    payload = {
        "businessContext": {
            "factorStates": [
                {
                    "factor": "首充用户占比",
                    "state": "已观察变化，贡献尚未量化",
                },
                {
                    "factor": "支付成功率",
                    "state": "缺少独立观测，本轮按不变处理",
                },
            ]
        }
    }

    workflow._validate_business_factor_state_narrative(
        {
            "evidence_boundary": (
                "当前材料包含首充用户占比的观察变化和"
                "支付成功率的缺失观测。"
            )
        },
        payload,
        fields=("evidence_boundary",),
    )


def test_evidence_interpretation_display_uses_authoritative_claim_text() -> None:
    state = _audit_state()

    with patch(
        "bi_agent.runtime.langgraph_workflow._invoke_llm",
        return_value={
            "interpretation": "当前证据支持目标日相比基准日的变化判断。",
            "decision_summary": "模型自行压缩的短摘要。",
            "evidence_boundary": "当前仅支持目标日与基准日对比。",
        },
    ):
        workflow._interpret_evidence(state)

    display_summary = state["evidence_interpretation"]["display_summary"]
    assert "付费金额较前一天上涨" in display_summary
    assert "单笔付费金额是主要正向贡献项" in display_summary
    assert "模型自行压缩的短摘要" not in display_summary


def test_evidence_interpretation_display_does_not_fallback_to_provider_claim() -> None:
    normalized = workflow._normalize_evidence_interpretation_output(
        {
            "interpretation": "当前没有已验证结论。",
            "decision_summary": "模型自行提出一个业务结论。",
            "evidence_boundary": "当前证据不足。",
        },
        {
            "request": {},
            "intent": {"pattern_family": "custom_baseline"},
            "evidence": [],
        },
    )

    assert normalized["display_summary"] == "当前证据尚未形成可发布的业务结论。"


def test_evidence_interpretation_display_preserves_all_authoritative_claims() -> None:
    state = {
        "request": {},
        "intent": {"pattern_family": "custom_baseline"},
        "evidence": [],
        "authority_verified_claims": [
            {"text": "第一条已验证结论。"},
            {"text": "第二条已验证结论。"},
            {"text": "第三条已验证结论。"},
        ],
    }

    normalized = workflow._normalize_evidence_interpretation_output(
        {
            "interpretation": "已完成解读。",
            "decision_summary": "模型短摘要。",
            "evidence_boundary": "仅保留已验证结论。",
        },
        state,
    )

    assert normalized["display_summary"] == (
        "第一条已验证结论。 第二条已验证结论。 第三条已验证结论。"
    )


def test_final_answer_audit_display_is_derived_locally() -> None:
    state = _audit_state()

    with patch(
        "bi_agent.runtime.langgraph_workflow._invoke_llm",
        return_value={"material_findings": []},
    ):
        audit = workflow._final_answer_audit(state)

    assert audit["display_summary"] == "答案与当前业务证据一致，可以保留。"
    assert "provider_display_summary" not in audit


def test_final_answer_audit_context_includes_exact_verified_comparison() -> None:
    state = _audit_state()
    state["evidence"].insert(
        0,
        {
            "capability_id": "compare_periods",
            "evidence_ref": "evidence:paid-amount:comparison",
            "typed_payload": {
                "target_value": "308240309.0",
                "baseline_value": "304142630.0",
                "absolute_change": "4097679.0",
                "relative_change": "0.0134728860600699",
            },
        },
    )
    state["authority_verified_claims"] = [
        {
            "text": "付费金额从3.0414亿增至3.0824亿。",
            "scope": "全量用户",
            "time_window": "2026-06-01",
        }
    ]

    context = workflow._business_final_audit_context(state)
    exact = [
        item
        for item in context["reviewAnchors"]
        if item["kind"] == "verified_fact"
    ]

    assert len(exact) == 1
    assert "308,240,309.0" in exact[0]["summary"]
    assert "304,142,630.0" in exact[0]["summary"]
    assert "4,097,679.0" in exact[0]["summary"]


def test_business_projection_resolves_generic_window_labels_from_observed_dates() -> None:
    state = _audit_state()
    state["intent"]["target"] = {"label": "target"}
    state["intent"]["baseline"] = {"label": "baseline"}
    state["evidence"].insert(
        0,
        {
            "capability_id": "compare_periods",
            "evidence_type": "statistical_association",
            "typed_payload": {
                "target": {"observation_keys": ["2026-06-01"]},
                "primary_baseline": {
                    "observation_keys": ["2026-05-31"]
                },
            },
        },
    )

    question = workflow._business_evidence_context(state)["question"]
    understanding = workflow._business_answer_context(state)[
        "questionUnderstanding"
    ]

    assert question["target"] == "2026-06-01"
    assert question["baseline"] == "2026-05-31"
    assert "2026-06-01" in understanding
    assert "2026-05-31" in understanding
    assert "target" not in understanding
    assert "baseline" not in understanding
