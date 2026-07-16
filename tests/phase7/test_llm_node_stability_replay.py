from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bi_agent.runtime.llm_prompts import PROMPT_VERSION, build_prompt
from tools.phase7.run_llm_node_stability_replay import (
    _core_factor_text_errors,
    _extract_input_payload,
    _material_signature,
    _task_contract_errors,
    build_job_matrix,
    execute_job_once,
    load_replay_scenarios,
    main,
    revalidate_results,
    reserve_artifact_directory,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
INITIAL_PACKAGE = REPO_ROOT / (
    "artifacts/phase7/human-led-q1/case-b-rerun-11/"
    "human-led-q1-case-b-rerun-11-initial/answer_package.json"
)
RESUME_PACKAGE = REPO_ROOT / (
    "artifacts/phase7/human-led-q1/case-b-rerun-10/"
    "human-led-q1-case-b-rerun-10-resume-01/answer_package.json"
)


def _scenarios():
    return load_replay_scenarios(
        initial_package=INITIAL_PACKAGE,
        resume_package=RESUME_PACKAGE,
    )


def test_replay_scenarios_use_case_b_inputs_and_current_prompts() -> None:
    scenarios = _scenarios()

    assert [scenario.scenario_id for scenario in scenarios] == [
        "business_intent",
        "boundary_decision_unbound",
        "boundary_decision_bound",
        "analysis_route_plan",
        "final_route_narrative",
        "data_coverage_interpretation",
        "next_action",
        "evidence_interpretation",
        "causal_audit",
        "answer_synthesis",
        "semantic_audit",
        "semantic_audit_unsupported_cause",
        "answer_repair",
        "final_business_summary",
        "final_answer_audit",
        "final_answer_audit_unsupported_cause",
        "final_answer_audit_payment_success_overclaim",
    ]
    assert len(scenarios) == 17

    for scenario in scenarios:
        rebuilt = build_prompt(scenario.task, scenario.payload)
        assert scenario.prompt_version == PROMPT_VERSION
        assert scenario.messages == rebuilt.messages
        assert scenario.required_keys == rebuilt.required_keys
        assert scenario.source_input_hash
        assert scenario.source_path in {str(INITIAL_PACKAGE), str(RESUME_PACKAGE)}

    by_id = {scenario.scenario_id: scenario for scenario in scenarios}
    assert by_id["business_intent"].provenance == "exact_replay_payload"
    assert by_id["boundary_decision_unbound"].source_path == str(INITIAL_PACKAGE)
    assert by_id["boundary_decision_bound"].source_path == str(RESUME_PACKAGE)
    assert by_id["analysis_route_plan"].provenance == "derived_task_split"
    assert by_id["analysis_route_plan"].source_task == "analysis_route"
    assert by_id["final_route_narrative"].provenance == "derived_task_split"

    answer_synthesis = by_id["answer_synthesis"]
    assert answer_synthesis.provenance == "derived_business_projection"
    assert answer_synthesis.required_keys == ("answer_text", "display_summary")
    assert set(answer_synthesis.payload) == {"businessContext"}
    answer_context = answer_synthesis.payload["businessContext"]
    assert set(answer_context) == {
        "questionUnderstanding",
        "analysisPath",
        "evidence",
        "causalBoundary",
        "answerShape",
    }
    assert len(answer_context["evidence"]["claimSlots"]) == 2
    serialized_answer_context = json.dumps(answer_context, ensure_ascii=False)
    for internal_name in (
        "draft_claims",
        "evidence_ref",
        "formula_component_contribution",
        "driver_decomposition",
    ):
        assert internal_name not in serialized_answer_context

    projected_shapes = {
        "evidence_interpretation": {"businessContext"},
        "causal_audit": {"businessContext", "causalReview"},
        "semantic_audit": {"answerText", "businessContext", "displayReview"},
        "semantic_audit_unsupported_cause": {
            "answerText",
            "businessContext",
            "displayReview",
        },
        "answer_repair": {"answerText", "businessContext", "displayReview"},
        "final_business_summary": {
            "draftAnswer",
            "businessContext",
            "displayReview",
        },
        "final_answer_audit": {
            "finalAnswer",
            "businessContext",
            "displayReview",
        },
        "final_answer_audit_unsupported_cause": {
            "finalAnswer",
            "businessContext",
            "displayReview",
        },
        "final_answer_audit_payment_success_overclaim": {
            "finalAnswer",
            "businessContext",
            "displayReview",
        },
    }
    for scenario_id, expected_keys in projected_shapes.items():
        scenario = by_id[scenario_id]
        assert scenario.provenance == "derived_business_projection"
        assert set(scenario.payload) == expected_keys
        visible = json.dumps(scenario.payload, ensure_ascii=False)
        assert "2026-06-01" in visible
        assert "2026-05-31" in visible
        for internal_name in (
            "evidence_ref",
            "capability_id",
            "driver_decomposition",
            "formula_component_contribution",
            "compiler_runtime_plan",
            "internal_visible_token",
        ):
            assert internal_name not in visible
        if scenario_id in {"semantic_audit", "final_answer_audit"}:
            assert "维度合同缺失" not in visible
        if scenario_id.startswith("final_answer_audit"):
            anchors = scenario.payload["businessContext"]["reviewAnchors"]
            assert anchors
            assert {anchor["kind"] for anchor in anchors} >= {
                "claim_slot",
                "factor_state",
                "boundary",
            }

    narrative_payload = by_id["final_route_narrative"].payload
    assert set(narrative_payload) == {"route_context"}
    assert narrative_payload["route_context"]["route_steps"]
    narrative_text = str(narrative_payload)
    assert "known_capabilities" not in narrative_text
    assert "compare_periods" not in narrative_text


def test_job_matrix_covers_four_variants_for_every_repeat() -> None:
    scenarios = _scenarios()[:2]

    jobs = build_job_matrix(
        scenarios,
        repeats=3,
        flash_model="deepseek-v4-flash",
        pro_model="deepseek-v4-pro",
    )

    assert len(jobs) == 2 * 4 * 3
    assert len({job.job_id for job in jobs}) == len(jobs)
    assert {
        (job.model, job.thinking)
        for job in jobs
    } == {
        ("deepseek-v4-flash", "enabled"),
        ("deepseek-v4-flash", "disabled"),
        ("deepseek-v4-pro", "enabled"),
        ("deepseek-v4-pro", "disabled"),
    }
    assert {job.repeat for job in jobs} == {1, 2, 3}


def test_job_matrix_can_select_model_tier_and_thinking_mode() -> None:
    scenario = _scenarios()[0]

    jobs = build_job_matrix(
        [scenario],
        repeats=4,
        flash_model="deepseek-v4-flash",
        pro_model="deepseek-v4-pro",
        model_tiers=("pro",),
        thinking_modes=("disabled",),
    )

    assert len(jobs) == 4
    assert {job.model_tier for job in jobs} == {"pro"}
    assert {job.model for job in jobs} == {"deepseek-v4-pro"}
    assert {job.thinking for job in jobs} == {"disabled"}
    assert {job.repeat for job in jobs} == {1, 2, 3, 4}


def test_execute_job_once_calls_provider_once_and_drops_reasoning_content() -> None:
    scenario = _scenarios()[0]
    job = build_job_matrix(
        [scenario],
        repeats=1,
        flash_model="deepseek-v4-flash",
        pro_model="deepseek-v4-pro",
    )[0]
    create_calls: list[dict[str, object]] = []

    class FakeCompletions:
        def create(self, **request):
            create_calls.append(request)
            message = SimpleNamespace(
                content='{"question_family":"paid_amount_change_explanation"}',
                reasoning_content="hidden reasoning must never be persisted",
            )
            usage = SimpleNamespace(
                model_dump=lambda: {
                    "prompt_tokens": 101,
                    "completion_tokens": 17,
                    "total_tokens": 118,
                }
            )
            return SimpleNamespace(
                id="response-1",
                choices=[SimpleNamespace(message=message)],
                usage=usage,
            )

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions())
    )

    result = execute_job_once(job, client=fake_client)

    assert len(create_calls) == 1
    assert result["status"] == "completed"
    assert result["raw_response_content"] == (
        '{"question_family":"paid_amount_change_explanation"}'
    )
    assert result["usage"]["total_tokens"] == 118
    assert result["reasoning_content_present"] is True
    assert "reasoning_content" not in result
    assert "hidden reasoning" not in str(result)
    assert create_calls[0]["extra_body"] == {
        "thinking": {"type": job.thinking}
    }
    assert create_calls[0]["reasoning_effort"] == "high"
    assert "temperature" not in create_calls[0]


def test_case_b_expectations_reject_material_business_failures() -> None:
    jobs = {
        job.scenario_id: job
        for job in build_job_matrix(_scenarios(), repeats=1)
    }
    invalid_outputs = {
        "business_intent": {
            "question_family": "paid_amount_change_explanation",
            "target_metric": "paid_amount",
            "scope": "full_sample",
            "time_window": "2026-06-01",
            "baseline_candidates": ["previous_day"],
            "analysis_requirements": {"claim_intents": []},
        },
        "boundary_decision_unbound": {
            "boundary_status": "clear",
            "clarification_questions": [],
        },
        "boundary_decision_bound": {
            "boundary_status": "needs_question",
            "clarification_questions": ["再次选择基线？"],
        },
        "analysis_route_plan": {
            "requested_nodes": ["compare_periods"],
            "analysis_requirements": {
                "target_metrics": ["paid_amount"],
                "baselines": ["previous_day"],
                "claim_intents": ["comparative_change"],
            },
        },
        "data_coverage_interpretation": {
            "coverage_status": "coverage_gap_but_answerable",
            "business_impact": "缺少分项数据，无法判断主要贡献因素。",
            "decision_summary": "只能回答总量变化。",
        },
        "next_action": {
            "next_action": "degrade",
            "decision_summary": "辅助维度不足，整体降级。",
        },
        "evidence_interpretation": {
            "interpretation": "支付成功率不可用，因此无法判断三项核心因素谁是主因。",
            "decision_summary": "无法判断主因。",
            "evidence_boundary": "核心结论不可用。",
        },
        "causal_audit": {
            "causal_assessment": "insufficient_evidence",
            "publishable_wording": "当前无法发布因素贡献结论。",
        },
        "answer_synthesis": {
            "answer_text": "支付成功率没有影响，因此无法回答三项因素。",
        },
        "semantic_audit": {
            "audit_status": "fail",
            "extracted_claims": [],
            "issues": [],
        },
        "semantic_audit_unsupported_cause": {
            "audit_status": "passed",
            "issues": [],
            "display_summary": "当前文案可以发布。",
        },
        "answer_repair": {
            "answer_text": (
                "2026年6月1日相较2026年5月31日付费金额上涨1.35%。"
                "单笔付费金额是主要贡献项，付费频次形成抵消，付费人数小幅正向贡献。"
                "首充人数贡献尚未量化；支付成功率缺少独立观测，本轮按不变处理。"
                "已确认某项促销活动导致单笔付费金额上升。"
            ),
            "display_summary": "已完成修订。",
        },
        "final_business_summary": {
            "summary_text": (
                "我对问题的理解是：你想确认目标日付费金额变化的主要因素。\n"
                "分析脉络：先比较目标日与前一天，再检查因素贡献。\n"
                "关键发现：当前无法判断主因，已排除付费人数的影响。\n"
                "最终结论：2026年6月1日相较2026年5月31日无法判断主因。\n"
                "需要注意：支付成功率缺少独立观测。"
            ),
            "statement_bindings": [
                {
                    "excerpt": "已排除付费人数的影响",
                    "statement_class": "factor_contribution",
                    "authority_keys": ["付费人数"],
                }
            ],
            "display_summary": "当前无法判断主因。",
        },
        "final_answer_audit": {
            "material_findings": [
                {
                    "code": "unsupported_material_claim",
                    "answer_excerpt": "答案里不存在的活动原因",
                    "context_anchor": {
                        "kind": "boundary",
                        "key": "原因边界",
                    },
                    "edit_action": "weaken",
                    "explanation": "该原因缺少独立证据。",
                }
            ],
            "display_summary": "发现一处原因表述。",
        },
        "final_answer_audit_unsupported_cause": {
            "material_findings": [],
            "display_summary": "未发现问题。",
        },
        "final_answer_audit_payment_success_overclaim": {
            "material_findings": [],
            "display_summary": "未发现问题。",
        },
    }

    for scenario_id, output in invalid_outputs.items():
        errors = _task_contract_errors(jobs[scenario_id], output)
        assert errors, scenario_id


def test_final_summary_stability_contract_rejects_malformed_statement_bindings() -> None:
    job = next(
        job
        for job in build_job_matrix(_scenarios(), repeats=1)
        if job.scenario_id == "final_business_summary"
    )
    output = {
        "summary_text": (
            "我对问题的理解是：核对目标日付费金额变化。\n"
            "分析脉络：先比较目标日与前一天，再检查因素贡献。\n"
            "关键发现：2026年6月1日较2026年5月31日上涨1.35%。\n"
            "最终结论：单笔付费金额贡献126.2%，付费频次贡献-28.2%，"
            "付费人数贡献2.0%。\n"
            "需要注意：首充人数贡献尚未量化；支付成功率缺少独立观测，"
            "本轮按不变处理。"
        ),
        "statement_bindings": "结论1",
        "display_summary": "已完成目标日与前一天的因素核对。",
    }

    assert "final_business_summary_contract_invalid:statement_bindings" in (
        _task_contract_errors(job, output)
    )


def test_case_b_expectations_accept_replay_supported_core_decisions() -> None:
    jobs = {
        job.scenario_id: job
        for job in build_job_matrix(_scenarios(), repeats=1)
    }
    initial_calls = json.loads(INITIAL_PACKAGE.read_text(encoding="utf-8"))[
        "llm_calls"
    ]
    resume_calls = json.loads(RESUME_PACKAGE.read_text(encoding="utf-8"))[
        "llm_calls"
    ]

    def output(calls, task, occurrence=0):
        matches = [call for call in calls if call.get("task") == task]
        return dict(matches[occurrence]["structured_output"])

    plan = output(resume_calls, "analysis_route", 0)
    plan = {
        "requested_nodes": plan["requested_nodes"],
        "analysis_requirements": plan["analysis_requirements"],
    }
    semantic = output(resume_calls, "semantic_audit")
    semantic.pop("extracted_claims", None)
    supported = {
        "business_intent": output(initial_calls, "business_intent"),
        "boundary_decision_unbound": output(
            initial_calls,
            "boundary_decision",
        ),
        "boundary_decision_bound": output(resume_calls, "boundary_decision"),
        "analysis_route_plan": plan,
        "next_action": output(resume_calls, "next_action"),
        "evidence_interpretation": output(
            resume_calls,
            "evidence_interpretation",
        ),
        "causal_audit": {
            "causal_assessment": "not_supported",
            "publishable_wording": (
                "已对账的会计贡献支持单笔付费金额为主要贡献项，"
                "付费频次形成抵消，付费人数提供小幅正向贡献；"
                "当前缺乏独立因果验证，深层业务机制尚未确认。"
            ),
            "supporting_reasons": ["三项组成贡献已经完成对账。"],
            "evidence_limit": "深层原因仍需独立证据。",
            "display_summary": "贡献结论可用，业务机制尚未验证。",
        },
        "answer_synthesis": {
            "answer_text": output(resume_calls, "answer_synthesis")["answer_text"],
        },
        "semantic_audit": semantic,
        "semantic_audit_unsupported_cause": {
            "audit_status": "needs_revision",
            "issues": [
                {
                    "severity": "error",
                    "description": "促销活动原因缺少独立业务证据。",
                }
            ],
            "display_summary": "发现一处未经证据支持的原因表述。",
        },
        "answer_repair": {
                "answer_text": (
                    "2026年6月1日相较2026年5月31日付费金额上涨1.35%。"
                    "单笔付费金额贡献126.2%，付费频次贡献-28.2%，"
                    "付费人数贡献2.0%。"
                    "首充人数贡献尚未量化；支付成功率缺少独立观测，本轮按不变处理。"
                    "现有证据未验证单笔付费金额变化背后的具体业务原因。"
                ),
            "display_summary": "保留已验证贡献，移除未经验证的具体原因。",
        },
        "final_answer_audit": {
            "material_findings": [],
        },
        "final_answer_audit_unsupported_cause": {
            "material_findings": [
                {
                    "code": "unsupported_material_claim",
                    "answer_excerpt": "某项促销活动导致单笔付费金额上升",
                    "context_anchor": {
                        "kind": "boundary",
                        "key": "原因边界",
                    },
                    "edit_action": "remove",
                    "explanation": "当前业务证据没有验证该具体原因。",
                }
            ],
        },
        "final_answer_audit_payment_success_overclaim": {
            "material_findings": [
                {
                    "code": "unsupported_material_claim",
                    "answer_excerpt": "支付成功率实际没有影响",
                    "context_anchor": {
                        "kind": "factor_state",
                        "key": "支付成功率",
                    },
                    "edit_action": "weaken",
                    "explanation": "缺少独立观测只能按不变处理，不能写成没有影响。",
                }
            ],
        },
    }

    for scenario_id, candidate in supported.items():
        assert _task_contract_errors(jobs[scenario_id], candidate) == [], scenario_id


def test_answer_synthesis_rejects_provider_owned_canonical_claims() -> None:
    job = next(
        job
        for job in build_job_matrix(_scenarios(), repeats=1)
        if job.scenario_id == "answer_synthesis"
    )
    replay_output = next(
        call["structured_output"]
        for call in json.loads(RESUME_PACKAGE.read_text(encoding="utf-8"))["llm_calls"]
        if call.get("task") == "answer_synthesis"
    )

    errors = _task_contract_errors(job, replay_output)

    assert "answer_synthesis_returned_canonical_claims" in errors


def test_causal_audit_scores_mechanism_boundary_without_requiring_number_recopy() -> None:
    job = next(
        job
        for job in build_job_matrix(_scenarios(), repeats=1)
        if job.scenario_id == "causal_audit"
    )
    output = {
        "causal_assessment": "not_supported",
        "publishable_wording": (
            "已对账的会计贡献支持单笔付费金额为主要贡献项，"
            "付费频次形成抵消，付费人数提供小幅正向贡献；"
            "当前深层业务机制缺乏独立证据，无法确认具体原因。"
        ),
        "supporting_reasons": ["三项组成贡献已经完成对账。"],
        "evidence_limit": "深层原因仍需独立证据。",
        "display_summary": "贡献结论可用，业务机制尚未验证。",
    }

    assert job.required_keys == (
        "causal_assessment",
        "publishable_wording",
        "supporting_reasons",
        "evidence_limit",
        "display_summary",
    )
    assert _task_contract_errors(job, output) == []


def test_auxiliary_observation_gap_does_not_look_like_zero_factor_effect() -> None:
    text = (
        "付费金额上涨1.35%，单笔付费金额贡献126.2%，付费频次贡献-28.2%，"
        "付费人数贡献2.0%。首充相关指标存在观测缺口，但未影响核心三因素结论；"
        "首充人数自身贡献尚未量化。支付成功率缺少独立观测，本轮按不变处理。"
    )

    assert "case_b_first_paid_user_effect_overclaimed" not in (
        _core_factor_text_errors(text)
    )


def test_neutral_assumption_boundary_does_not_look_like_proven_zero_effect() -> None:
    text = (
        "付费金额上涨1.35%，单笔付费金额贡献126.2%，付费频次贡献-28.2%，"
        "付费人数贡献2.0%。首充人数贡献尚未量化。支付成功率缺少独立观测，"
        "本轮按不变处理，不视为实际无影响。"
    )

    assert "case_b_payment_success_overclaimed" not in (
        _core_factor_text_errors(text)
    )


def test_warning_only_semantic_revision_is_scored_as_locally_nonblocking() -> None:
    job = next(
        job
        for job in build_job_matrix(_scenarios(), repeats=1)
        if job.scenario_id == "semantic_audit"
    )
    output = {
        "audit_status": "needs_revision",
        "issues": [
            {"severity": "warning", "issue_description": "可优化表达。"},
            {"severity": "info", "issue_description": "可补充提示。"},
        ],
        "display_summary": "结论可保留，仅有表达建议。",
    }

    assert _task_contract_errors(job, output) == []


def test_unsupported_cause_cannot_be_recast_as_an_unanchored_candidate() -> None:
    job = next(
        job
        for job in build_job_matrix(_scenarios(), repeats=1)
        if job.scenario_id == "semantic_audit_unsupported_cause"
    )
    output = {
        "audit_status": "needs_revision",
        "issues": [
            {
                "severity": "error",
                "description": (
                    "该原因没有业务证据，建议改为初步推测可能与促销活动相关。"
                ),
            }
        ],
        "display_summary": "发现一处未经验证的原因表述。",
    }

    assert "case_b_unsupported_cause_recast_as_hypothesis" in (
        _task_contract_errors(job, output)
    )


def test_business_narrative_rejects_camel_case_internal_field() -> None:
    job = next(
        job
        for job in build_job_matrix(_scenarios(), repeats=1)
        if job.scenario_id == "evidence_interpretation"
    )
    output = {
        "interpretation": "当前表述与factorState中的边界一致。",
        "decision_summary": "核心贡献结论保留。",
        "evidence_boundary": "当前仅支持目标日与基准日对比。",
    }

    assert "case_b_business_narrative_unlocalized_token" in (
        _task_contract_errors(job, output)
    )


def test_business_narrative_rejects_numbered_claim_slot_label() -> None:
    job = next(
        job
        for job in build_job_matrix(_scenarios(), repeats=1)
        if job.scenario_id == "evidence_interpretation"
    )
    output = {
        "interpretation": (
            "结论1支持付费金额上涨1.35%。"
            "单笔付费金额贡献126.2%，付费频次贡献-28.2%，付费人数贡献2.0%。"
            "首充人数贡献尚未量化。支付成功率缺少独立观测，本轮按不变处理。"
        ),
        "decision_summary": "目标日相比基准日的业务变化已完成解读。",
        "evidence_boundary": "当前只支持目标日与基准日的单次对比。",
        "display_summary": "当前贡献拆解和因素边界已保留。",
    }

    assert "case_b_business_narrative_claim_slot_label" in _task_contract_errors(
        job,
        output,
    )


def test_evidence_interpretation_preserves_factor_observation_states() -> None:
    job = next(
        job
        for job in build_job_matrix(_scenarios(), repeats=1)
        if job.scenario_id == "evidence_interpretation"
    )
    shared = {
        "decision_summary": (
            "单笔付费金额贡献126.2%，付费频次贡献-28.2%，付费人数贡献2.0%。"
        ),
        "evidence_boundary": (
            "首充人数只观察到变化，贡献尚未量化。"
            "支付成功率缺少独立观测，本轮按不变处理。"
        ),
        "display_summary": "当前贡献拆解和因素边界已保留。",
    }
    candidate_mechanism = {
        **shared,
        "interpretation": (
            "单笔付费金额贡献126.2%，付费频次贡献-28.2%，付费人数贡献2.0%。"
            "首充人数变化可能影响付费人数。"
            "支付成功率缺少独立观测，本轮按不变处理。"
        ),
    }
    unobserved_as_observed = {
        **shared,
        "interpretation": (
            "单笔付费金额贡献126.2%，付费频次贡献-28.2%，付费人数贡献2.0%。"
            "首充人数和支付成功率的变化未量化贡献，仅作为观察项。"
            "支付成功率缺少独立观测，本轮按不变处理。"
        ),
    }
    distinct_clauses = {
        **shared,
        "interpretation": (
            "单笔付费金额贡献126.2%，付费频次贡献-28.2%，付费人数贡献2.0%。"
            "首充人数变化未量化。支付成功率按不变处理。"
            "支付成功率缺少独立观测。"
        ),
    }

    assert "case_b_observed_factor_mechanism_invented" in _task_contract_errors(
        job,
        candidate_mechanism,
    )
    assert "case_b_factor_state_narrative_conflict" in _task_contract_errors(
        job,
        unobserved_as_observed,
    )
    assert "case_b_factor_state_narrative_conflict" not in (
        _task_contract_errors(job, distinct_clauses)
    )


def test_final_audit_narrative_rejects_review_process_jargon() -> None:
    job = next(
        job
        for job in build_job_matrix(_scenarios(), repeats=1)
        if job.scenario_id == "final_answer_audit"
    )
    payload = _extract_input_payload(job.messages)
    final_answer = str(payload["finalAnswer"])
    excerpt = final_answer[: min(12, len(final_answer))]
    anchor = payload["businessContext"]["reviewAnchors"][0]
    output = {
        "material_findings": [
            {
                "code": "claim_paraphrase_unclear",
                "answer_excerpt": excerpt,
                "context_anchor": {
                    "kind": anchor["kind"],
                    "key": anchor["key"],
                },
                "edit_action": "clarify",
                "explanation": "展示检查发现证据锚点需要进一步说明。",
            }
        ],
    }

    assert "case_b_final_audit_process_jargon" in _task_contract_errors(
        job,
        output,
    )


def test_answer_synthesis_rejects_target_date_reused_as_baseline() -> None:
    job = next(
        job
        for job in build_job_matrix(_scenarios(), repeats=1)
        if job.scenario_id == "answer_synthesis"
    )
    output = {
        "answer_text": (
            "2026-06-01付费金额较基准日（2026-06-01）上涨1.35%。"
            "单笔付费金额贡献126.2%，付费频次贡献-28.2%，付费人数贡献2.0%。"
            "首充人数贡献尚未量化。支付成功率缺少独立观测，本轮按不变处理。"
        )
    }

    errors = _task_contract_errors(job, output)

    assert "case_b_baseline_date_missing" in errors
    assert "case_b_baseline_target_date_confused" in errors


def test_material_signature_ignores_legal_plan_order_but_detects_narrative_drift() -> None:
    jobs = {
        job.scenario_id: job
        for job in build_job_matrix(_scenarios(), repeats=1)
    }
    plan_job = jobs["analysis_route_plan"]
    requirements = {
        "target_metrics": ["paid_amount"],
        "requested_components": ["paid_users", "avg_order_amount"],
        "requested_dimensions": [],
        "baselines": ["previous_day"],
        "context_sources": [],
        "dataset_requirements": ["paid_order_success"],
        "diagnostic_tags": ["change_explanation"],
        "claim_intents": [
            "comparative_change",
            "formula_component_contribution",
        ],
        "scope": "full_sample",
    }
    required_nodes = list(
        next(
            scenario
            for scenario in _scenarios()
            if scenario.scenario_id == "analysis_route_plan"
        ).payload["required_capability_ids"]
    )
    first = {
        "requested_nodes": required_nodes,
        "analysis_requirements": requirements,
    }
    second = {
        "requested_nodes": list(reversed(required_nodes)),
        "analysis_requirements": {
            **requirements,
            "requested_components": list(
                reversed(requirements["requested_components"])
            ),
            "claim_intents": list(reversed(requirements["claim_intents"])),
        },
    }

    assert _material_signature(plan_job, first) == _material_signature(
        plan_job,
        second,
    )

    narrative_job = jobs["final_route_narrative"]
    narrative_scenario = next(
        scenario
        for scenario in _scenarios()
        if scenario.scenario_id == "final_route_narrative"
    )
    refs = [
        step["step_ref"]
        for step in narrative_scenario.payload["route_context"]["route_steps"]
    ]
    safe = {
        "route_summary": "先核对上涨是否成立，再按既定步骤检查因素贡献。",
        "sections": [
            {
                "step_ref": ref,
                "route_step": "核对该项业务问题。",
                "expected_evidence": "查看对应业务证据。",
            }
            for ref in refs
        ],
        "decision_summary": "上涨方向仍待数据验证。",
        "display_summary": "先验证方向，再检查因素。",
    }
    drifted = {**safe, "decision_summary": "已经确认上涨，继续检查因素。"}

    assert _material_signature(narrative_job, safe) != _material_signature(
        narrative_job,
        drifted,
    )


def test_revalidation_preserves_raw_output_and_recomputes_business_contract() -> None:
    scenario = next(
        item for item in _scenarios() if item.scenario_id == "business_intent"
    )
    output = {
        "question_family": "paid_amount_change_explanation",
        "target_metric": "paid_amount",
        "pattern_family": "custom_baseline",
        "pattern_params": {},
        "scope": "full_sample",
        "time_window": "2026-06-01",
        "target_claim": "解释用户提出的上涨假设。",
        "baseline_candidates": ["previous_day"],
        "analysis_requirements": {
            "claim_intents": [
                "comparative_change",
                "formula_component_contribution",
            ],
            "claim_intent_roles": {
                "comparative_change": "user_required",
                "formula_component_contribution": "user_required",
            },
        },
        "status_message": "前一天是推荐候选，仍待用户确认。",
        "display_summary": "上涨方向仍待数据验证。",
    }
    raw = json.dumps(output, ensure_ascii=False)
    record = {
        "job_id": "business_intent__flash__thinking_enabled__r01",
        "scenario_id": scenario.scenario_id,
        "task": scenario.task,
        "provenance": scenario.provenance,
        "model_tier": "flash",
        "model": "deepseek-v4-flash",
        "thinking": "enabled",
        "repeat": 1,
        "status": "completed",
        "raw_response_content": raw,
        "validation": {"business_contract_pass": False},
    }

    revalidated = revalidate_results([record], [scenario])

    assert revalidated[0]["raw_response_content"] == raw
    assert revalidated[0]["previous_validation"] == record["validation"]
    assert revalidated[0]["validation"]["business_contract_pass"] is True


def test_reserve_artifact_directory_never_overwrites_existing_run(
    tmp_path: Path,
) -> None:
    first = reserve_artifact_directory(tmp_path, run_token="fixed-token")
    (first / "sentinel.txt").write_text("keep", encoding="utf-8")

    second = reserve_artifact_directory(tmp_path, run_token="fixed-token")

    assert first != second
    assert first.name == "case-b-llm-stability-fixed-token"
    assert second.name == "case-b-llm-stability-fixed-token-02"
    assert (first / "sentinel.txt").read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("flag", ["--dry-run", "--list-scenarios"])
def test_non_live_cli_modes_do_not_require_provider_credentials(
    flag: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            flag,
            "--initial-package",
            str(INITIAL_PACKAGE),
            "--resume-package",
            str(RESUME_PACKAGE),
            "--repeats",
            "2",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"scenario_count": 17' in output
    if flag == "--dry-run":
        assert '"call_count": 136' in output


def test_dry_run_cli_can_select_node_model_thinking_and_repeat_count(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "--dry-run",
            "--initial-package",
            str(INITIAL_PACKAGE),
            "--resume-package",
            str(RESUME_PACKAGE),
            "--node",
            "answer_synthesis",
            "--model-tier",
            "pro",
            "--thinking",
            "disabled",
            "--repeats",
            "7",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scenario_count"] == 1
    assert payload["call_count"] == 7
    assert payload["repeats"] == 7
    assert payload["variants"] == [
        {
            "model_tier": "pro",
            "model": "deepseek-v4-pro",
            "thinking": "disabled",
        }
    ]
