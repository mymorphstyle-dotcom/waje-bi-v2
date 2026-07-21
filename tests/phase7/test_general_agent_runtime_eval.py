from __future__ import annotations

from types import SimpleNamespace

from evals.general_agent_runtime.run_local import (
    DEFAULT_CASES,
    _evaluate,
    _load_cases,
)


def _clarification_result(*, prompt: str) -> SimpleNamespace:
    pending = {
        "actionRef": "pending-action:one",
        "actionType": "ask_user",
        "prompt": prompt,
        "options": [
            {
                "optionId": "month",
                "label": "对比上月",
                "description": "与上一个完整月份比较。",
                "recommended": True,
            },
            {
                "optionId": "quarter",
                "label": "对比上季度",
                "description": "与上一个完整季度比较。",
                "recommended": False,
            },
        ],
    }
    return SimpleNamespace(
        status="needs_input",
        terminal_admission=None,
        checkpoint_item=None,
        customer_projection=lambda: {"pendingAction": pending},
    )


def test_live_eval_cases_only_assert_current_runtime_contract() -> None:
    cases = _load_cases(DEFAULT_CASES)

    assert {case["caseId"] for case in cases} == {
        "direct_business_explanation",
        "capability_catalog",
        "clear_formula_decomposition",
        "material_comparison_clarification",
        "activity_impact_boundary",
        "outlier_supported_growth",
        "evidence_follow_up",
    }
    assert all("eventualOutcome" not in case["expected"] for case in cases)
    assert next(
        case for case in cases if case["caseId"] == "evidence_follow_up"
    )["expected"]["forbiddenTools"] == [
        "run_bi_analysis",
        "continue_bi_analysis",
    ]


def test_live_eval_scores_typed_chinese_clarification() -> None:
    failures = _evaluate(
        expected={
            "initialAction": "ask_user",
            "requiredTool": "ask_user",
            "customerState": "needs_input",
            "optionCount": {"minimum": 2, "maximum": 3},
            "recommendedOptionCount": 1,
            "customerLanguage": "zh-Hans",
        },
        result=_clarification_result(prompt="请选择用于估算活动增量的比较基线。"),
        inspection={
            "selection": {
                "initialAction": "ask_user",
                "requiredToolName": "ask_user",
            },
            "toolCalls": ["ask_user"],
            "tasks": [],
        },
        tasks_before=0,
    )

    assert failures == []


def test_live_eval_rejects_customer_language_drift() -> None:
    failures = _evaluate(
        expected={
            "initialAction": "ask_user",
            "requiredTool": "ask_user",
            "customerState": "needs_input",
            "optionCount": {"minimum": 2, "maximum": 3},
            "recommendedOptionCount": 1,
            "customerLanguage": "zh-Hans",
        },
        result=_clarification_result(prompt="Choose a baseline."),
        inspection={
            "selection": {
                "initialAction": "ask_user",
                "requiredToolName": "ask_user",
            },
            "toolCalls": ["ask_user"],
            "tasks": [],
        },
        tasks_before=0,
    )

    assert failures == ["customer_language_mismatch"]
