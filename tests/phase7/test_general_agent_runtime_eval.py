from __future__ import annotations

from collections import Counter
import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from evals.general_agent_runtime.run_local import (
    DEFAULT_CASES,
    _advisory_review_package,
    _collect_publication_authority,
    _evaluate,
    _load_cases,
    _numeric_claims,
    _numeric_claim_is_source_supported,
    _validate_case,
    _validate_catalog,
)


def _clarification_result(*, prompt: str) -> SimpleNamespace:
    pending = {
        "actionRef": "pending-action:one",
        "actionType": "ask_user",
        "prompt": prompt,
        "materialDecisionTopics": ["baseline_or_counterfactual"],
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


def test_standard_pack_v1_has_complete_current_contract_coverage() -> None:
    cases = _load_cases(DEFAULT_CASES)
    _validate_catalog(cases, complete=True)

    assert len(cases) == 48
    assert Counter(case["category"] for case in cases) == {
        "business": 24,
        "runtime": 12,
        "security": 4,
        "experience": 8,
    }
    assert Counter(case["execution"]["adapter"] for case in cases) == {
        "agent_live": 10,
        "pytest": 30,
        "playwright": 8,
    }
    live_cases = [
        case for case in cases if case["execution"]["adapter"] == "agent_live"
    ]
    assert {case["caseId"] for case in live_cases} == {
        "direct_business_explanation",
        "capability_catalog",
        "clear_formula_decomposition",
        "material_comparison_clarification",
        "activity_impact_boundary",
        "outlier_supported_growth",
        "evidence_follow_up",
        "challenge_existing_conclusion",
        "boundary_follow_up",
        "explicit_period_and_formula_revision",
    }
    assert all("turn" not in case and "followUp" not in case for case in cases)
    assert all(
        case["execution"]["releaseRepeats"] == 3
        for case in live_cases
        if case["riskTier"] == "critical"
    )
    assert all(
        case["execution"]["awaitTerminal"] is True for case in live_cases
    )
    evidence_turn = next(
        case for case in live_cases if case["caseId"] == "evidence_follow_up"
    )["turns"][0]
    assert evidence_turn["expected"]["forbiddenTools"] == [
        "run_bi_analysis",
        "continue_bi_analysis",
    ]
    assert evidence_turn["expected"]["fidelity"] == {
        "minimumMaterialRefs": 1,
        "minimumNumericClaims": 1,
        "numericClaims": "published_or_derived_source",
        "requirePublicationIntegrity": True,
    }
    assert next(
        case for case in live_cases
        if case["caseId"] == "explicit_period_and_formula_revision"
    )["turns"][0]["expected"]["requiredTool"] == "continue_bi_analysis"
    completed_keys = [
        case["fixture"]["completedThreadKey"]
        for case in live_cases
        if case["fixture"]["threadMode"] == "completed_analysis"
    ]
    assert len(completed_keys) == len(set(completed_keys)) == 4


def test_p7_pack_keeps_structural_repair_and_human_quality_review_separate() -> None:
    cases = _load_cases(
        Path("evals/general_agent_runtime/p7-cases.jsonl")
    )
    _validate_catalog(cases, complete=False)

    assert len(cases) == 11
    assert Counter(case["execution"]["adapter"] for case in cases) == {
        "agent_live": 1,
        "pytest": 10,
    }
    live = next(
        case for case in cases if case["execution"]["adapter"] == "agent_live"
    )
    assert live["advisoryReview"]["mode"] == "human_advisory"
    assert live["advisoryReview"]["turnIds"] == [
        "initial",
        "omitted_material_follow_up",
        "boundary_challenge",
    ]
    targets = {
        case["execution"].get("target")
        for case in cases
        if case["execution"]["adapter"] == "pytest"
    }
    assert (
        "tests/phase7/test_p7_answer_completeness.py::"
        "test_incomplete_required_handles_trigger_one_additive_completion_revision"
    ) in targets
    assert (
        "tests/phase7/test_narrative_workflow.py::"
        "test_verifier_findings_are_advisory_and_do_not_trigger_automatic_rewrite"
    ) in targets
    assert (
        "tests/phase7/test_agent_runtime_state_authority.py::"
        "test_post_tool_model_failure_delivers_persisted_customer_safe_summary"
    ) in targets
    assert (
        "tests/phase7/test_dynamic_agent_tool_discovery.py::"
        "test_selection_provider_failure_preserves_latest_published_analysis"
    ) in targets


def test_decision_review_requires_actionability_and_stays_advisory() -> None:
    cases = _load_cases(DEFAULT_CASES)
    advisory_cases = [case for case in cases if "advisoryReview" in case]
    assert all(
        case["advisoryReview"]["mode"] == "human_advisory"
        for case in advisory_cases
    )
    assert all(
        "actionability" in case["advisoryReview"]["dimensions"]
        for case in advisory_cases
        if case["advisoryReview"]["decisionCase"]
    )
    assert all(
        "actionability" not in case["advisoryReview"]["dimensions"]
        for case in advisory_cases
        if not case["advisoryReview"]["decisionCase"]
    )
    review = _advisory_review_package(
        {
            "mode": "human_advisory",
            "turnIds": ["answer"],
            "dimensions": ["analysis_completeness", "evidence_boundaries"],
            "decisionCase": False,
            "reviewNote": "等待人工评价。",
        },
        inspections={
            "answer": {
                "fidelity": {
                    "answerText": "结论。\n\n依据。",
                }
            }
        },
    )

    assert review == {
        "mode": "human_advisory",
        "status": "pending_human_review",
        "dimensions": ["analysis_completeness", "evidence_boundaries"],
        "decisionCase": False,
        "reviewNote": "等待人工评价。",
        "observations": [
            {
                "turnId": "answer",
                "characterCount": 8,
                "paragraphCount": 2,
                "hasAnswer": True,
            }
        ],
    }
    result = SimpleNamespace(
        status="completed",
        terminal_admission=SimpleNamespace(
            completion_kind="direct_response",
            authority_refs=(),
        ),
        checkpoint_item=None,
        customer_projection=lambda: {},
    )
    assert _evaluate(
        expected={"customerState": "completed"},
        result=result,
        inspection={"selection": {}, "toolCalls": [], "tasks": []},
        tasks_before=0,
    ) == []


def test_standard_pack_rejects_critical_live_repeat_drift() -> None:
    case = copy.deepcopy(
        next(
            case
            for case in _load_cases(DEFAULT_CASES)
            if case["caseId"] == "clear_formula_decomposition"
        )
    )
    case["execution"]["releaseRepeats"] = 2

    with pytest.raises(ValueError, match="eval_case_live_repeats_invalid"):
        _validate_case(case, 1)


def test_standard_pack_rejects_shared_completed_thread_fixture() -> None:
    cases = copy.deepcopy(_load_cases(DEFAULT_CASES))
    completed = [
        case
        for case in cases
        if (case.get("fixture") or {}).get("threadMode") == "completed_analysis"
    ]
    completed[1]["fixture"]["completedThreadKey"] = completed[0]["fixture"][
        "completedThreadKey"
    ]

    with pytest.raises(ValueError, match="eval_completed_thread_fixture_key_reused"):
        _validate_catalog(cases, complete=False)


def test_fidelity_authority_includes_current_customer_safe_tool_result() -> None:
    authority_refs: set[str] = set()
    authoritative_text: list[str] = []

    _collect_publication_authority(
        {
            "materialRefs": ["publication:one"],
            "artifactRefs": ["claim:one"],
            "customerSummary": "付费频次贡献 29.53亿元，占变动 76.5%。",
            "output": {
                "content": {
                    "facts": [{"value": "29.53", "range_end": None}],
                }
            },
        },
        authority_refs=authority_refs,
        authoritative_text=authoritative_text,
    )

    assert authority_refs == {"publication:one", "claim:one"}
    assert "付费频次贡献 29.53亿元，占变动 76.5%。" in authoritative_text
    assert "29.53" in authoritative_text


def test_numeric_fidelity_allows_only_same_unit_published_differences() -> None:
    source = {"17.96次", "16.0次", "2164.3元", "2128.3元"}

    assert _numeric_claim_is_source_supported(
        "1.96次",
        source,
        allow_derived_difference=True,
    )
    assert _numeric_claim_is_source_supported(
        "-36.0元",
        source,
        allow_derived_difference=True,
    )
    assert not _numeric_claim_is_source_supported(
        "1.96次",
        source,
        allow_derived_difference=False,
    )
    assert not _numeric_claim_is_source_supported(
        "99.9元",
        source,
        allow_derived_difference=True,
    )
    assert not _numeric_claim_is_source_supported(
        "1.96元",
        source,
        allow_derived_difference=True,
    )


def test_numeric_fidelity_inherits_explicit_markdown_table_header_units() -> None:
    answer = """\
总变化为38.62亿。

| 因子 | 贡献（亿） | 占比 |
| --- | ---: | ---: |
| 付费频次 | 29.53 | 76.5% |
| 单笔付费金额 | -4.29 | -11.1% |
"""

    assert _numeric_claims(answer) == {
        "38.62亿",
        "29.53亿",
        "76.5%",
        "-4.29亿",
        "-11.1%",
    }
    assert _numeric_claims("正文中的无单位数字是29.53。") == {"29.53"}


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


def test_live_eval_accepts_typed_published_context_recovery() -> None:
    result = SimpleNamespace(
        status="completed_with_limits",
        error_code="provider_unavailable",
        terminal_admission=SimpleNamespace(
            completion_kind="context_response",
            authority_refs=("publication:one",),
        ),
        checkpoint_item=None,
        customer_projection=lambda: {},
    )

    failures = _evaluate(
        expected={
            "initialActionOneOf": ["respond", "call_tool"],
            "requiredToolOneOf": [
                "inspect_analysis_artifact",
                "explain_claim",
            ],
            "allowPublishedContextRecovery": True,
            "customerStateOneOf": ["completed", "completed_with_limits"],
            "completionKindOneOf": ["context_response", "tool_response"],
            "authorityRequired": True,
            "fidelity": {
                "minimumMaterialRefs": 1,
                "requirePublicationIntegrity": True,
            },
        },
        result=result,
        inspection={
            "selection": {},
            "toolCalls": [],
            "tasks": [],
            "fidelity": {
                "answerText": "保留已发布完整分析。",
                "finalOutput": {"materialRefs": ["publication:one"]},
                "authoritativeNumericClaims": [],
                "publicationCount": 1,
                "publicationIntegrity": True,
            },
        },
        tasks_before=0,
    )

    assert failures == []


def test_live_eval_rejects_unsupported_numbers_and_invalid_publication() -> None:
    result = SimpleNamespace(
        status="completed",
        terminal_admission=SimpleNamespace(
            completion_kind="context_response",
            authority_refs=("publication:one",),
        ),
        checkpoint_item=None,
        customer_projection=lambda: {},
    )

    failures = _evaluate(
        expected={
            "fidelity": {
                "minimumMaterialRefs": 1,
                "minimumNumericClaims": 1,
                "numericClaims": "published_source_subset",
                "requirePublicationIntegrity": True,
            }
        },
        result=result,
        inspection={
            "selection": {},
            "toolCalls": [],
            "tasks": [],
            "fidelity": {
                "answerText": "付费金额增长 99.9%。",
                "finalOutput": {"materialRefs": ["publication:one"]},
                "authoritativeNumericClaims": ["38.62亿"],
                "publicationCount": 1,
                "publicationIntegrity": False,
            },
        },
        tasks_before=0,
    )

    assert failures == [
        "factual_numeric_claim_unsupported",
        "publication_fidelity_invalid",
    ]


def test_live_eval_accepts_source_bounded_factual_explanation() -> None:
    result = SimpleNamespace(
        status="completed",
        terminal_admission=SimpleNamespace(
            completion_kind="context_response",
            authority_refs=("publication:one",),
        ),
        checkpoint_item=None,
        customer_projection=lambda: {},
    )

    failures = _evaluate(
        expected={
            "fidelity": {
                "minimumMaterialRefs": 1,
                "minimumNumericClaims": 1,
                "numericClaims": "published_source_subset",
                "requirePublicationIntegrity": True,
            }
        },
        result=result,
        inspection={
            "selection": {},
            "toolCalls": [],
            "tasks": [],
            "fidelity": {
                "answerText": "付费金额增长 38.62亿。",
                "finalOutput": {"materialRefs": ["publication:one"]},
                "authoritativeNumericClaims": ["38.62亿"],
                "publicationCount": 1,
                "publicationIntegrity": True,
            },
        },
        tasks_before=0,
    )

    assert failures == []


def test_live_eval_checks_exact_publication_mode_when_contract_requests_it() -> None:
    result = SimpleNamespace(
        status="completed",
        terminal_admission=None,
        checkpoint_item=None,
        customer_projection=lambda: {},
    )
    failures = _evaluate(
        expected={"fidelity": {"answerMode": "publication_exact"}},
        result=result,
        inspection={
            "selection": {},
            "toolCalls": [],
            "tasks": [],
            "fidelity": {
                "answerText": "被改写的答案",
                "finalOutput": {},
                "authoritativePublicationText": "权威发布答案",
            },
        },
        tasks_before=0,
    )
    assert failures == ["publication_answer_mismatch"]
