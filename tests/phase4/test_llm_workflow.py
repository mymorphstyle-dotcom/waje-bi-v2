import json
import tempfile
import unittest

from bi_agent.runtime.compiler import compile_graph
from bi_agent.runtime.exploration_budget import default_budget
from bi_agent.runtime.langgraph_workflow import (
    _capability_path_labels,
    _clarification_policy_gate,
    _default_claim_from_evidence,
    _execute_capabilities,
    _execute_joint_attribution,
    _final_business_summary_fallback,
    _final_summary_needs_display_repair,
    _infer_question_families_from_requested_nodes,
    _normalize_evidence_interpretation_output,
    _align_route_output_to_requested,
    _normalize_route_requested_nodes,
    _repair_path_invents_fixed_future_window,
    _reduce_evidence,
    _route_after_next_action,
    _sanitize_terminal_explanation,
    run_pattern_workflow,
)
from bi_agent.runtime.llm_client import (
    LLMConfigurationError,
    OpenAICompatibleLLMClient,
    _localize_narrative_fields,
)
from bi_agent.runtime.llm_prompts import build_prompt, validate_prompt_specs
from tests.phase4.fake_llm import FakeLLMClient


def _llm_input_payload(answer_package, task):
    call = next(
        item for item in answer_package["admin_audit"]["llm_calls"] if item["task"] == task
    )
    user_message = next(item for item in call["messages"] if item["role"] == "user")
    content = user_message["content"]
    start = content.index("<input_json>") + len("<input_json>")
    end = content.index("</input_json>")
    return json.loads(content[start:end].strip())


class LLMWorkflowTest(unittest.TestCase):
    def test_prompt_specs_are_consistent(self):
        self.assertEqual(validate_prompt_specs(), [])

    def test_conversation_orchestrator_prompt_keeps_llm_in_business_routing_role(self):
        messages = build_prompt("conversation_orchestrator", {"user_message": "check"}).messages
        text = "\n".join(message["content"] for message in messages)

        self.assertIn("Classify one user message inside a BI investigation thread", text)
        self.assertIn("Allowed intent values", text)
        self.assertIn("Allowed topic_relation values", text)
        self.assertIn("Use ask_topic_choice", text)
        self.assertIn("Do not answer the BI question", text)
        self.assertIn("Simplified Chinese business wording", text)

    def test_business_intent_prompt_separates_repeated_patterns_from_baseline_comparison(self):
        messages = build_prompt("business_intent", {"question": "check"}).messages
        text = "\n".join(message["content"] for message in messages)

        self.assertIn("one-off baseline/target comparison", text)
        self.assertIn("repeated time shape", text)
        self.assertIn("weekday-vs-weekday inside many weeks", text)
        self.assertIn("month start/boundary/mid/end inside many months", text)
        self.assertIn("rolling-window trends", text)
        self.assertIn("use business labels", text)
        self.assertIn("付费金额", text)
        self.assertIn("paid_amount", text)

    def test_boundary_decision_prompt_hides_internal_tokens_from_business_text(self):
        messages = build_prompt("boundary_decision", {"intent": {}}).messages
        text = "\n".join(message["content"] for message in messages)

        self.assertIn("shown to business users", text)
        self.assertIn("Do not expose internal field names", text)
        self.assertIn("full_sample", text)
        self.assertIn("mid_phase", text)
        self.assertIn("全样本", text)
        self.assertIn("月中窗口", text)
        self.assertIn("稳定性要求", text)
        self.assertIn("Do not ask only to", text)
        self.assertIn("stable higher", text)
        self.assertIn("month start compared with mid/end", text)
        self.assertIn("exclusive end dates", text)
        self.assertIn("materiality and stability", text)
        self.assertIn("Do not invent p-values", text)
        self.assertIn("confidence levels", text)
        self.assertIn("significance levels", text)
        self.assertIn("重要性", text)
        self.assertIn("Never write 材料性", text)
        self.assertIn("显著性水平", text)

    def test_confirm_understanding_prompt_has_stable_business_and_machine_shape(self):
        messages = build_prompt("confirm_understanding", {"intent": {}}).messages
        text = "\n".join(message["content"] for message in messages)

        self.assertIn("confirmed_intent must be a JSON object", text)
        self.assertIn("business_summary", text)
        self.assertIn("machine_intent", text)
        self.assertIn("never a string", text)
        self.assertIn("status_message and accepted_assumptions are shown", text)
        self.assertIn("do not expose internal field names", text)
        self.assertIn("min_periods", text)

    def test_analysis_route_prompt_filters_by_supported_question_family(self):
        messages = build_prompt("analysis_route", {"intent": {}}).messages
        text = "\n".join(message["content"] for message in messages)

        self.assertIn("supported_question_families", text)
        self.assertIn("Do not request", text)
        self.assertIn("metric_coverage_profile", text)
        self.assertIn("data_quality_profile", text)
        self.assertIn("weekday_calendar_compare", text)
        self.assertIn("compare_period_phases", text)
        self.assertIn("rolling_window_compare", text)
        self.assertIn("Do not add formula", text)
        self.assertIn("p-values", text)

    def test_causal_audit_prompt_assigns_implication_judgment_to_llm(self):
        messages = build_prompt("causal_audit", {"causal_evidence_dossier": {}}).messages
        text = "\n".join(message["content"] for message in messages)

        self.assertIn("Causal Auditor", text)
        self.assertIn("Analyst draft", text)
        self.assertIn("causal_assessment", text)
        self.assertIn("plausible_mechanism", text)
        self.assertIn("candidate_hypothesis", text)
        self.assertIn("mixed_or_confounded", text)
        self.assertIn("publishable_wording", text)
        self.assertIn("Simplified Chinese", text)
        self.assertIn("Do not expose hidden chain-of-thought", text)
        self.assertIn("evidence refs", text)
        self.assertIn("provider metadata", text)

    def test_data_coverage_prompt_uses_result_summary_for_coverage_facts(self):
        messages = build_prompt("data_coverage_interpretation", {"intent": {}}).messages
        text = "\n".join(message["content"] for message in messages)

        self.assertIn("data_result_summary", text)
        self.assertIn("Do not claim complete weeks", text)
        self.assertIn("row_count", text)
        self.assertIn("field_values", text)
        self.assertIn("Keep coverage_status consistent with the narrative", text)
        self.assertIn("do not say the user must confirm", text)
        self.assertIn("sql_hash", text)

    def test_next_action_prompt_uses_business_language_and_stop_rules(self):
        messages = build_prompt("next_action", {"intent": {}}).messages
        text = "\n".join(message["content"] for message in messages)

        self.assertIn("weak_direction", text)
        self.assertIn("below_materiality_floor", text)
        self.assertIn("choose degrade", text)
        self.assertIn("Do not expose", text)
        self.assertIn("internal field names", text)
        self.assertIn("Simplified Chinese", text)

    def test_revenue_health_boundary_defaults_to_low_risk_assumption(self):
        state = {
            "request": {"allow_question_interrupt": True},
            "checkpoint_events": [{"node": "clarification_policy_gate"}],
            "intent": {
                "question_family": "revenue_health_review",
                "primary_question_family": "revenue_health_review",
                "ambiguous_slots": [],
                "target_metric": "paid_amount",
                "scope": "all_users",
                "time_window": "2026-01-01..2026-06-30",
            },
            "boundary_decision": {
                "boundary_status": "needs_question",
                "recommended_assumption": "采用产品默认业务假设继续。",
                "clarification_questions": [
                    {"question": "请选择同比或环比基准。"},
                    {"question": "是否指定细分维度。"},
                ],
            },
        }

        _clarification_policy_gate(state)

        self.assertEqual(
            state["clarification_outcome"]["boundary_status"],
            "low_risk_assumption",
        )
        self.assertEqual(state["checkpoint_events"][-1]["route"], "low_risk_assumption")

    def test_evidence_interpretation_prompt_keeps_boundary_business_readable(self):
        messages = build_prompt("evidence_interpretation", {"intent": {}}).messages
        text = "\n".join(message["content"] for message in messages)

        self.assertIn("evidence_boundary must be a string", text)
        self.assertIn("Do not return an object", text)
        self.assertIn("Do not expose capability ids", text)
        self.assertIn("do not call the result stable or reliable", text)
        self.assertIn("Simplified Chinese", text)

    def test_semantic_audit_prompt_keeps_issue_descriptions_business_readable(self):
        messages = build_prompt("semantic_audit", {"answer_text": "check"}).messages
        text = "\n".join(message["content"] for message in messages)

        self.assertIn("Issue descriptions", text)
        self.assertIn("business-readable Chinese", text)
        self.assertIn("Do not expose internal field names", text)
        self.assertIn("draft_claims", text)
        self.assertIn("evidence_brief", text)
        self.assertIn("wording_limit", text)

    def test_semantic_audit_receives_full_evidence_context(self):
        fake = FakeLLMClient()
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {"artifact_root": tmpdir, "run_id": "semantic-audit-context", "llm_client": fake}
            )

        payload = _llm_input_payload(result.answer_package, "semantic_audit")

        self.assertIn("evidence", payload)
        self.assertIn("answer_context", payload)
        self.assertIn("key_findings", payload["answer_context"])
        self.assertIn("evidence_refs", payload)

    def test_answer_prompts_remove_unlisted_claims_and_action_advice(self):
        for task in ("answer_synthesis", "answer_repair"):
            messages = build_prompt(task, {"answer_context": {}}).messages
            text = "\n".join(message["content"] for message in messages)

            self.assertIn("unlisted claims", text)
            self.assertIn("remove them from answer_text", text)
            self.assertIn("Do not add operational action recommendations", text)

    def test_answer_prompts_block_business_reader_metadata_leaks(self):
        for task in ("answer_synthesis", "answer_repair"):
            messages = build_prompt(task, {"answer_context": {}}).messages
            text = "\n".join(message["content"] for message in messages)

            self.assertIn("raw SQL", text)
            self.assertIn("internal ids", text)
            self.assertIn("enum tokens", text)
            self.assertIn("evidence refs", text)
            self.assertIn("provider metadata", text)

    def test_degraded_prompt_explains_materiality_without_data_volume_drift(self):
        messages = build_prompt("degraded_explanation", {"intent": {}}).messages
        text = "\n".join(message["content"] for message in messages)

        self.assertIn("below_materiality_floor", text)
        self.assertIn("change size", text)
        self.assertIn("not data volume", text)
        self.assertIn("weak_direction", text)
        self.assertIn("fewer valid comparable periods than the run requires", text)
        self.assertIn("Do not suggest changing, adjusting, or relaxing thresholds", text)

    def test_final_summary_prompt_uses_business_wording_for_simple_comparison(self):
        messages = build_prompt("final_business_summary", {"intent": {}}).messages
        text = "\n".join(message["content"] for message in messages)

        self.assertIn("observed increase/decrease", text)
        self.assertIn("do not write statistical association", text)

    def test_degraded_explanation_sanitizes_unsupported_period_and_threshold_advice(self):
        sanitized = _sanitize_terminal_explanation(
            {
                "status": "degraded",
                "explanation": "变化幅度低于重要性阈值，同时可比较期间数量不足，无法确认模式。",
                "owner": "业务分析师",
                "repair_path": "建议调整重要性阈值，或扩大时间窗口。",
            },
            {
                "evidence_brief": {
                    "limitations": ["below_materiality_floor", "weak_direction"],
                }
            },
            "degraded",
        )

        text = " ".join(
            str(sanitized.get(key, "")) for key in ("explanation", "repair_path")
        )
        self.assertNotIn("可比较期间数量不足", text)
        self.assertNotIn("调整重要性阈值", text)
        self.assertIn("变化幅度低于当前重要性阈值", text)

    def test_degraded_explanation_sanitizes_invented_future_window(self):
        sanitized = _sanitize_terminal_explanation(
            {
                "status": "degraded",
                "explanation": "方向不一致且变化幅度低于当前重要性阈值。",
                "owner": "分析团队",
                "repair_path": "延长观察周期至12个月以上后重新评估。",
            },
            {
                "evidence_brief": {
                    "limitations": ["below_materiality_floor", "weak_direction"],
                }
            },
            "degraded",
        )

        self.assertNotIn("12个月", sanitized["repair_path"])
        self.assertIn("继续观察新周期", sanitized["repair_path"])

    def test_degraded_explanation_sanitizes_contract_and_data_collection_drift(self):
        sanitized = _sanitize_terminal_explanation(
            {
                "status": "degraded",
                "explanation": "未发现明确的事件或合同依据，因此模式无法确认。",
                "owner": "分析团队",
                "repair_path": "建议收集更多数据并积累更多月度数据。",
            },
            {
                "evidence_brief": {
                    "limitations": [
                        "below_materiality_floor",
                        "weak_direction",
                        "no_event_contract_or_matches",
                    ],
                }
            },
            "degraded",
        )

        text = " ".join(
            str(sanitized.get(key, "")) for key in ("explanation", "repair_path")
        )
        self.assertNotIn("合同依据", text)
        self.assertNotIn("收集更多数据", text)
        self.assertNotIn("积累更多月度数据", text)
        self.assertIn("补充事件或机制证据", text)

    def test_fixed_future_window_detection_does_not_flag_calendar_dates(self):
        self.assertFalse(
            _repair_path_invents_fixed_future_window(
                "观察窗口覆盖2026年1月1日至6月30日，结论只适用于当前窗口。"
            )
        )
        self.assertFalse(_repair_path_invents_fixed_future_window("仅10%（3个月）支持该假设。"))
        self.assertTrue(_repair_path_invents_fixed_future_window("延长观察周期至12个月以上后重新评估。"))

    def test_missing_llm_env_fails_before_claiming_draft(self):
        with self.assertRaisesRegex(LLMConfigurationError, "missing_llm_model"):
            OpenAICompatibleLLMClient.from_env({})

    def test_llm_narrative_fallback_keeps_machine_tokens(self):
        output = _localize_narrative_fields(
            {
                "status_message": "success",
                "target_metric": "paid_amount",
                "recommended_assumption": "产品默认的材料性和稳定性规则，不使用p值。",
                "route_summary": "使用compare_period_phases和metric_timeseries分析paid_amount，不说显著性。",
                "accepted_assumptions": "scope为full_sample，min_periods=20。",
                "confirmed_intent": {
                    "business_summary": "scope为full_sample，材料性规则。",
                    "machine_intent": {"scope": "full_sample"},
                },
                "decision_summary": (
                    "scope为full_sample，对账单强度按默认处理，"
                    "模式参数min_periods=20，"
                    "使用产品默认的重要性和稳定性规则"
                    "（product default materiality and stability rules），"
                    "超过默认重要性阈值（例如5%）。"
                ),
                "claims": [{"text": "Pattern observed."}],
            }
        )

        self.assertEqual(output["status_message"], "已完成当前业务判断。")
        self.assertEqual(output["target_metric"], "paid_amount")
        self.assertEqual(
            output["recommended_assumption"],
            "产品默认的重要性和稳定性规则，不使用重要性规则。",
        )
        self.assertEqual(
            output["route_summary"],
            "使用周期内阶段对比和指标时间序列分析付费金额，不说重要性。",
        )
        self.assertEqual(output["accepted_assumptions"], ["范围为全样本，至少20个可比周期。"])
        self.assertEqual(
            output["confirmed_intent"]["business_summary"],
            "范围为全样本，重要性规则。",
        )
        self.assertEqual(output["confirmed_intent"]["machine_intent"]["scope"], "full_sample")
        self.assertEqual(
            output["decision_summary"],
            (
                "范围为全样本，结论强度按默认处理，"
                "窗口规则至少20个可比周期，使用产品默认的重要性和稳定性规则，"
                "超过默认重要性阈值。"
            ),
        )
        self.assertEqual(output["claims"][0]["text"], "已生成基于证据的业务表述。")

    def test_llm_narrative_keeps_accepted_assumptions_flat(self):
        output = _localize_narrative_fields(
            {
                "accepted_assumptions": [
                    "假设基线分为月中和月末两个阶段。",
                    "使用产品默认重要性规则。",
                ]
            }
        )

        self.assertEqual(
            output["accepted_assumptions"],
            ["假设基线分为月中和月末两个阶段。", "使用产品默认重要性规则。"],
        )

    def test_llm_narrative_allows_business_entity_tokens(self):
        output = _localize_narrative_fields(
            {
                "description": "所有非FooBar2026渠道的汇总",
                "business_summary": "对比Alpha渠道和Beta渠道的付费金额表现。",
            }
        )

        self.assertEqual(output["description"], "所有非FooBar2026渠道的汇总")
        self.assertEqual(
            output["business_summary"],
            "对比Alpha渠道和Beta渠道的付费金额表现。",
        )

    def test_llm_narrative_removes_invented_default_stability_percent(self):
        output = _localize_narrative_fields(
            {
                "recommended_assumption": (
                    "使用产品默认的稳定性和重要性规则：要求目标渠道的月度日均付费金额"
                    "在至少80%的月份中高于基准渠道，且平均高出比例超过产品默认的重要性阈值。"
                )
            }
        )

        self.assertNotIn("80%", output["recommended_assumption"])
        self.assertIn("足够多的月份", output["recommended_assumption"])

    def test_llm_narrative_removes_invented_period_stability_percent(self):
        output = _localize_narrative_fields(
            {
                "recommended_assumption": (
                    "使用产品默认规则：要求目标版本在至少75%的周期中高于基准版本，"
                    "并超过产品默认的重要性阈值。"
                )
            }
        )

        self.assertNotIn("75%", output["recommended_assumption"])
        self.assertIn("足够多的周期", output["recommended_assumption"])

    def test_llm_narrative_normalizes_next_action_technical_terms(self):
        output = _localize_narrative_fields(
            {
                "decision_summary": (
                    "Pattern证据不足，pattern_status: high，"
                    "中等置信度，medium，allow_question_interrupt=false，"
                    "材料阈值和物质性下限，模式稳定可靠，"
                    "模式确认性高，目标vs基线成立，幅度显著，选择synthesize_answer而非degrade。"
                ),
                "next_action": "synthesize_answer",
            }
        )

        self.assertNotIn("Pattern", output["decision_summary"])
        self.assertNotIn("pattern_status", output["decision_summary"])
        self.assertNotIn("synthesize_answer", output["decision_summary"])
        self.assertNotIn("degrade", output["decision_summary"])
        self.assertNotIn("置信度", output["decision_summary"])
        self.assertNotIn("medium", output["decision_summary"])
        self.assertNotIn("allow_question_interrupt", output["decision_summary"])
        self.assertNotIn("vs", output["decision_summary"])
        self.assertNotIn("材料阈值", output["decision_summary"])
        self.assertNotIn("物质性", output["decision_summary"])
        self.assertNotIn("稳定可靠", output["decision_summary"])
        self.assertNotIn("模式确认性高", output["decision_summary"])
        self.assertNotIn("显著", output["decision_summary"])
        self.assertIn("本次对比证据较强", output["decision_summary"])
        self.assertIn("目标相比基线", output["decision_summary"])
        self.assertIn("重要性阈值", output["decision_summary"])
        self.assertEqual(output["next_action"], "synthesize_answer")

    def test_llm_narrative_preserves_route_summary_after_token_cleanup(self):
        output = _localize_narrative_fields(
            {
                "route_summary": (
                    "首先使用data_quality_profile检查2026年Q1和Q2日均付费金额的数据覆盖和质量，"
                    "然后使用compare_periods直接比较两个季度的日均付费金额，"
                    "最后使用answer_verify验证最终声明。这三者均支持custom_baseline_comparison问题族，"
                    "且成本较低，适合在research模式下优先保证答案质量。"
                )
            }
        )

        self.assertNotEqual(output["route_summary"], "已完成分析路线设计。")
        self.assertNotIn("data_quality_profile", output["route_summary"])
        self.assertNotIn("custom_baseline_comparison", output["route_summary"])
        self.assertNotIn("research", output["route_summary"])
        self.assertNotIn("检查检查", output["route_summary"])
        self.assertIn("数据质量检查能力检查", output["route_summary"])
        self.assertIn("周期对比", output["route_summary"])
        self.assertIn("答案校验", output["route_summary"])

    def test_capability_path_labels_include_public_route_nodes(self):
        self.assertEqual(
            _capability_path_labels(
                ("data_quality_profile", "compare_periods", "answer_verify")
            ),
            "数据质量检查、周期对比、答案校验",
        )

    def test_route_output_alignment_removes_replaced_candidate_paths(self):
        output = _align_route_output_to_requested(
            {
                "requested_nodes": ["data_quality_profile", "compare_periods", "answer_verify"],
                "route_summary": "先用metric_timeseries，再用rolling_window_compare。",
                "expected_evidence": ["metric_timeseries evidence", "rolling_window_compare evidence"],
                "decision_summary": "选择rolling_window_compare。",
            },
            ("data_quality_profile", "compare_periods", "answer_verify"),
        )
        text = json.dumps(output, ensure_ascii=False)

        self.assertNotIn("metric_timeseries", text)
        self.assertNotIn("rolling_window_compare", text)
        self.assertIn("周期对比", text)

    def test_evidence_interpretation_normalizes_custom_baseline_direction(self):
        output = {
            "interpretation": "当前证据基于一次性基线比较（Q1 相比 Q2），中位数提升15%。",
            "decision_summary": "Q1相比Q2的结果成立。",
            "evidence_boundary": "不推断长期趋势。",
        }
        state = {
            "intent": {
                "pattern_family": "custom_baseline",
                "baseline": {"label": "Q1"},
                "target": {"label": "Q2"},
            }
        }

        normalized = _normalize_evidence_interpretation_output(output, state)

        text = " ".join(normalized.values())
        self.assertNotIn("Q1 相比 Q2", text)
        self.assertNotIn("Q1相比Q2", text)
        self.assertNotIn("中位数", text)
        self.assertIn("Q2 相比 Q1", text)
        self.assertIn("对比提升", text)

    def test_evidence_interpretation_businessizes_negative_pattern_result(self):
        output = {
            "interpretation": (
                "目标索赔缺乏统计支持，中位提升为-7.1%，"
                "全部月份均未超过重要性阈值（0.03）。"
            ),
            "decision_summary": "目标索赔缺乏统计支持，方向比指向反方向。",
            "evidence_boundary": "共90个数据点，结果受低于重要性阈值的限制。",
        }
        state = {
            "intent": {"pattern_family": "intra_period"},
            "evidence_brief": {"limitations": ["weak_direction", "below_materiality_floor"]},
            "evidence": [
                {
                    "capability_id": "compare_period_phases",
                    "typed_payload": {
                        "pattern_family": "intra_period",
                        "median_uplift": -0.071,
                        "direction_ratio": 0.1,
                        "comparable_periods": 30,
                    },
                }
            ],
        }

        normalized = _normalize_evidence_interpretation_output(output, state)
        text = " ".join(normalized.values())

        self.assertNotIn("目标索赔", text)
        self.assertNotIn("中位提升为-", text)
        self.assertNotIn("全部月份均未超过", text)
        self.assertNotIn("90个数据点", text)
        self.assertIn("目标声明", text)
        self.assertIn("中位变化为下降 7.1%", text)
        self.assertIn("正向月份也未达到当前重要性阈值", text)
        self.assertIn("90个阶段聚合点", text)

    def test_default_claim_preserves_daily_average_business_metric(self):
        state = {
            "intent": {
                "pattern_family": "custom_baseline",
                "target_metric": "paid_amount",
                "scope": "full_sample",
                "time_window": "2026-01-01..2026-06-30",
                "target_claim": "2026年Q2的日均付费金额高于2026年Q1的日均付费金额",
                "baseline": {"label": "Q1"},
                "target": {"label": "Q2"},
            },
            "evidence_brief": {"limitations": []},
            "evidence": [
                {
                    "evidence_ref": "compare_periods:run-1",
                    "capability_id": "compare_periods",
                    "strength": "high",
                    "wording_limit": "supported",
                    "typed_payload": {
                        "pattern_family": "custom_baseline",
                        "median_uplift": 0.1504,
                        "direction_ratio": 1.0,
                        "comparable_periods": 1,
                    },
                }
            ],
        }

        claim = _default_claim_from_evidence(state)

        self.assertIn("日均付费金额提升 15.0%", claim["text"])
        self.assertNotIn("中位数", claim["text"])
        self.assertNotIn("方向命中率", claim["text"])
        self.assertNotIn("可比周期", claim["text"])
        self.assertEqual(claim["numbers"]["direction_ratio"], 1.0)
        self.assertEqual(claim["numbers"]["comparable_periods"], 1)

    def test_final_summary_display_repair_rejects_audit_jargon_and_overstrong_wording(self):
        claim = {
            "text": "Q2 相比 Q1 在 2026-01-01..2026-06-30 观察到：日均付费金额提升 15.0%，方向命中率 100.0%，1 个可比周期。",
            "numbers": {
                "median_uplift": 0.1504,
                "direction_ratio": 1.0,
                "comparable_periods": 1,
            },
            "evidence_refs": ["compare_periods:run-1"],
            "scope": "full_sample",
            "time_window": "2026-01-01..2026-06-30",
        }
        state = {
            "intent": {
                "pattern_family": "custom_baseline",
                "target_metric": "paid_amount",
                "scope": "full_sample",
                "time_window": "2026-01-01..2026-06-30",
                "baseline": {"label": "Q1"},
                "target": {"label": "Q2"},
            },
            "draft_claims": [claim],
            "evidence": [
                {
                    "evidence_ref": "compare_periods:run-1",
                    "capability_id": "compare_periods",
                    "typed_payload": {
                        "pattern_family": "custom_baseline",
                        "median_uplift": 0.1504,
                        "direction_ratio": 1.0,
                        "comparable_periods": 1,
                    },
                }
            ],
        }
        summary = (
            "我对问题的理解是：你想看Q2相比Q1的日均付费金额。\n"
            "分析脉络：已完成期间对比。\n"
            "关键发现：日均付费金额提升 15.0%，方向命中率 100.0%，1 个可比周期。\n"
            "最终结论：结论经过语义审计和硬验证，证据充分。\n"
            "需要注意：不能外推为长期稳定规律。"
        )

        self.assertTrue(_final_summary_needs_display_repair(summary, state))

    def test_final_summary_display_repair_accepts_compact_percent_text(self):
        claim = {
            "text": "当前证据不支持月初高于月中和月末。",
            "numbers": {
                "median_uplift": -0.0712977193565374,
                "direction_ratio": 0.1,
                "comparable_periods": 30,
            },
            "evidence_refs": ["compare_period_phases:run-1"],
            "scope": "full_sample",
            "time_window": "2024-01-01..2026-06-30",
        }
        state = {
            "intent": {
                "pattern_family": "intra_period",
                "target_metric": "paid_amount",
                "scope": "full_sample",
                "time_window": "2024-01-01..2026-06-30",
            },
            "draft_claims": [claim],
            "evidence": [
                {
                    "evidence_ref": "compare_period_phases:run-1",
                    "capability_id": "compare_period_phases",
                    "typed_payload": {
                        "pattern_family": "intra_period",
                        "median_uplift": -0.0712977193565374,
                        "direction_ratio": 0.1,
                        "comparable_periods": 30,
                    },
                }
            ],
        }
        summary = (
            "我对问题的理解是：判断月初是否高于月中和月末。\n"
            "分析脉络：按月内阶段对比。\n"
            "关键发现：30个可比月份中，仅10%（3个月）支持该假设，中位变化为-7.1%。\n"
            "最终结论：当前统计证据不支持该假设。\n"
            "需要注意：结论不能外推为机制解释。"
        )

        self.assertFalse(_final_summary_needs_display_repair(summary, state))

    def test_final_summary_display_repair_rejects_degraded_boundary_drift(self):
        state = {
            "intent": {
                "pattern_family": "intra_period",
                "target_metric": "paid_amount",
                "scope": "full_sample",
                "time_window": "2026-01-01..2026-06-30",
            },
            "draft_claims": [],
            "evidence_brief": {
                "limitations": ["below_materiality_floor", "weak_direction"],
            },
            "final_explanation": {"status": "degraded"},
        }
        summary = (
            "我对问题的理解是：你想判断月初是否更高。\n"
            "分析脉络：由于数据量级较小，分析能力受到限制。\n"
            "关键发现：变化幅度低于重要性阈值。\n"
            "最终结论：当前证据不足以支持主张。\n"
            "需要注意：建议延长至全年或多年，或补充合同条款后再看。"
        )

        self.assertTrue(_final_summary_needs_display_repair(summary, state))

    def test_final_summary_display_repair_rejects_materiality_ratio_threshold_drift(self):
        state = {
            "intent": {
                "pattern_family": "intra_period",
                "target_metric": "paid_amount",
                "scope": "full_sample",
                "time_window": "2024-01-01..2026-06-30",
            },
            "draft_claims": [],
            "evidence": [
                {
                    "capability_id": "compare_period_phases",
                    "typed_payload": {
                        "pattern_family": "intra_period",
                        "median_uplift": -0.0713,
                        "direction_ratio": 0.2667,
                        "direction_consistency_ratio": 0.2667,
                        "materiality_hit_ratio": 0.1,
                        "comparable_periods": 30,
                        "min_periods": 30,
                        "materiality_floor": 0.03,
                    },
                    "limitations": ["below_materiality_floor", "weak_direction"],
                    "strength": "low",
                    "wording_limit": "insufficient",
                }
            ],
            "evidence_brief": {
                "limitations": ["below_materiality_floor", "weak_direction"],
            },
            "final_explanation": {"status": "degraded"},
        }
        summary = (
            "我对问题的理解是：你想判断月初是否高于月中和月末。\n"
            "分析脉络：我按月份对比月初和其他阶段。\n"
            "关键发现：中位下降 7.1%，方向一致比例 26.7%，达到重要性阈值的比例 10.0%。\n"
            "最终结论：达到重要性阈值的比例10%，低于3%阈值，当前证据不支持该假设。\n"
            "需要注意：方向一致性不足，变化幅度未达到当前重要性阈值。"
        )

        self.assertTrue(_final_summary_needs_display_repair(summary, state))

    def test_degraded_final_summary_fallback_uses_business_language(self):
        summary = _final_business_summary_fallback(
            {
                "intent": {
                    "pattern_family": "intra_period",
                    "target_metric": "paid_amount",
                    "scope": "full_sample",
                    "time_window": "2026-01-01..2026-06-30",
                },
                "request": {"question": "月边界窗口相比月中是否更高？"},
                "draft_claims": [],
                "final_explanation": {
                    "explanation": "变化幅度低于重要性阈值，方向不一致。",
                    "repair_path": "持续观察新周期并复核方向一致性。",
                },
            }
        )

        self.assertNotIn("系统", summary)
        self.assertNotIn("降级", summary)
        self.assertIn("当前证据不足以发布这个主结论", summary)

    def test_degraded_final_summary_fallback_includes_primary_evidence_numbers(self):
        state = {
            "intent": {
                "pattern_family": "intra_period",
                "target_metric": "paid_amount",
                "scope": "full_sample",
                "time_window": "2024-01-01..2026-06-30",
            },
            "request": {"question": "月末21号以后付费金额是否高于月中？"},
            "draft_claims": [],
            "evidence": [
                {
                    "capability_id": "compare_period_phases",
                    "typed_payload": {
                        "pattern_family": "intra_period",
                        "median_uplift": 0.039385402448221585,
                        "direction_ratio": 0.5666666666666667,
                        "comparable_periods": 30,
                        "min_periods": 30,
                        "materiality_floor": 0.03,
                    },
                    "limitations": ["weak_direction"],
                    "strength": "low",
                    "wording_limit": "tendency",
                }
            ],
            "evidence_brief": {"limitations": ["weak_direction"]},
            "final_explanation": {
                "explanation": "当前证据不足，不能发布主业务结论。",
                "repair_path": "继续观察新周期并复核方向一致性。",
            },
        }

        summary = _final_business_summary_fallback(state)

        self.assertIn("中位提升 3.9%", summary)
        self.assertIn("方向一致比例 56.7%", summary)
        self.assertIn("达到重要性阈值的比例 56.7%", summary)
        self.assertIn("30 个可比周期", summary)

    def test_degraded_final_summary_without_primary_numbers_needs_repair(self):
        state = {
            "intent": {
                "pattern_family": "custom_baseline",
                "target_metric": "paid_amount",
                "scope": "full_sample",
                "time_window": "2024-01-01..2026-06-30",
                "baseline": {"label": "其他渠道合计"},
                "target": {"label": "WajeSpecial渠道"},
            },
            "draft_claims": [],
            "evidence": [
                {
                    "capability_id": "rolling_window_compare",
                    "typed_payload": {
                        "pattern_family": "custom_baseline",
                        "median_uplift": 1.5728228306622776,
                        "direction_ratio": 0.9310344827586207,
                        "comparable_periods": 29,
                    },
                    "limitations": ["insufficient_comparable_periods"],
                }
            ],
            "evidence_brief": {"limitations": ["insufficient_comparable_periods"]},
            "final_explanation": {"status": "degraded"},
        }

        summary = (
            "我对问题的理解是：你想看 WajeSpecial渠道 相比 其他渠道合计 的日均付费金额是否有明显变化。\n"
            "分析脉络：我先确认问题边界、数据口径和可执行分析路径。\n"
            "关键发现：当前证据不足，不能发布主业务结论。\n"
            "最终结论：当前证据不足以发布这个主结论。\n"
            "需要注意：补充更多可比周期。"
        )

        self.assertTrue(_final_summary_needs_display_repair(summary, state))

    def test_final_summary_repair_requires_driver_claim_numbers(self):
        state = {
            "intent": {
                "pattern_family": "custom_baseline",
                "target_metric": "paid_amount",
                "scope": "all_users",
                "time_window": "2026-01-01..2026-06-30",
                "baseline": {"label": "2026年Q1"},
                "target": {"label": "2026年Q2"},
            },
            "request": {
                "question": "2026年Q2相比Q1付费金额提升，主要是付费用户数增加还是单付费用户金额提升带来的？"
            },
            "draft_claims": [
                {
                    "text": "2026年Q2相比2026年Q1，付费金额提升约16.3%",
                    "numbers": {"median_uplift": 0.1632579798864855},
                    "evidence_refs": ["compare_periods:run-1"],
                    "scope": "all_users",
                    "time_window": "2026-01-01..2026-06-30",
                },
                {
                    "text": "单付费用户金额是主要贡献项，贡献65.4%；付费用户数贡献34.6%。",
                    "numbers": {
                        "unit_value_share": 0.6537576498494277,
                        "volume_share": 0.3462423501505722,
                    },
                    "evidence_refs": ["driver_decomposition:inline"],
                    "scope": "all_users",
                    "time_window": "2026-01-01..2026-06-30",
                },
            ],
            "evidence": [
                {
                    "evidence_ref": "driver_decomposition:inline",
                    "capability_id": "driver_decomposition",
                    "typed_payload": {
                        "decompositions": [
                            {
                                "primary_driver": "unit_value",
                                "volume_key": "paid_users",
                                "unit_value_share": 0.6537576498494277,
                                "volume_share": 0.3462423501505722,
                                "amount_delta_ratio": 0.1632579798864855,
                            }
                        ]
                    },
                    "strength": "high",
                    "wording_limit": "quantified",
                }
            ],
        }
        incomplete = (
            "我对问题的理解是：你想看Q2相比Q1。\n"
            "分析脉络：我做了周期对比和驱动拆解。\n"
            "关键发现：付费金额提升 16.3%。\n"
            "最终结论：付费金额提升约16.3%。\n"
            "需要注意：不能外推为长期规律。"
        )

        self.assertTrue(_final_summary_needs_display_repair(incomplete, state))
        fallback = _final_business_summary_fallback(state)
        self.assertIn("65.4%", fallback)
        self.assertIn("34.6%", fallback)
        self.assertIn("单付费用户金额", fallback)

    def test_next_action_ask_degrades_when_evidence_has_terminal_business_boundary(self):
        state = {
            "request": {"allow_question_interrupt": True},
            "checkpoint_events": [{"node": "decide_next_action"}],
            "next_action": {
                "next_action": "ask_question",
                "decision_summary": "建议用户调整稳定性规则。",
            },
            "intent": {
                "pattern_family": "custom_baseline",
                "target_metric": "paid_amount",
                "scope": "full_sample",
                "time_window": "2024-01-01..2026-06-30",
            },
            "evidence_brief": {"limitations": ["insufficient_comparable_periods"]},
            "evidence": [
                {
                    "capability_id": "compare_periods",
                    "typed_payload": {
                        "pattern_family": "custom_baseline",
                        "comparable_periods": 29,
                    },
                    "limitations": ["insufficient_comparable_periods"],
                    "strength": "low",
                    "wording_limit": "insufficient",
                }
            ],
        }

        self.assertEqual(_route_after_next_action(state), "degrade")
        self.assertEqual(state["next_action"]["next_action"], "degrade")
        self.assertEqual(
            state["checkpoint_events"][-1]["route"],
            "ask_overridden_to_degrade",
        )

    def test_next_action_ask_degrades_when_post_evidence_gap_is_not_business_ambiguity(self):
        state = {
            "request": {"allow_question_interrupt": True},
            "checkpoint_events": [{"node": "decide_next_action"}],
            "next_action": {
                "next_action": "ask_question",
                "decision_summary": "建议用户补充更多业务维度。",
            },
            "intent": {
                "ambiguous_slots": [],
                "pattern_family": "rolling",
                "question_family": "revenue_health_review",
                "target_metric": "paid_amount",
                "scope": "all_users",
                "time_window": "2026-01-01..2026-06-30",
            },
            "evidence_brief": {
                "limitations": ["driver_components_missing"],
                "primary_capability": "driver_decomposition",
                "wording_limit": "insufficient",
            },
            "evidence": [
                {
                    "capability_id": "data_quality_profile",
                    "evidence_ref": "data_quality_profile:run",
                    "strength": "high",
                    "wording_limit": "supported",
                    "limitations": [],
                },
                {
                    "capability_id": "driver_decomposition",
                    "evidence_ref": "driver_decomposition:inline",
                    "strength": "low",
                    "wording_limit": "insufficient",
                    "limitations": ["driver_components_missing"],
                },
            ],
        }

        self.assertEqual(_route_after_next_action(state), "degrade")
        self.assertEqual(state["next_action"]["next_action"], "degrade")
        self.assertEqual(
            state["checkpoint_events"][-1]["route"],
            "ask_overridden_to_degrade",
        )

    def test_llm_narrative_replaces_non_string_narrative_values(self):
        output = _localize_narrative_fields(
            {
                "evidence_boundary": {
                    "weekday_calendar_compare": "medium",
                    "event_evidence": "low",
                }
            }
        )

        self.assertEqual(output["evidence_boundary"], "证据边界已记录。")

    def test_llm_narrative_localizes_audit_issue_descriptions(self):
        output = _localize_narrative_fields(
            {
                "issues": [
                    {
                        "description": (
                            "draft_claims and evidence_brief disagree with wording_limit "
                            "for paid_amount."
                        ),
                        "issue_description": (
                            "draft_claims and evidence_brief disagree with wording_limit "
                            "for paid_amount."
                        ),
                    }
                ]
            }
        )

        description = output["issues"][0]["description"]
        issue_description = output["issues"][0]["issue_description"]
        self.assertNotIn("draft_claims", description)
        self.assertNotIn("evidence_brief", description)
        self.assertNotIn("wording_limit", description)
        self.assertNotIn("paid_amount", description)
        self.assertNotIn("draft_claims", issue_description)
        self.assertNotIn("evidence_brief", issue_description)
        self.assertNotIn("wording_limit", issue_description)
        self.assertNotIn("paid_amount", issue_description)
        self.assertIn("答案声明", description)
        self.assertIn("证据摘要", description)

    def test_successful_workflow_calls_required_llm_tasks(self):
        fake = FakeLLMClient()
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {"artifact_root": tmpdir, "run_id": "llm-flow", "llm_client": fake}
            )

        self.assertEqual(result.status, "draft")
        for task in (
            "business_intent",
            "boundary_decision",
            "confirm_understanding",
            "analysis_route",
            "data_coverage_interpretation",
            "next_action",
            "evidence_interpretation",
            "answer_synthesis",
            "semantic_audit",
        ):
            self.assertIn(task, fake.calls)
        self.assertEqual(
            [call["task"] for call in result.answer_package["admin_audit"]["llm_calls"]],
            fake.calls,
        )
        for call in result.answer_package["admin_audit"]["llm_calls"]:
            self.assertTrue(call["messages"])
            self.assertIn("required_keys", call)
            self.assertIn("raw_response_content", call)
            self.assertIn("started_at", call)
            self.assertIn("finished_at", call)
            self.assertGreaterEqual(call["duration_ms"], 0)
        for event in result.answer_package["checkpoint_events"]:
            self.assertIn("started_at", event)
            self.assertIn("finished_at", event)
            self.assertGreaterEqual(event["duration_ms"], 0)

    def test_workflow_invokes_causal_audit_before_answer_synthesis(self):
        fake = FakeLLMClient()
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {"artifact_root": tmpdir, "run_id": "causal-audit-order", "llm_client": fake}
            )

        self.assertEqual(result.status, "draft")
        self.assertIn("causal_audit", fake.calls)
        self.assertLess(fake.calls.index("causal_audit"), fake.calls.index("answer_synthesis"))

    def test_answer_synthesis_receives_causal_audit_context(self):
        fake = FakeLLMClient()
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {"artifact_root": tmpdir, "run_id": "causal-audit-context", "llm_client": fake}
            )

        payload = _llm_input_payload(result.answer_package, "answer_synthesis")

        self.assertIn("causal_evidence_dossier", payload["answer_context"])
        self.assertIn("causal_audit", payload["answer_context"])
        self.assertEqual(
            payload["answer_context"]["causal_evidence_dossier"]["observed_pattern"][
                "direction"
            ],
            "target_higher",
        )
        self.assertEqual(
            payload["answer_context"]["causal_audit"]["causal_assessment"],
            "candidate_hypothesis",
        )

    def test_workflow_persists_causal_audit_in_answer_package(self):
        fake = FakeLLMClient()
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {"artifact_root": tmpdir, "run_id": "causal-audit-artifact", "llm_client": fake}
            )

        admin = result.answer_package["admin_audit"]

        self.assertEqual(admin["causal_audit"]["causal_assessment"], "candidate_hypothesis")
        self.assertIn("observed_pattern", admin["causal_evidence_dossier"])

    def test_analysis_route_prompt_includes_capability_cards_and_budget(self):
        fake = FakeLLMClient()
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {"artifact_root": tmpdir, "run_id": "route-cards", "llm_client": fake}
            )

        calls = result.answer_package["admin_audit"]["llm_calls"]
        route_call = next(call for call in calls if call["task"] == "analysis_route")
        route_messages = "\n".join(message["content"] for message in route_call["messages"])

        self.assertIn("compare_periods", route_messages)
        self.assertIn("budget_state", route_messages)
        self.assertIn("do_not_trade_answer_quality_for_cost_during_research", route_messages)

    def test_business_intent_prompt_does_not_prebind_question_family(self):
        fake = FakeLLMClient()
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "intent-autonomy",
                    "llm_client": fake,
                    "question": "2026年Q2相比Q1，付费金额有没有明显变化？",
                    "question_family": "pattern_explanation",
                    "pattern_family": "custom_baseline",
                    "scope": "full_sample",
                    "time_window": "2026-01-01..2026-06-30",
                }
            )

        payload = _llm_input_payload(result.answer_package, "business_intent")

        self.assertNotIn("question_family_hint", payload)
        self.assertNotIn("question_family", payload.get("bound_business_context", {}))

    def test_business_intent_uses_llm_question_family_before_request_fallback(self):
        fake = FakeLLMClient(
            {"business_intent": {"question_family": "custom_baseline_comparison"}}
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "intent-request-does-not-override",
                    "llm_client": fake,
                    "question": "2026年Q2相比Q1，付费金额有没有明显变化？",
                    "question_family": "pattern_explanation",
                    "pattern_family": "custom_baseline",
                    "scope": "full_sample",
                    "time_window": "2026-01-01..2026-06-30",
                }
            )

        payload = _llm_input_payload(result.answer_package, "confirm_understanding")

        self.assertEqual(payload["intent"]["question_family"], "custom_baseline_comparison")

    def test_workflow_executes_harness_capability_when_accepted(self):
        fake = FakeLLMClient(
            {"analysis_route": {"requested_nodes": ["compare_period_phases"]}}
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {"artifact_root": tmpdir, "run_id": "harness-route", "llm_client": fake}
            )

        evidence = result.answer_package["sections"][1]["payload"]["evidence"]

        self.assertTrue(
            any(item.get("capability_id") == "compare_period_phases" for item in evidence)
        )

    def test_execute_capabilities_runs_public_custom_baseline_graph(self):
        compiled = compile_graph(
            question_family="custom_baseline_comparison",
            target_metric="paid_amount",
            pattern_family="custom_baseline",
            requested_nodes=(
                "data_quality_profile",
                "compare_periods",
                "evidence_reduce",
                "answer_verify",
            ),
        )
        state = {
            "request": {
                "rows": [
                    {"period": "h1_2026", "group": "baseline", "amount": 100},
                    {"period": "h1_2026", "group": "target", "amount": 120},
                ],
                "required_fields": ("period", "group", "amount"),
                "role": "analyst",
            },
            "run_id": "execute-public-custom-baseline",
            "sql_hash": "sqlhash-custom",
            "budget_state": default_budget("ordinary"),
            "compiled_graph": compiled,
            "intent": {
                "question_family": "custom_baseline_comparison",
                "target_metric": "paid_amount",
                "pattern_family": "custom_baseline",
                "pattern_params": {
                    "period_key": "period",
                    "group_key": "group",
                    "target_group": "target",
                    "baseline_group": "baseline",
                    "min_periods": 1,
                },
                "scope": "full_sample",
                "time_window": "2026-01-01..2026-06-30",
                "target_claim": "Q2 相比 Q1 的付费金额变化",
                "baseline": {"label": "Q1"},
                "target": {"label": "Q2"},
            },
        }

        result = _execute_capabilities(state)

        evidence = result["evidence"]
        self.assertTrue(
            any(item.get("capability_id") == "compare_periods" for item in evidence)
        )
        self.assertTrue(
            any(item.get("capability_id") == "data_quality_profile" for item in evidence)
        )

    def test_reduce_evidence_uses_public_compare_as_primary_evidence(self):
        state = {
            "request": {},
            "checkpoint_events": [],
            "intent": {
                "question_family": "custom_baseline_comparison",
                "target_metric": "paid_amount",
                "pattern_family": "custom_baseline",
                "scope": "full_sample",
                "time_window": "2026-01-01..2026-06-30",
                "baseline": {"label": "Q1"},
                "target": {"label": "Q2"},
            },
            "evidence": [
                {
                    "evidence_ref": "compare_periods:run-1",
                    "capability_id": "compare_periods",
                    "strength": "high",
                    "wording_limit": "supported",
                    "limitations": [],
                    "typed_payload": {
                        "pattern_family": "custom_baseline",
                        "median_uplift": 0.2,
                        "direction_ratio": 1.0,
                        "comparable_periods": 1,
                        "min_periods": 1,
                        "materiality_floor": 0.03,
                    },
                }
            ],
        }

        _reduce_evidence(state)
        claim = _default_claim_from_evidence(state)

        self.assertEqual(state["evidence_brief"]["pattern_ref"], "compare_periods:run-1")
        self.assertEqual(state["evidence_brief"]["pattern_status"], "high")
        self.assertTrue(state["evidence_brief"]["pattern_established"])
        self.assertEqual(claim["evidence_refs"], ["compare_periods:run-1"])

    def test_analysis_route_does_not_prebind_requested_nodes(self):
        fake = FakeLLMClient({"analysis_route": {"requested_nodes": []}})
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "route-autonomy",
                    "llm_client": fake,
                    "requested_nodes": ["joint_attribution"],
                }
            )

        payload = _llm_input_payload(result.answer_package, "analysis_route")

        self.assertNotIn("requested_nodes_hint", payload)
        self.assertNotIn("joint_attribution", result.answer_package["accepted_graph"])

    def test_factor_attribution_route_runs_driver_decomposition(self):
        fake = FakeLLMClient(
            {
                "business_intent": {
                    "question_family": "segment_or_factor_attribution",
                    "pattern_family": "custom_baseline",
                    "target_metric": "paid_amount",
                    "scope": "full_sample",
                    "time_window": "2026-01-01..2026-06-30",
                    "target_claim": "判断增长来自用户数还是客单价",
                },
                "analysis_route": {
                    "requested_nodes": ["joint_attribution", "answer_verify"],
                },
            }
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "driver-decomposition-route",
                    "llm_client": fake,
                    "question": "Q2比Q1是用户数还是客单价驱动？",
                    "pattern_family": "custom_baseline",
                    "pattern_params": {
                        "period_key": "period",
                        "group_key": "group",
                        "target_group": "target",
                        "baseline_group": "baseline",
                    },
                    "rows": [
                        {
                            "period": "h1",
                            "group": "baseline",
                            "amount": 100,
                            "paid_users": 10,
                        },
                        {
                            "period": "h1",
                            "group": "target",
                            "amount": 150,
                            "paid_users": 12,
                        },
                    ],
                    "required_fields": ["period", "group", "amount", "paid_users"],
                    "time_window": "2026-01-01..2026-06-30",
                }
            )

        evidence = result.answer_package["sections"][1]["payload"]["evidence"]

        self.assertEqual(result.status, "draft")
        self.assertIn("driver_decomposition", result.answer_package["accepted_graph"])
        self.assertTrue(
            any(item.get("capability") == "driver_decomposition" for item in evidence)
        )
        self.assertIn(
            "unit_value_share",
            result.answer_package["sections"][0]["payload"]["claims"][0]["numbers"],
        )

    def test_joint_attribution_promotion_node_uses_rows_and_joint_dimensions(self):
        state = {
            "request": {},
            "sql_hash": "sql:test",
            "checkpoint_events": [{}],
            "intent": {
                "scope": "all_users",
                "time_window": "2026-01-01..2026-06-30",
                "pattern_params": {
                    "joint_dimension_keys": ("channel", "phase"),
                    "group_key": "group",
                    "target_group": "target",
                    "baseline_group": "baseline",
                },
            },
            "rows": [
                {"channel": "WajeSpecial", "phase": "start", "group": "baseline", "amount": 100, "n": 40},
                {"channel": "WajeSpecial", "phase": "start", "group": "target", "amount": 180, "n": 45},
                {"channel": "WajeSpecial", "phase": "mid", "group": "baseline", "amount": 100, "n": 40},
                {"channel": "WajeSpecial", "phase": "mid", "group": "target", "amount": 30, "n": 45},
            ],
            "evidence": [
                {
                    "capability": "segment_bridge",
                    "typed_payload": {"residual": 0.25, "fit": 0.60},
                }
            ],
        }

        _execute_joint_attribution(state)

        joint = state["evidence"][-1]
        self.assertEqual(joint["capability"], "joint_attribution")
        self.assertEqual(joint["typed_payload"]["dimension_keys"], ["channel", "phase"])
        self.assertEqual(
            joint["typed_payload"]["top_combinations"][0]["dimension_values"],
            ["WajeSpecial", "start"],
        )

    def test_segment_contribution_evidence_has_claim_ready_fields(self):
        compiled = compile_graph(
            question_family="segment_or_factor_attribution",
            target_metric="paid_amount",
            pattern_family="custom_baseline",
            requested_nodes=("segment_contribution", "answer_verify"),
        )
        state = {
            "request": {
                "rows": [
                    {"period": "WajeSpecial", "group": "baseline", "amount": 100},
                    {"period": "WajeSpecial", "group": "target", "amount": 160},
                    {"period": "Organic", "group": "baseline", "amount": 100},
                    {"period": "Organic", "group": "target", "amount": 90},
                ],
                "required_fields": ("period", "group", "amount"),
                "role": "analyst",
            },
            "run_id": "segment-evidence",
            "sql_hash": "sqlhash-segment",
            "budget_state": default_budget("ordinary"),
            "compiled_graph": compiled,
            "intent": {
                "question_family": "segment_or_factor_attribution",
                "target_metric": "paid_amount",
                "pattern_family": "custom_baseline",
                "pattern_params": {
                    "period_key": "period",
                    "group_key": "group",
                    "target_group": "target",
                    "baseline_group": "baseline",
                },
                "scope": "full_sample",
                "time_window": "2026-01-01..2026-06-30",
                "target_claim": "渠道贡献",
                "baseline": {"label": "Q1"},
                "target": {"label": "Q2"},
            },
        }

        result = _execute_capabilities(state)
        segment = next(
            item for item in result["evidence"] if item["capability"] == "segment_contribution"
        )

        self.assertEqual(segment["capability_id"], "segment_contribution")
        self.assertIn("typed_payload", segment)
        self.assertIn("numeric_facts", segment)
        self.assertIn(
            segment["wording_limit"],
            {"contextual", "supported", "tendency", "insufficient"},
        )

    def test_route_normalization_adds_driver_decomposition_for_explicit_volume_vs_unit_value_question(self):
        nodes = _normalize_route_requested_nodes(
            ("data_quality_profile", "compare_periods", "answer_verify"),
            {
                "question_family": "custom_baseline_comparison",
                "pattern_family": "custom_baseline",
                "target_claim": "Q2提升主要是付费用户数增加还是单付费用户金额提升带来的",
                "target_metric": "paid_amount",
            },
        )

        self.assertIn("driver_decomposition", nodes)

    def test_route_normalization_removes_segment_contribution_without_segment_dimension(self):
        nodes = _normalize_route_requested_nodes(
            ("driver_decomposition", "segment_contribution", "answer_verify"),
            {
                "question_family": "segment_or_factor_attribution",
                "pattern_family": "custom_baseline",
                "target_claim": "Q2提升主要是付费用户数贡献还是单付费用户金额贡献",
                "target_metric": "paid_amount",
            },
        )

        self.assertIn("driver_decomposition", nodes)
        self.assertNotIn("segment_contribution", nodes)

    def test_route_normalization_adds_segment_contribution_from_original_question(self):
        nodes = _normalize_route_requested_nodes(
            ("data_quality_profile", "driver_decomposition", "answer_verify"),
            {
                "question": "Q2付费金额提升主要是哪些渠道贡献的？",
                "question_family": "segment_or_factor_attribution",
                "primary_question_family": "segment_or_factor_attribution",
                "secondary_question_families": ["custom_baseline_comparison"],
                "pattern_family": "custom_baseline",
                "target_claim": "pattern_explanation",
                "target_metric": "paid_amount",
            },
        )

        self.assertIn("segment_contribution", nodes)

    def test_route_normalization_adds_segment_contribution_when_family_drifts_to_baseline(self):
        nodes = _normalize_route_requested_nodes(
            ("data_quality_profile", "compare_periods", "driver_decomposition", "answer_verify"),
            {
                "question": "2026年Q2相比Q1，哪些渠道解释了付费金额变化？",
                "question_family": "custom_baseline_comparison",
                "pattern_family": "custom_baseline",
                "target_claim": "识别各渠道对付费金额变化的解释程度",
                "target_metric": "paid_amount",
            },
        )

        self.assertIn("segment_contribution", nodes)

    def test_requested_segment_node_infers_secondary_question_family(self):
        intent = {
            "question_family": "custom_baseline_comparison",
            "primary_question_family": "custom_baseline_comparison",
            "question_families": ["custom_baseline_comparison"],
            "secondary_question_families": [],
        }

        _infer_question_families_from_requested_nodes(
            intent,
            ("data_quality_profile", "segment_contribution", "answer_verify"),
        )

        self.assertIn("segment_or_factor_attribution", intent["question_families"])
        self.assertIn("segment_or_factor_attribution", intent["secondary_question_families"])

    def test_analysis_route_accepts_llm_node_objects_but_filters_internal_reducer(self):
        fake = FakeLLMClient(
            {
                "analysis_route": {
                    "requested_nodes": [
                        {"capability_id": "rolling_window_compare"},
                        {"capability_id": "metric_timeseries"},
                        {"capability": "answer_verify"},
                        {"node_id": "data_quality_profile"},
                        {"node": "evidence_reduce"},
                        {"id": "compare_period_phases"},
                    ]
                }
            }
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {"artifact_root": tmpdir, "run_id": "route-node-objects", "llm_client": fake}
            )

        self.assertEqual(result.status, "draft")
        payload = _llm_input_payload(result.answer_package, "analysis_route")

        self.assertNotIn("evidence_reduce", repr(payload["known_capabilities"]))
        self.assertIn("rolling_window_compare", result.answer_package["accepted_graph"])
        self.assertIn("data_quality_profile", result.answer_package["accepted_graph"])
        self.assertNotIn("evidence_reduce", result.answer_package["accepted_graph"])
        self.assertNotIn("metric_timeseries", result.answer_package["accepted_graph"])
        self.assertIn("compare_period_phases", result.answer_package["accepted_graph"])

    def test_custom_baseline_pattern_route_uses_period_compare_not_rolling(self):
        fake = FakeLLMClient(
            {
                "business_intent": {
                    "question_family": "pattern_explanation",
                    "pattern_family": "custom_baseline",
                    "target_metric": "paid_amount",
                    "scope": "full_sample",
                    "time_window": "2024-01-01..2026-06-30",
                    "target_claim": "WajeSpecial渠道稳定高于其他渠道合计",
                    "baseline_candidates": [],
                    "status_message": "intent",
                },
                "analysis_route": {
                    "requested_nodes": [
                        "data_quality_profile",
                        "metric_timeseries",
                        "rolling_window_compare",
                        "answer_verify",
                    ]
                },
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "custom-baseline-route-normalized",
                    "llm_client": fake,
                    "pattern_family": "custom_baseline",
                    "time_window": "2024-01-01..2026-06-30",
                    "rows": [
                        {"period": "2024-01", "group": "baseline", "amount": 100},
                        {"period": "2024-01", "group": "target", "amount": 120},
                    ],
                    "pattern_params": {
                        "period_key": "period",
                        "group_key": "group",
                        "target_group": "target",
                        "baseline_group": "baseline",
                        "min_periods": 1,
                    },
                }
            )

        self.assertIn("compare_periods", result.answer_package["accepted_graph"])
        self.assertNotIn("rolling_window_compare", result.answer_package["accepted_graph"])
        self.assertNotIn("metric_timeseries", result.answer_package["accepted_graph"])

    def test_boundary_question_without_user_choice_blocks_without_conclusion(self):
        fake = FakeLLMClient(
            {
                "boundary_decision": {
                    "boundary_status": "needs_question",
                    "recommended_assumption": {"scope": "full_sample"},
                    "clarification_questions": [
                        {
                            "question": "Which scope should be used?",
                            "options": ["full sample", "custom scope"],
                        }
                    ],
                    "decision_summary": "Scope could change the answer.",
                }
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {"artifact_root": tmpdir, "run_id": "needs-question", "llm_client": fake}
            )

        self.assertEqual(result.status, "draft")
        self.assertIn("clarification_question", fake.calls)
        self.assertIn("blocked_explanation", fake.calls)
        summary = result.answer_package["sections"][0]["payload"]
        self.assertFalse(summary["claims"])
        self.assertEqual(summary["final_explanation"]["status"], "blocked")

    def test_degrade_suggestion_does_not_drop_established_pattern_answer(self):
        fake = FakeLLMClient({"next_action": {"next_action": "degrade"}})

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {"artifact_root": tmpdir, "run_id": "degrade-override", "llm_client": fake}
            )

        self.assertEqual(result.status, "draft")
        self.assertIn("evidence_interpretation", fake.calls)
        self.assertIn("answer_synthesis", fake.calls)
        self.assertNotIn("degraded_explanation", fake.calls)
        routes = [
            event.get("route")
            for event in result.answer_package["checkpoint_events"]
            if event.get("node") == "decide_next_action"
        ]
        self.assertIn("degrade_overridden_to_bounded_answer", routes)

    def test_unsupported_pattern_synthesizes_negative_answer(self):
        rows = []
        for month in range(1, 7):
            rows.extend(
                [
                    {"month": f"2026-{month:02d}", "phase": "start", "amount": 100},
                    {"month": f"2026-{month:02d}", "phase": "mid", "amount": 120},
                    {"month": f"2026-{month:02d}", "phase": "end", "amount": 120},
                ]
            )
        fake = FakeLLMClient({"next_action": {"next_action": "degrade"}})

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "unsupported-pattern",
                    "llm_client": fake,
                    "rows": rows,
                    "time_window": "2026-01..2026-06",
                    "pattern_params": {"target_phase": "start", "min_periods": 6},
                }
            )

        payload = result.answer_package["sections"][0]["payload"]
        answer_text = payload["answer_text"]
        self.assertIn("answer_synthesis", fake.calls)
        self.assertNotIn("degraded_explanation", fake.calls)
        self.assertIn("不支持", answer_text)
        self.assertIn("不支持", payload["claims"][0]["text"])
        self.assertNotIn("中位提升 -", answer_text)
        routes = [
            event.get("route")
            for event in result.answer_package["checkpoint_events"]
            if event.get("node") == "decide_next_action"
        ]
        self.assertIn("degrade_overridden_to_negative_answer", routes)

    def test_degraded_explanation_hides_internal_tokens_from_business_output(self):
        rows = [{"month": "2026-01", "phase": "start", "amount": 100}]
        fake = FakeLLMClient(
            {
                "next_action": {"next_action": "synthesize_answer"},
                "degraded_explanation": {
                    "status": "degraded",
                    "explanation": (
                        "pattern_status: low; pattern_established=false; "
                        "wording_limit: insufficient."
                    ),
                    "owner": "pattern_scan:intra_period",
                    "repair_path": "Inspect pattern_scan evidence_ref.",
                },
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "degraded-business-copy",
                    "llm_client": fake,
                    "rows": rows,
                    "time_window": "2026-01..2026-06",
                    "pattern_params": {"target_phase": "start", "min_periods": 6},
                }
            )

        final_explanation = result.answer_package["sections"][0]["payload"][
            "final_explanation"
        ]
        visible_text = " ".join(
            str(final_explanation.get(key, ""))
            for key in ("explanation", "owner", "repair_path")
        )
        self.assertEqual(final_explanation["status"], "degraded")
        self.assertNotIn("pattern_status", visible_text)
        self.assertNotIn("pattern_established", visible_text)
        self.assertNotIn("wording_limit", visible_text)
        self.assertNotIn("pattern_scan", visible_text)
        self.assertNotIn("evidence_ref", visible_text)

    def test_degraded_explanation_rejects_data_volume_materiality_drift(self):
        state = {
            "evidence_brief": {
                "limitations": ["below_materiality_floor", "weak_direction"]
            },
            "validator_results": [],
        }
        output = {
            "status": "degraded",
            "explanation": "数据量低于重要性门槛，无法解释。",
            "owner": "数据治理团队",
            "repair_path": "补充更多历史数据。",
        }

        final_explanation = _sanitize_terminal_explanation(output, state, "degraded")

        visible_text = " ".join(
            str(final_explanation.get(key, ""))
            for key in ("explanation", "owner", "repair_path")
        )
        self.assertNotIn("数据量低于", visible_text)
        self.assertIn("变化幅度低于当前重要性阈值", visible_text)

    def test_degraded_explanation_reassigns_quality_owner_when_limits_are_business(self):
        state = {
            "evidence_brief": {
                "limitations": ["below_materiality_floor", "weak_direction"]
            },
            "validator_results": [],
        }
        output = {
            "status": "degraded",
            "explanation": "变化幅度低于重要性阈值，方向一致性不足。",
            "owner": "数据质量团队",
            "repair_path": "继续观察新周期。",
        }

        final_explanation = _sanitize_terminal_explanation(output, state, "degraded")

        self.assertEqual(final_explanation["owner"], "业务分析负责人")

    def test_degraded_explanation_rejects_data_source_repair_for_business_limits(self):
        state = {
            "evidence_brief": {
                "limitations": ["below_materiality_floor", "weak_direction"]
            },
            "validator_results": [],
        }
        output = {
            "status": "degraded",
            "explanation": "变化幅度低于重要性阈值，方向一致性不足。",
            "owner": "数据工程团队",
            "repair_path": "检查数据源完整性和提取逻辑。",
        }

        final_explanation = _sanitize_terminal_explanation(output, state, "degraded")

        visible_text = " ".join(
            str(final_explanation.get(key, ""))
            for key in ("explanation", "owner", "repair_path")
        )
        self.assertNotIn("数据源完整性", visible_text)
        self.assertEqual(final_explanation["owner"], "业务分析负责人")

    def test_degraded_explanation_rejects_data_quality_drift_for_business_limits(self):
        state = {
            "evidence_brief": {
                "limitations": ["below_materiality_floor", "weak_direction"]
            },
            "validator_results": [],
        }
        output = {
            "status": "degraded",
            "explanation": "现有数据质量不足以支持该模式判定。",
            "owner": "业务分析师",
            "repair_path": "延长观察周期。",
        }

        final_explanation = _sanitize_terminal_explanation(output, state, "degraded")

        self.assertIn("变化幅度低于当前重要性阈值", final_explanation["explanation"])

    def test_noninteractive_coverage_question_continues_when_validators_pass(self):
        fake = FakeLLMClient(
            {
                "data_coverage_interpretation": {
                    "coverage_status": "needs_question",
                    "business_impact": "Model wants confirmation.",
                    "decision_summary": "Ask in interactive mode.",
                }
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "coverage-noninteractive",
                    "llm_client": fake,
                    "allow_question_interrupt": False,
                }
            )

        self.assertEqual(result.status, "draft")
        self.assertIn("answer_synthesis", fake.calls)
        self.assertNotIn("blocked_explanation", fake.calls)

    def test_answerable_custom_baseline_overrides_coverage_question(self):
        fake = FakeLLMClient(
            {
                "data_coverage_interpretation": {
                    "coverage_status": "needs_question",
                    "business_impact": "需要补充每日明细才能确认日均。",
                    "decision_summary": "询问是否允许用固定天数。",
                }
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "coverage-question-answerable",
                    "llm_client": fake,
                    "rows": [
                        {"period": "h1", "group": "baseline", "amount": 100},
                        {"period": "h1", "group": "target", "amount": 120},
                    ],
                    "required_fields": ("period", "group", "amount"),
                    "pattern_family": "custom_baseline",
                    "pattern_params": {
                        "period_key": "period",
                        "group_key": "group",
                        "target_group": "target",
                        "baseline_group": "baseline",
                        "min_periods": 1,
                    },
                    "baseline": {"label": "Q1"},
                    "target": {"label": "Q2"},
                }
            )

        self.assertEqual(result.status, "draft")
        self.assertIn("answer_synthesis", fake.calls)
        self.assertNotIn("blocked_explanation", fake.calls)
        coverage = result.answer_package["admin_audit"]["coverage_interpretation"]
        self.assertEqual(coverage["coverage_status"], "coverage_gap_but_answerable")
        self.assertEqual(coverage["local_override"], "needs_question_without_local_gap")

    def test_answerable_custom_baseline_cleans_answerable_coverage_confirmation_text(self):
        fake = FakeLLMClient(
            {
                "data_coverage_interpretation": {
                    "coverage_status": "coverage_gap_but_answerable",
                    "business_impact": "缺少日明细，无法直接计算，需要用户确认。",
                    "decision_summary": "建议确认是否接受当前聚合结果。",
                }
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "coverage-gap-confirmation-cleaned",
                    "llm_client": fake,
                    "rows": [
                        {"period": "h1", "group": "baseline", "amount": 100},
                        {"period": "h1", "group": "target", "amount": 120},
                    ],
                    "required_fields": ("period", "group", "amount"),
                    "pattern_family": "custom_baseline",
                    "pattern_params": {
                        "period_key": "period",
                        "group_key": "group",
                        "target_group": "target",
                        "baseline_group": "baseline",
                        "min_periods": 1,
                    },
                    "baseline": {"label": "Q1"},
                    "target": {"label": "Q2"},
                }
            )

        coverage = result.answer_package["admin_audit"]["coverage_interpretation"]
        visible = coverage["business_impact"] + coverage["decision_summary"]
        self.assertEqual(coverage["coverage_status"], "coverage_gap_but_answerable")
        self.assertNotIn("确认", visible)
        self.assertNotIn("无法直接", visible)

    def test_coverage_block_without_local_data_failure_continues_as_warning(self):
        fake = FakeLLMClient(
            {
                "data_coverage_interpretation": {
                    "coverage_status": "blocked",
                    "business_impact": "模型判断需要阻断。",
                    "decision_summary": "缺少具体本地缺口。",
                }
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "coverage-block-overridden",
                    "llm_client": fake,
                    "rows": [
                        {"period": "h1", "group": "baseline", "amount": 100},
                        {"period": "h1", "group": "target", "amount": 120},
                    ],
                    "required_fields": ("period", "group", "amount"),
                    "pattern_family": "custom_baseline",
                    "pattern_params": {
                        "period_key": "period",
                        "group_key": "group",
                        "target_group": "target",
                        "baseline_group": "baseline",
                        "min_periods": 1,
                    },
                    "baseline": {"label": "Q1"},
                    "target": {"label": "Q2"},
                }
            )

        self.assertEqual(result.status, "draft")
        self.assertIn("answer_synthesis", fake.calls)
        self.assertNotIn("blocked_explanation", fake.calls)
        coverage = result.answer_package["admin_audit"]["coverage_interpretation"]
        self.assertEqual(
            coverage["coverage_status"],
            "sufficient",
        )
        self.assertEqual(
            coverage["local_override"],
            "blocked_without_local_evidence",
        )

    def test_answerable_coverage_gap_continues_to_evidence_execution(self):
        fake = FakeLLMClient(
            {
                "data_coverage_interpretation": {
                    "coverage_status": "coverage_gap_but_answerable",
                    "business_impact": "起始窗口有轻微偏移，但仍可回答趋势问题。",
                    "decision_summary": "继续执行证据路径，并在结论边界中保留覆盖说明。",
                }
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "coverage-gap-answerable",
                    "llm_client": fake,
                    "rows": [
                        {"window": "2026-02", "amount": 120, "baseline_high": 100},
                        {"window": "2026-03", "amount": 140, "baseline_high": 120},
                    ],
                    "pattern_family": "rolling",
                    "pattern_params": {"period_key": "window", "min_periods": 2},
                }
            )

        self.assertEqual(result.status, "draft")
        self.assertIn("answer_synthesis", fake.calls)
        self.assertNotIn("degraded_explanation", fake.calls)

    def test_data_coverage_input_includes_aggregate_result_summary(self):
        fake = FakeLLMClient()
        rows = [
            {"week": "2026-01-05", "weekday": 1, "amount": 100},
            {"week": "2026-01-05", "weekday": 4, "amount": 120},
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "coverage-result-summary",
                    "llm_client": fake,
                    "rows": rows,
                    "pattern_family": "weekly",
                    "pattern_params": {
                        "week_key": "week",
                        "weekday_key": "weekday",
                        "target_weekdays": [4],
                        "baseline_weekdays": [1],
                    },
                }
            )

        payload = _llm_input_payload(result.answer_package, "data_coverage_interpretation")
        summary = payload["data_result_summary"]

        self.assertEqual(summary["row_count"], 2)
        self.assertEqual(summary["fields"], ["week", "weekday", "amount"])
        self.assertEqual(summary["field_values"]["week"], ["2026-01-05"])
        self.assertEqual(summary["field_values"]["weekday"], ["1", "4"])

    def test_repeated_evidence_expansion_is_capped_by_trace(self):
        fake = FakeLLMClient({"next_action": {"next_action": "scan_sibling"}})

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {"artifact_root": tmpdir, "run_id": "loop-capped", "llm_client": fake}
            )

        self.assertEqual(result.status, "draft")
        plan_routes = [
            event.get("route")
            for event in result.answer_package["checkpoint_events"]
            if event.get("node") == "decide_next_action"
        ]
        self.assertEqual(plan_routes.count("plan"), 1)
        self.assertIn("synthesize_after_loop_cap", plan_routes)

    def test_llm_claim_time_window_is_normalized_to_evidence_window(self):
        fake = FakeLLMClient(
            {
                "answer_synthesis": {
                    "answer_text": "Draft answer with exception period.",
                    "claims": [
                        {
                            "text": "The main pattern is supported.",
                            "evidence_refs": ["pattern_scan:intra_period"],
                            "numbers": {"median_uplift": 0.2},
                            "scope": "full_sample",
                            "time_window": "2026-05",
                        }
                    ],
                }
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "claim-window-normalized",
                    "llm_client": fake,
                }
            )

        claims = result.answer_package["sections"][0]["payload"]["claims"]
        self.assertEqual(claims[0]["time_window"], "2024-01..2026-05")
        self.assertEqual(result.answer_package["admin_audit"]["verifier"]["errors"], [])

    def test_duplicate_llm_claims_are_deduped(self):
        duplicate = {
            "text": "The same pattern claim.",
            "evidence_refs": ["pattern_scan:intra_period"],
            "numbers": {"median_uplift": 0.2},
            "scope": "full_sample",
            "time_window": "2024-01..2026-05",
        }
        fake = FakeLLMClient(
            {"answer_synthesis": {"answer_text": "Draft.", "claims": [duplicate, duplicate]}}
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {"artifact_root": tmpdir, "run_id": "dedupe-claims", "llm_client": fake}
            )

        claims = result.answer_package["sections"][0]["payload"]["claims"]
        self.assertEqual(len(claims), 1)

    def test_llm_side_claims_do_not_enter_verified_claim_list(self):
        fake = FakeLLMClient(
            {
                "answer_synthesis": {
                    "answer_text": "Pattern answer with side diagnostics.",
                    "claims": [
                        {
                            "text": "Data quality is high.",
                            "evidence_refs": ["data_quality_check:inline"],
                            "numbers": {"row_count": 100},
                            "scope": "full_sample",
                            "time_window": "2024-01..2026-05",
                        },
                        {
                            "text": "No outliers were detected.",
                            "evidence_refs": ["outlier_scan:inline"],
                            "numbers": {"outliers": []},
                            "scope": "full_sample",
                            "time_window": "2024-01..2026-05",
                        },
                    ],
                }
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {"artifact_root": tmpdir, "run_id": "side-claims", "llm_client": fake}
            )

        claims = result.answer_package["sections"][0]["payload"]["claims"]
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["evidence_refs"], ["pattern_scan:intra_period"])
        self.assertNotIn("Data quality", claims[0]["text"])
        self.assertNotIn("outliers", claims[0]["text"])

    def test_single_period_answer_text_uses_bounded_wording(self):
        fake = FakeLLMClient(
            {
                "business_intent": {
                    "pattern_family": "custom_baseline",
                    "question_family": "pattern_explanation",
                    "target_metric": "paid_amount",
                    "scope": "full_sample",
                    "time_window": "2026-01..2026-06",
                    "target_claim": "custom baseline pattern",
                    "baseline_candidates": ["custom"],
                    "status_message": "intent",
                },
                "answer_synthesis": {
                    "answer_text": "This is a high-confidence pattern and non-random.",
                    "claims": [],
                },
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "single-period-wording",
                    "llm_client": fake,
                    "pattern_family": "custom_baseline",
                    "time_window": "2026-01..2026-06",
                    "rows": [
                        {"period": "h1", "group": "baseline", "amount": 100},
                        {"period": "h1", "group": "target", "amount": 120},
                    ],
                    "pattern_params": {
                        "period_key": "period",
                        "group_key": "group",
                        "target_group": "target",
                        "baseline_group": "baseline",
                        "min_periods": 1,
                    },
                }
            )

        answer_text = result.answer_package["sections"][0]["payload"]["answer_text"]
        self.assertNotIn("high-confidence", answer_text)
        self.assertNotIn("non-random", answer_text)
        self.assertNotIn("方向命中率", answer_text)
        self.assertNotIn("1 个可比周期", answer_text)
        self.assertNotIn("custom_baseline", answer_text)

    def test_business_narrative_answer_is_not_overwritten_by_single_period_claim(self):
        fake = FakeLLMClient(
            {
                "business_intent": {
                    "pattern_family": "custom_baseline",
                    "question_family": "pattern_explanation",
                    "target_metric": "paid_amount",
                    "scope": "full_sample",
                    "time_window": "2026-01-01..2026-06-30",
                    "target_claim": "custom baseline pattern",
                    "baseline_candidates": ["custom"],
                    "status_message": "intent",
                },
                "answer_synthesis": {
                    "answer_text": (
                        "我对问题的理解是：你想比较 Q2 相比 Q1 的付费金额表现。\n"
                        "分析思路：我先把 Q1 作为基线、Q2 作为目标窗口，"
                        "再用已验收的付费金额口径做聚合对比。\n"
                        "关键发现：Q2 相比 Q1 的付费金额提升 20.0%，当前只有 1 个可比周期。\n"
                        "结论：Q2 高于 Q1，但这只支持窗口对比。\n"
                        "需要注意：后续要继续观察更多季度和异常日。"
                    ),
                    "claims": [],
                },
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "business-narrative-kept",
                    "llm_client": fake,
                    "pattern_family": "custom_baseline",
                    "time_window": "2026-01-01..2026-06-30",
                    "baseline": {"label": "Q1"},
                    "target": {"label": "Q2"},
                    "rows": [
                        {"period": "h1", "group": "baseline", "amount": 100},
                        {"period": "h1", "group": "target", "amount": 120},
                    ],
                    "pattern_params": {
                        "period_key": "period",
                        "group_key": "group",
                        "target_group": "target",
                        "baseline_group": "baseline",
                        "min_periods": 1,
                    },
                }
            )

        answer_text = result.answer_package["sections"][0]["payload"]["answer_text"]
        self.assertIn("我对问题的理解", answer_text)
        self.assertIn("分析思路", answer_text)
        self.assertIn("关键发现", answer_text)
        self.assertIn("需要注意", answer_text)
        self.assertIn("Q2", answer_text)
        self.assertIn("Q1", answer_text)

    def test_custom_baseline_default_answer_uses_business_labels(self):
        fake = FakeLLMClient()

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "custom-labels",
                    "llm_client": fake,
                    "pattern_family": "custom_baseline",
                    "time_window": "2026-01-01..2026-06-30",
                    "baseline": {"label": "Q1"},
                    "target": {"label": "Q2"},
                    "rows": [
                        {"period": "h1", "group": "baseline", "amount": 100},
                        {"period": "h1", "group": "target", "amount": 120},
                    ],
                    "pattern_params": {
                        "period_key": "period",
                        "group_key": "group",
                        "target_group": "target",
                        "baseline_group": "baseline",
                        "min_periods": 1,
                    },
                }
            )

        answer_text = result.answer_package["sections"][0]["payload"]["answer_text"]

        self.assertIn("Q2", answer_text)
        self.assertIn("Q1", answer_text)
        self.assertIn("提升 20.0%", answer_text)
        self.assertIn("我对问题的理解", answer_text)
        self.assertIn("关键发现", answer_text)
        self.assertIn("需要注意", answer_text)
        self.assertNotIn("自定义基线付费金额对比", answer_text)

    def test_business_summary_localizes_llm_scope_and_metric_aliases(self):
        fake = FakeLLMClient(
            {"business_intent": {"scope": "all", "target_metric": "daily_paid_amount"}}
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "scope-alias",
                    "llm_client": fake,
                    "pattern_family": "custom_baseline",
                    "time_window": "2026-01-01..2026-06-30",
                    "baseline": {"label": "Q1"},
                    "target": {"label": "Q2"},
                    "rows": [
                        {"period": "h1", "group": "baseline", "amount": 100},
                        {"period": "h1", "group": "target", "amount": 120},
                    ],
                    "pattern_params": {
                        "period_key": "period",
                        "group_key": "group",
                        "target_group": "target",
                        "baseline_group": "baseline",
                        "min_periods": 1,
                    },
                }
            )

        summary = result.answer_package["sections"][0]["payload"][
            "final_business_summary"
        ]
        self.assertIn("全样本", summary)
        self.assertNotIn("口径是all", summary)
        self.assertIn("付费金额", summary)
        self.assertNotIn("daily_paid_amount", summary)

    def test_semantic_audit_revision_routes_to_repair_then_bounded_claim(self):
        fake = FakeLLMClient(
            {
                "semantic_audit": {
                    "audit_status": "needs_revision",
                    "extracted_claims": [],
                    "issues": [{"type": "duplicate_claims"}],
                }
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {"artifact_root": tmpdir, "run_id": "semantic-repair", "llm_client": fake}
            )

        self.assertIn("answer_repair", fake.calls)
        self.assertNotIn("degraded_explanation", fake.calls)
        summary = result.answer_package["sections"][0]["payload"]
        self.assertEqual(len(summary["claims"]), 1)
        self.assertEqual(summary["claims"][0]["evidence_refs"], ["pattern_scan:intra_period"])
        self.assertEqual(result.answer_package["admin_audit"]["verifier"]["errors"], [])

    def test_causal_gap_wording_is_weakened_before_verifier(self):
        fake = FakeLLMClient(
            {
                "answer_synthesis": {
                    "answer_text": (
                        "No event-based causes were identified to explain the pattern. "
                        "No event-based explanations are available due to insufficient evidence."
                    ),
                    "claims": [
                        {
                            "text": (
                                "No event-based causes were identified to explain the pattern. "
                                "No event-based explanations are available due to insufficient evidence."
                            ),
                            "evidence_refs": ["pattern_scan:intra_period"],
                            "numbers": {"median_uplift": 0.2},
                            "scope": "full_sample",
                            "time_window": "2024-01..2026-05",
                        }
                    ],
                }
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {"artifact_root": tmpdir, "run_id": "weaken-causal", "llm_client": fake}
            )

        summary = result.answer_package["sections"][0]["payload"]
        self.assertNotIn("causes", summary["answer_text"])
        self.assertNotIn("due to", summary["answer_text"])
        self.assertNotIn("causes", summary["claims"][0]["text"])
        self.assertNotIn("due to", summary["claims"][0]["text"])
        self.assertFalse(result.answer_package["admin_audit"]["verifier"]["warnings"])


if __name__ == "__main__":
    unittest.main()
