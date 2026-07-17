from __future__ import annotations

from bi_agent.runtime import langgraph_workflow as workflow
from bi_agent.runtime.llm_client import _localize_narrative_fields
from bi_agent.runtime.llm_prompts import TASK_REQUIRED_KEYS, _task_rules


def test_final_business_writer_and_authority_binder_have_separate_contracts() -> None:
    assert TASK_REQUIRED_KEYS["final_business_summary"] == ("summary_text",)
    assert TASK_REQUIRED_KEYS["final_narrative_binding"] == (
        "statement_bindings",
    )
    assert "statement_bindings" not in _task_rules("final_business_summary")
    assert "frozenSummary" in _task_rules("final_narrative_binding")


def test_high_value_insight_writing_uses_critical_model_with_thinking() -> None:
    assert workflow.LLM_TASK_PROFILES["answer_synthesis"] == (
        "critical",
        "enabled",
    )
    assert workflow.LLM_TASK_PROFILES["final_business_summary"] == (
        "critical",
        "enabled",
    )
    assert workflow.LLM_TASK_PROFILES["final_narrative_binding"] == (
        "default",
        "disabled",
    )


def test_task_specific_narrative_schema_preserves_reason_lists() -> None:
    output = _localize_narrative_fields(
        {
            "supporting_reasons": [
                "三项会计贡献已经对账。",
                "更深层业务机制仍需独立证据。",
            ]
        }
    )

    assert output["supporting_reasons"] == [
        "三项会计贡献已经对账。",
        "更深层业务机制仍需独立证据。",
    ]


def test_business_answer_context_carries_verified_insight_portfolio(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        workflow,
        "_question_understanding_sentence",
        lambda _state: "理解问题。",
    )
    monkeypatch.setattr(
        workflow,
        "_analysis_path_sentence",
        lambda _state: "完成诊断。",
    )
    monkeypatch.setattr(
        workflow,
        "_business_evidence_context",
        lambda _state: {"claimSlots": [], "factorStates": []},
    )
    portfolio = {
        "counterfactuals": [
            {
                "statement": "移除主导项后，目标指标将转为下降。",
                "evidence_state": "derived",
            }
        ],
        "diagnostic_sufficiency": {
            "decision": "sufficient",
            "reasons": ["主导因素已经完成决策相关下钻。"],
        },
    }

    context = workflow._business_answer_context(
        {"diagnostic_insights": portfolio}
    )

    assert context["insightPortfolio"] == portfolio
    assert "管理结论" in context["answerShape"]
    assert "关键反事实" in context["answerShape"]


def test_diagnostic_sufficiency_prevents_premature_answer_synthesis(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        workflow,
        "_evidence_supports_bounded_answer",
        lambda _state: True,
    )
    monkeypatch.setattr(
        workflow,
        "_pattern_has_negative_answer_evidence",
        lambda _state: False,
    )
    state = {
        "checkpoint_events": [{"node": "decide_next_action"}],
        "next_action": {
            "next_action": "synthesize_answer",
            "decision_summary": "公式贡献已经完成，可以生成答案。",
        },
        "diagnostic_insights": {
            "diagnostic_sufficiency": {
                "decision": "continue",
                "reasons": ["主导因素仍有可执行的地区和城市下钻路线。"],
                "next_routes": ["hierarchical_dimension_localization"],
            }
        },
        "request": {"allow_question_interrupt": False},
        "intent": {"pattern_family": "custom_baseline"},
        "evidence": [],
    }

    route = workflow._route_after_next_action(state)

    assert route == "plan"
    assert state["next_action"]["next_action"] == "continue_evidence"
    assert state["checkpoint_events"][-1]["route"] == (
        "diagnostic_insufficiency_continue"
    )


def test_only_stable_medium_cross_source_evidence_becomes_auxiliary_claim() -> None:
    state = {
        "evidence": [
            {
                "evidence_ref": "association:stable",
                "claim_type": "cross_source_statistical_association",
                "claim_input_ready": True,
                "strength": "medium",
                "wording_limit": "stable_association",
            },
            {
                "evidence_ref": "association:sensitivity",
                "claim_type": "cross_source_statistical_association",
                "claim_input_ready": True,
                "strength": "low",
                "wording_limit": "sensitivity_only",
            },
        ]
    }

    selected = workflow._publishable_auxiliary_claim_evidence(
        state,
        claim_types=("cross_source_statistical_association",),
        excluded_claim_types={},
    )

    assert selected == [
        (
            "cross_source_statistical_association",
            state["evidence"][0],
        )
    ]

    state["evidence"] = [state["evidence"][1]]
    assert workflow._publishable_auxiliary_claim_evidence(
        state,
        claim_types=("cross_source_statistical_association",),
        excluded_claim_types={},
    ) == []
