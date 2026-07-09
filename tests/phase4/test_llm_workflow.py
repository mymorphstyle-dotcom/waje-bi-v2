import json
import multiprocessing
import tempfile
import time
import unittest
from unittest.mock import patch

from bi_agent.runtime import llm_client as llm_client_module
from bi_agent.runtime.compiler import compile_graph
from bi_agent.runtime.exploration_budget import default_budget
from bi_agent.runtime.langgraph_workflow import (
    _answer_synthesis_context,
    _capability_path_labels,
    _clarification_policy_gate,
    _claims_from_llm_or_default,
    _default_claim_from_evidence,
    _execute_capabilities,
    _execute_joint_attribution,
    _final_business_summary_fallback,
    _final_summary_has_unsupported_wording,
    _final_summary_needs_display_repair,
    _infer_question_families_from_requested_nodes,
    _normalize_evidence_interpretation_output,
    _align_route_output_to_requested,
    _normalize_route_requested_nodes,
    _repair_path_invents_fixed_future_window,
    _reduce_evidence,
    evaluate_answer_quality,
    repair_final_answer_with_verified_claim,
    _route_after_next_action,
    _sanitize_terminal_explanation,
    _understand_business_intent,
    WorkflowFailure,
    run_pattern_workflow,
)
from bi_agent.runtime.llm_client import (
    LLMConfigurationError,
    LLMTimeoutError,
    OpenAICompatibleLLMClient,
    _localize_narrative_fields,
)
from bi_agent.runtime.llm_prompts import build_prompt, validate_prompt_specs
from tests.phase4.fake_llm import FakeLLMClient
from tests.phase4.fake_llm import FakeLLMResult


def spawn_safe_fake_llm_request(config, messages):
    return {
        "response_id": "subprocess-response",
        "content": '{"ok": true}',
        "usage": {},
    }


def spawn_safe_stuck_llm_request(config, messages):
    time.sleep(2)
    return {
        "response_id": "too-late",
        "content": '{"ok": true}',
        "usage": {},
    }


class SpawnTimeoutThenSuccessWorker:
    def __call__(self, config, messages):
        attempt = int(config.get("attempt", 1))
        if attempt == 1:
            time.sleep(2)
        return {
            "response_id": "subprocess-retry-success",
            "content": '{"ok": true}',
            "usage": {},
        }


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

    def test_workflow_uses_clickhouse_provider_rows_instead_of_default_rows(self):
        class Provider:
            def __init__(self):
                self.planned = False
                self.fetched = False

            def configured(self):
                return True

            def binding_reason(self):
                return ""

            def plan(self, request, intent, accepted_graph):
                from bi_agent.runtime.clickhouse_revenue_rows import RevenueRowPlan

                self.planned = True
                return RevenueRowPlan(
                    sql_text=(
                        "SELECT period, group, sum(amount) AS amount "
                        "FROM t GROUP BY period, group"
                    ),
                    query_id="query-real",
                    required_fields=("period", "group", "amount"),
                    dimension_keys=("channel", "payment_method"),
                )

            def fetch(self, plan):
                from bi_agent.runtime.clickhouse_revenue_rows import RevenueRowsResult

                self.fetched = True
                return RevenueRowsResult(
                    ok=True,
                    rows=(
                        {
                            "period": "2026-07-07",
                            "group": "baseline",
                            "amount": 100,
                            "channel": "A",
                            "payment_method": "M",
                        },
                        {
                            "period": "2026-07-08",
                            "group": "target",
                            "amount": 130,
                            "channel": "A",
                            "payment_method": "M",
                        },
                    ),
                    query_hash="hash-real",
                    query_id="query-real",
                    result_refs=("hash-real",),
                )

        provider = Provider()
        result = run_pattern_workflow(
            {
                "run_id": "clickhouse-provider-rows",
                "llm_client": FakeLLMClient(
                    {
                        "analysis_route": {
                            "requested_nodes": [
                                "compare_periods",
                                "joint_attribution",
                                "answer_verify",
                            ]
                        }
                    }
                ),
                "row_provider": provider,
            }
        )

        evidence = result.answer_package["sections"][1]["payload"]["evidence"]
        result_refs = {
            ref for item in evidence for ref in item.get("result_refs", ())
        }
        self.assertEqual(result.status, "draft")
        self.assertTrue(provider.planned)
        self.assertTrue(provider.fetched)
        self.assertIn("hash-real", result_refs)

    def test_workflow_blocks_when_clickhouse_provider_is_unconfigured(self):
        class Provider:
            def configured(self):
                return False

            def binding_reason(self):
                return "missing_clickhouse_env"

        fake = FakeLLMClient()
        result = run_pattern_workflow(
            {
                "run_id": "clickhouse-provider-missing-env",
                "llm_client": fake,
                "row_provider": Provider(),
            }
        )

        self.assertEqual(result.status, "draft")
        self.assertIn("blocked_explanation", fake.calls)
        self.assertNotIn("data_coverage_interpretation", fake.calls)
        validators = result.answer_package["admin_audit"]["validator_results"]
        clickhouse = next(item for item in validators if item["validator"] == "clickhouse_runtime")
        self.assertFalse(clickhouse["ok"])
        self.assertEqual(clickhouse["reason"], "missing_clickhouse_env")

    def test_joint_attribution_uses_clickhouse_dimension_keys(self):
        result = run_pattern_workflow(
            {
                "run_id": "joint-dimensions",
                "question": "渠道和支付方式组合共同解释昨天收入变化吗？",
                "llm_client": FakeLLMClient(
                    {
                        "analysis_route": {
                            "requested_nodes": ["joint_attribution", "answer_verify"]
                        }
                    }
                ),
                "requested_nodes": ["joint_attribution", "answer_verify"],
                "joint_dimension_keys": ("channel", "payment_method"),
                "rows": [
                    {
                        "period": "p1",
                        "group": "baseline",
                        "amount": 100,
                        "channel": "A",
                        "payment_method": "M",
                        "n": 50,
                    },
                    {
                        "period": "p1",
                        "group": "target",
                        "amount": 150,
                        "channel": "A",
                        "payment_method": "M",
                        "n": 50,
                    },
                    {
                        "period": "p1",
                        "group": "baseline",
                        "amount": 80,
                        "channel": "B",
                        "payment_method": "N",
                        "n": 50,
                    },
                    {
                        "period": "p1",
                        "group": "target",
                        "amount": 70,
                        "channel": "B",
                        "payment_method": "N",
                        "n": 50,
                    },
                ],
            }
        )

        evidence = result.answer_package["sections"][1]["payload"]["evidence"]
        joint = next(item for item in evidence if item["capability_id"] == "joint_attribution")
        self.assertEqual(joint["typed_payload"]["dimension_keys"], ["channel", "payment_method"])
        self.assertEqual(joint["evidence_type"], "statistical_association")

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

    def test_segment_causal_followup_defaults_to_observational_boundary(self):
        state = {
            "request": {"allow_question_interrupt": True},
            "checkpoint_events": [{"node": "clarification_policy_gate"}],
            "intent": {
                "question_family": "segment_or_factor_attribution",
                "primary_question_family": "segment_or_factor_attribution",
                "question_families": ["segment_or_factor_attribution"],
                "secondary_question_families": [],
                "question": "这些渠道里 WajeSpecial 是主要原因吗？",
                "target_claim": "判断 WajeSpecial 是否是主要原因",
                "ambiguous_slots": [{"slot": "claim_strength"}],
                "target_metric": "paid_amount",
                "scope": "full_sample",
                "time_window": "2024-01..2026-05",
            },
            "boundary_decision": {
                "boundary_status": "needs_question",
                "recommended_assumption": "",
                "clarification_questions": [
                    {"question": "是否允许把 WajeSpecial 写成主要原因？"},
                ],
            },
        }

        _clarification_policy_gate(state)

        self.assertEqual(
            state["clarification_outcome"]["boundary_status"],
            "low_risk_assumption",
        )
        self.assertIn("观察性归因", state["clarification_outcome"]["recommended_assumption"])

    def test_clarification_policy_gate_continues_after_user_choice(self):
        state = {
            "request": {
                "allow_question_interrupt": True,
                "clarification_choice": {
                    "answer_text": "按日粒度，移除贡献最大的正向日期后复算。",
                    "outlier_removal_strategy": "daily_remove_top_positive_day",
                },
            },
            "checkpoint_events": [{"node": "clarification_policy_gate"}],
            "intent": {
                "question_family": "custom_baseline_comparison",
                "primary_question_family": "custom_baseline_comparison",
                "question": "按日粒度，移除贡献最大的正向日期后复算。",
                "ambiguous_slots": [{"slot": "baseline"}],
                "target_metric": "paid_amount",
                "scope": "full_sample",
                "time_window": "2024-01..2026-05",
            },
            "boundary_decision": {
                "boundary_status": "needs_question",
                "recommended_assumption": "",
                "clarification_questions": [
                    {"question": "请确认异常日期移除规则。"},
                ],
            },
        }

        _clarification_policy_gate(state)

        self.assertEqual(
            state["clarification_outcome"]["boundary_status"],
            "low_risk_assumption",
        )
        self.assertIn("已按用户澄清继续", state["clarification_outcome"]["recommended_assumption"])

    def test_segment_followup_defaults_to_current_topic_boundary(self):
        state = {
            "request": {
                "allow_question_interrupt": True,
                "context_manifest": {
                    "items": [{"source_type": "topic", "source_ref": "topic-1"}]
                },
            },
            "checkpoint_events": [{"node": "clarification_policy_gate"}],
            "intent": {
                "question_family": "segment_or_factor_attribution",
                "primary_question_family": "segment_or_factor_attribution",
                "question_families": ["segment_or_factor_attribution"],
                "question": "这些变化在哪些渠道最明显？",
                "target_claim": "识别渠道变化最明显的贡献项",
                "ambiguous_slots": [{"slot": "baseline"}, {"slot": "change_measure"}],
                "target_metric": "paid_amount",
                "scope": "full_sample",
                "time_window": "2024-01..2026-05",
            },
            "boundary_decision": {
                "boundary_status": "needs_question",
                "recommended_assumption": "",
                "clarification_questions": [{"question": "请确认变化基线。"}],
            },
        }

        _clarification_policy_gate(state)

        self.assertEqual(
            state["clarification_outcome"]["boundary_status"],
            "low_risk_assumption",
        )
        self.assertIn("当前 topic", state["clarification_outcome"]["recommended_assumption"])

    def test_grain_correction_defaults_to_current_topic_boundary(self):
        state = {
            "request": {
                "allow_question_interrupt": True,
                "context_manifest": {
                    "items": [{"source_type": "topic", "source_ref": "topic-1"}]
                },
            },
            "checkpoint_events": [{"node": "clarification_policy_gate"}],
            "intent": {
                "question_family": "pattern_explanation",
                "primary_question_family": "pattern_explanation",
                "question": "口径改成按周看，还一样吗？",
                "target_claim": "按周粒度复核付费金额方向",
                "ambiguous_slots": [{"slot": "grain"}],
                "target_metric": "paid_amount",
                "scope": "full_sample",
                "time_window": "2024-01..2026-05",
            },
            "boundary_decision": {
                "boundary_status": "needs_question",
                "recommended_assumption": "",
                "clarification_questions": [{"question": "请确认原口径。"}],
            },
        }

        _clarification_policy_gate(state)

        self.assertEqual(
            state["clarification_outcome"]["boundary_status"],
            "low_risk_assumption",
        )
        self.assertIn("按周", state["clarification_outcome"]["recommended_assumption"])

    def test_daily_average_correction_defaults_to_current_topic_boundary(self):
        state = {
            "request": {
                "allow_question_interrupt": True,
                "context_manifest": {
                    "items": [{"source_type": "topic", "source_ref": "topic-1"}]
                },
            },
            "checkpoint_events": [{"node": "clarification_policy_gate"}],
            "intent": {
                "question_family": "custom_baseline_comparison",
                "primary_question_family": "custom_baseline_comparison",
                "question": "换成日均再看一遍。",
                "target_claim": "按日均付费金额重新比较",
                "ambiguous_slots": [{"slot": "baseline"}],
                "target_metric": "paid_amount",
                "scope": "full_sample",
                "time_window": "2024-01..2026-05",
            },
            "boundary_decision": {
                "boundary_status": "needs_question",
                "recommended_assumption": "",
                "clarification_questions": [{"question": "请确认日均基准。"}],
            },
        }

        _clarification_policy_gate(state)

        self.assertEqual(
            state["clarification_outcome"]["boundary_status"],
            "low_risk_assumption",
        )
        self.assertIn("日均", state["clarification_outcome"]["recommended_assumption"])

    def test_actionability_challenge_defaults_to_current_topic_boundary(self):
        state = {
            "request": {
                "allow_question_interrupt": True,
                "context_manifest": {
                    "items": [{"source_type": "topic", "source_ref": "topic-1"}]
                },
            },
            "checkpoint_events": [{"node": "clarification_policy_gate"}],
            "intent": {
                "question_family": "business_object_impact_review",
                "primary_question_family": "business_object_impact_review",
                "question": "这个结果能不能直接指导投放？",
                "target_claim": "判断当前结果能否直接指导投放",
                "ambiguous_slots": [{"slot": "action_scope"}],
                "target_metric": "paid_amount",
                "scope": "full_sample",
                "time_window": "2024-01..2026-05",
            },
            "boundary_decision": {
                "boundary_status": "needs_question",
                "recommended_assumption": "",
                "clarification_questions": [{"question": "请确认投放口径。"}],
            },
        }

        _clarification_policy_gate(state)

        self.assertEqual(
            state["clarification_outcome"]["boundary_status"],
            "low_risk_assumption",
        )
        self.assertIn("指导投放", state["clarification_outcome"]["recommended_assumption"])

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

    def test_answer_package_carries_context_audit_from_request(self):
        fake = FakeLLMClient()
        reuse_decisions = [
            {
                "source_ref": "result:q2-q1",
                "decision": "reuse",
                "reason": "validated_same_thread_scope",
                "can_support_claim": True,
                "requires_rerun": False,
            }
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "context-audit-package",
                    "llm_client": fake,
                    "context_manifest": {"manifest_id": "context-manifest-1"},
                    "reuse_decisions": reuse_decisions,
                }
            )

        self.assertEqual(result.answer_package["context_manifest_ref"], "context-manifest-1")
        self.assertEqual(result.answer_package["reuse_decisions"], reuse_decisions)

    def test_claims_carry_context_manifest_and_reuse_decisions(self):
        fake = FakeLLMClient()
        reuse_decisions = [
            {
                "source_ref": "result:q2-q1",
                "decision": "reuse",
                "reason": "validated_same_thread_scope",
                "can_support_claim": True,
                "requires_rerun": False,
            }
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "claim-context-audit",
                    "llm_client": fake,
                    "context_manifest": {"manifest_id": "context-claim-audit"},
                    "reuse_decisions": reuse_decisions,
                }
            )

        claims = result.answer_package["sections"][0]["payload"]["claims"]
        self.assertTrue(claims)
        for claim in claims:
            self.assertEqual(claim["context_manifest_ref"], "context-claim-audit")
            self.assertEqual(claim["reuse_decisions"], reuse_decisions)

    def test_request_draft_claims_are_wrapped_with_context_audit(self):
        fake = FakeLLMClient()
        reuse_decisions = [
            {
                "source_ref": "result:q2-q1",
                "decision": "reuse",
                "reason": "validated_same_thread_scope",
                "can_support_claim": True,
                "requires_rerun": False,
            }
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "request-draft-claim-context-audit",
                    "llm_client": fake,
                    "context_manifest": {"manifest_id": "context-request-draft-claim"},
                    "reuse_decisions": reuse_decisions,
                    "draft_claims": [
                        {
                            "text": "外部传入 claim 也必须进入统一证据链审计。",
                            "evidence_refs": ["pattern_scan:intra_period"],
                            "numbers": {"median_uplift": 0.2},
                            "scope": "full_sample",
                            "time_window": "2024-01..2026-05",
                        }
                    ],
                }
            )

        claims = result.answer_package["sections"][0]["payload"]["claims"]
        self.assertEqual(claims[0]["text"], "外部传入 claim 也必须进入统一证据链审计。")
        self.assertEqual(claims[0]["context_manifest_ref"], "context-request-draft-claim")
        self.assertEqual(claims[0]["reuse_decisions"], reuse_decisions)

    def test_answer_prompts_remove_unlisted_claims_and_action_advice(self):
        for task in ("answer_synthesis", "answer_repair"):
            messages = build_prompt(task, {"answer_context": {}}).messages
            text = "\n".join(message["content"] for message in messages)

            self.assertIn("unlisted claims", text)
            self.assertIn("remove them from answer_text", text)
            self.assertIn("Do not add operational action recommendations", text)

    def test_answer_repair_prompt_uses_retry_context(self):
        messages = build_prompt("answer_repair", {"answer_context": {}}).messages
        text = "\n".join(message["content"] for message in messages)

        self.assertIn("retry_context", text)
        self.assertIn("failure_reason", text)

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
        self.assertIn("当前证据能把排查方向收敛到", text)

    def test_degraded_explanation_sanitizes_unsupported_period_and_threshold_advice(self):
        with self.assertRaisesRegex(WorkflowFailure, "materiality_drift"):
            _sanitize_terminal_explanation(
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
        with self.assertRaisesRegex(WorkflowFailure, "data_or_contract_drift"):
            _sanitize_terminal_explanation(
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

    def test_llm_client_enforces_wall_clock_timeout(self):
        class SlowCompletions:
            def create(self, **kwargs):
                time.sleep(0.2)

        class SlowChat:
            completions = SlowCompletions()

        class SlowClient:
            chat = SlowChat()

        client = OpenAICompatibleLLMClient(
            provider="openai_compatible",
            model="slow-model",
            api_key="test-key",
            timeout_seconds=0.01,
        )
        client._client = SlowClient()

        with self.assertRaisesRegex(LLMTimeoutError, "llm_request_timeout"):
            client.invoke_json(
                task="business_intent",
                prompt_version="test",
                messages=[{"role": "user", "content": "{}"}],
                required_keys=[],
            )

    def test_llm_client_retries_transient_failures_three_times(self):
        attempts = {"count": 0}

        class ResponseMessage:
            content = '{"ok": true}'

        class ResponseChoice:
            message = ResponseMessage()

        class Response:
            id = "response-retry-success"
            choices = [ResponseChoice()]
            usage = None

        class FlakyCompletions:
            def create(self, **kwargs):
                attempts["count"] += 1
                if attempts["count"] < 3:
                    raise RuntimeError("temporary-network-error")
                return Response()

        class FlakyChat:
            completions = FlakyCompletions()

        class FlakyClient:
            chat = FlakyChat()

        client = OpenAICompatibleLLMClient(
            provider="openai_compatible",
            model="retry-model",
            api_key="test-key",
        )
        client._client = FlakyClient()

        result = client.invoke_json(
            task="business_intent",
            prompt_version="test",
            messages=[{"role": "user", "content": "{}"}],
            required_keys=["ok"],
        )

        self.assertEqual(result.output["ok"], True)
        self.assertEqual(attempts["count"], 3)
        self.assertEqual(result.audit["attempt_count"], 3)

    def test_llm_client_raises_after_three_failed_attempts(self):
        attempts = {"count": 0}

        class FailingCompletions:
            def create(self, **kwargs):
                attempts["count"] += 1
                raise RuntimeError("temporary-network-error")

        class FailingChat:
            completions = FailingCompletions()

        class FailingClient:
            chat = FailingChat()

        client = OpenAICompatibleLLMClient(
            provider="openai_compatible",
            model="retry-model",
            api_key="test-key",
        )
        client._client = FailingClient()

        with self.assertRaisesRegex(RuntimeError, "temporary-network-error"):
            client.invoke_json(
                task="business_intent",
                prompt_version="test",
                messages=[{"role": "user", "content": "{}"}],
                required_keys=["ok"],
            )

        self.assertEqual(attempts["count"], 3)

    def test_llm_client_does_not_cap_json_completion_tokens(self):
        captured = {}

        class ResponseMessage:
            content = '{"ok": true}'

        class ResponseChoice:
            message = ResponseMessage()

        class Response:
            id = "response-token-cap"
            choices = [ResponseChoice()]
            usage = None

        class CapturingCompletions:
            def create(self, **kwargs):
                captured.update(kwargs)
                return Response()

        class CapturingChat:
            completions = CapturingCompletions()

        class CapturingClient:
            chat = CapturingChat()

        client = OpenAICompatibleLLMClient(
            provider="openai_compatible",
            model="capturing-model",
            api_key="test-key",
        )
        client._client = CapturingClient()

        result = client.invoke_json(
            task="business_intent",
            prompt_version="test",
            messages=[{"role": "user", "content": "{}"}],
            required_keys=["ok"],
        )

        self.assertEqual(result.output["ok"], True)
        self.assertNotIn("max_tokens", captured)
        self.assertNotIn("max_completion_tokens", captured)
        self.assertEqual(captured["temperature"], 0)

    def test_llm_client_runs_openai_provider_call_in_subprocess(self):
        self.assertEqual(llm_client_module._process_context().get_start_method(), "spawn")
        client = OpenAICompatibleLLMClient(
            provider="openai_compatible",
            model="subprocess-model",
            api_key="test-key",
        )
        client._request_worker = spawn_safe_fake_llm_request
        result = client.invoke_json(
            task="business_intent",
            prompt_version="test",
            messages=[{"role": "user", "content": "{}"}],
            required_keys=["ok"],
        )

        self.assertEqual(result.output["ok"], True)
        self.assertEqual(result.audit["response_id"], "subprocess-response")
        self.assertEqual(result.audit["attempt_count"], 1)

    def test_llm_client_does_not_fall_back_to_fork_context(self):
        class ForkContext:
            def get_start_method(self):
                return "fork"

        def fake_get_context(method=None):
            if method == "spawn":
                raise ValueError("spawn unavailable")
            return ForkContext()

        with patch.object(llm_client_module.multiprocessing, "get_context", fake_get_context):
            with self.assertRaisesRegex(LLMConfigurationError, "spawn_start_method_unavailable"):
                llm_client_module._process_context()

    def test_llm_client_kills_stuck_subprocess_without_workflow_retry(self):
        client = OpenAICompatibleLLMClient(
            provider="openai_compatible",
            model="subprocess-model",
            api_key="test-key",
            timeout_seconds=0.01,
            max_attempts=1,
        )
        client._request_worker = spawn_safe_stuck_llm_request
        with self.assertRaisesRegex(LLMTimeoutError, "llm_request_timeout"):
            client.invoke_json(
                task="business_intent",
                prompt_version="test",
                messages=[{"role": "user", "content": "{}"}],
                required_keys=["ok"],
            )

    def test_llm_client_retries_after_killing_timed_out_subprocess(self):
        client = OpenAICompatibleLLMClient(
            provider="openai_compatible",
            model="subprocess-model",
            api_key="test-key",
            timeout_seconds=1.5,
            max_attempts=2,
        )
        worker = SpawnTimeoutThenSuccessWorker()
        client._request_worker = worker

        result = client.invoke_json(
            task="business_intent",
            prompt_version="test",
            messages=[{"role": "user", "content": "{}"}],
            required_keys=["ok"],
        )

        self.assertEqual(result.output["ok"], True)
        self.assertEqual(result.audit["response_id"], "subprocess-retry-success")
        self.assertEqual(result.audit["attempt_count"], 2)

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

    def test_default_claim_explains_joint_attribution_business_result(self):
        state = {
            "intent": {
                "question_family": "segment_or_factor_attribution",
                "pattern_family": "custom_baseline",
                "target_metric": "paid_amount",
                "scope": "all_users",
                "time_window": "2026-01-01..2026-06-30",
                "target_claim": "渠道和月内阶段组合是否解释Q2相比Q1的主要变化",
                "baseline": {"label": "Q1"},
                "target": {"label": "Q2"},
            },
            "request": {"question": "渠道和月内阶段组合是否解释Q2相比Q1的主要变化？"},
            "evidence_brief": {
                "limitations": [
                    "skipped_incomplete_joint_combinations",
                    "sparse_cell",
                ]
            },
            "evidence": [
                {
                    "evidence_ref": "segment_contribution:inline",
                    "capability_id": "segment_contribution",
                    "strength": "low",
                    "wording_limit": "insufficient",
                    "limitations": ["no_comparable_segments"],
                    "typed_payload": {},
                },
                {
                    "evidence_ref": "joint_attribution:inline",
                    "capability_id": "joint_attribution",
                    "strength": "medium",
                    "wording_limit": "candidate",
                    "limitations": [
                        "skipped_incomplete_joint_combinations",
                        "sparse_cell",
                    ],
                    "typed_payload": {
                        "top_3_absolute_delta_share": 0.425,
                        "leading_absolute_delta_share": 0.168,
                        "total_delta": 3984843236.0,
                        "absolute_total_delta": 3984843236.0,
                        "combination_count": 10,
                        "skipped_sparse_rows": 10,
                        "top_combinations": [
                            {
                                "dimension_values": ["WajeSpecial", "start"],
                                "delta": 668574193.0,
                                "absolute_delta_share": 0.168,
                            },
                            {
                                "dimension_values": ["WajeSpecial", "end"],
                                "delta": 603941864.0,
                                "absolute_delta_share": 0.152,
                            },
                            {
                                "dimension_values": ["WajeSpecial", "mid"],
                                "delta": 422894415.0,
                                "absolute_delta_share": 0.105,
                            },
                        ],
                    },
                }
            ],
        }

        claim = _default_claim_from_evidence(state)
        state["draft_claims"] = [claim]

        self.assertIn("WajeSpecial × 月初", claim["text"])
        self.assertIn("合计占绝对变化 42.5%", claim["text"])
        self.assertIn("观察性归因", claim["text"])
        self.assertNotIn("有边界的业务判断", claim["text"])
        self.assertEqual(claim["evidence_refs"], ["joint_attribution:inline"])
        self.assertEqual(claim["numbers"]["top_3_absolute_delta_share"], 0.425)
        self.assertTrue(
            _final_summary_needs_display_repair(
                "我对问题的理解是：已理解。\n"
                "分析脉络：已分析。\n"
                "关键发现：当前证据可以支持一个有边界的业务判断。\n"
                "最终结论：当前证据支持一个有边界的业务判断。\n"
                "需要注意：保留边界。",
                state,
            )
        )

    def test_llm_claim_prefers_established_joint_ref_and_weakens_causal_wording(self):
        state = {
            "intent": {
                "pattern_family": "custom_baseline",
                "target_metric": "paid_amount",
                "scope": "all_users",
                "time_window": "2026-01-01..2026-06-30",
            },
            "evidence": [
                {
                    "evidence_ref": "data_quality_profile:run-1",
                    "capability_id": "data_quality_profile",
                    "strength": "low",
                    "wording_limit": "degraded",
                    "typed_payload": {"scope": "all_users", "time_window": "2026-01-01..2026-06-30"},
                },
                {
                    "evidence_ref": "joint_attribution:inline",
                    "capability_id": "joint_attribution",
                    "strength": "medium",
                    "wording_limit": "candidate",
                    "typed_payload": {
                        "scope": "all_users",
                        "time_window": "2026-01-01..2026-06-30",
                        "top_3_absolute_delta_share": 0.425,
                    },
                },
            ],
        }

        claims = _claims_from_llm_or_default(
            [
                {
                    "text": "组合贡献可以作为候选解释，但不能直接写成因果结论，也不能直接定因果。",
                    "evidence_refs": [
                        "data_quality_profile:run-1",
                        "joint_attribution:inline",
                    ],
                    "numbers": {"top_3_absolute_delta_share": 0.425},
                }
            ],
            state,
        )

        self.assertEqual(claims[0]["evidence_refs"][0], "joint_attribution:inline")
        self.assertNotIn("因果结论", claims[0]["text"])
        self.assertNotIn("定因果", claims[0]["text"])
        self.assertIn("原因定论", claims[0]["text"])

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
            "next_action",
            "evidence_interpretation",
            "answer_synthesis",
            "semantic_audit",
        ):
            self.assertIn(task, fake.calls)
        coverage_audit = next(
            call
            for call in result.answer_package["admin_audit"]["llm_calls"]
            if call["task"] == "data_coverage_interpretation"
        )
        self.assertEqual(coverage_audit["provider"], "fake")
        audit_tasks = [call["task"] for call in result.answer_package["admin_audit"]["llm_calls"]]
        for task in fake.calls:
            self.assertIn(task, audit_tasks)
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

    def test_business_question_uses_llm_for_confirm_understanding(self):
        fake = FakeLLMClient()
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "confirm-understanding-llm",
                    "llm_client": fake,
                    "question": "Q2 相比 Q1 付费金额为什么变了？",
                }
            )

        self.assertEqual(result.status, "draft")
        self.assertIn("confirm_understanding", fake.calls)
        confirm_audit = next(
            call
            for call in result.answer_package["admin_audit"]["llm_calls"]
            if call["task"] == "confirm_understanding"
        )
        self.assertEqual(confirm_audit["provider"], "fake")

    def test_analysis_route_llm_failure_fails_without_local_fallback(self):
        class TimeoutOnRouteLLM(FakeLLMClient):
            def invoke_json(self, *, task, prompt_version, messages, required_keys):
                if task == "analysis_route":
                    self.calls.append(task)
                    raise TimeoutError("llm_response_timeout")
                return super().invoke_json(
                    task=task,
                    prompt_version=prompt_version,
                    messages=messages,
                    required_keys=required_keys,
                )

        fake = TimeoutOnRouteLLM()
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "route-timeout-fallback",
                    "llm_client": fake,
                    "question": "Q2相比Q1付费金额提升的主要原因是什么？",
                }
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("analysis_route", fake.calls)
        self.assertIsNone(result.answer_package)

    def test_business_question_uses_llm_for_next_action(self):
        fake = FakeLLMClient()
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "next-action-llm",
                    "llm_client": fake,
                    "question": "Q2 相比 Q1 付费金额为什么变了？",
                }
            )

        self.assertEqual(result.status, "draft")
        self.assertIn("next_action", fake.calls)
        next_action_audit = next(
            call
            for call in result.answer_package["admin_audit"]["llm_calls"]
            if call["task"] == "next_action"
        )
        self.assertEqual(next_action_audit["provider"], "fake")

    def test_next_action_llm_failure_fails_without_local_fallback(self):
        class TimeoutOnNextActionLLM(FakeLLMClient):
            def invoke_json(self, *, task, prompt_version, messages, required_keys):
                if task == "next_action":
                    self.calls.append(task)
                    raise TimeoutError("llm_response_timeout")
                return super().invoke_json(
                    task=task,
                    prompt_version=prompt_version,
                    messages=messages,
                    required_keys=required_keys,
                )

        fake = TimeoutOnNextActionLLM()
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "next-action-timeout",
                    "llm_client": fake,
                    "question": "Q2 相比 Q1 付费金额为什么变了？",
                }
            )

        self.assertEqual(result.status, "failed")
        self.assertIn("next_action", fake.calls)
        self.assertIsNone(result.answer_package)

    def test_business_question_answer_nodes_fail_without_local_fallback(self):
        for failing_task in (
            "evidence_interpretation",
            "answer_synthesis",
            "semantic_audit",
        ):
            with self.subTest(failing_task=failing_task):
                class TimeoutOnAnswerNodeLLM(FakeLLMClient):
                    def invoke_json(self, *, task, prompt_version, messages, required_keys):
                        if task == failing_task:
                            self.calls.append(task)
                            raise TimeoutError("llm_response_timeout")
                        return super().invoke_json(
                            task=task,
                            prompt_version=prompt_version,
                            messages=messages,
                            required_keys=required_keys,
                        )

                fake = TimeoutOnAnswerNodeLLM()
                with tempfile.TemporaryDirectory() as tmpdir:
                    result = run_pattern_workflow(
                        {
                            "artifact_root": tmpdir,
                            "run_id": f"{failing_task}-timeout",
                            "llm_client": fake,
                            "question": "Q2 相比 Q1 付费金额为什么变了？",
                        }
                    )

                self.assertEqual(result.status, "failed")
                self.assertIn(failing_task, fake.calls)
                self.assertEqual(fake.calls.count(failing_task), 1)
                self.assertIsNone(result.answer_package)

    def test_business_question_terminal_nodes_fail_without_local_fallback(self):
        class TimeoutOnTerminalNodeLLM(FakeLLMClient):
            def __init__(self, failing_task, overrides=None):
                super().__init__(overrides)
                self.failing_task = failing_task

            def invoke_json(self, *, task, prompt_version, messages, required_keys):
                if task == self.failing_task:
                    self.calls.append(task)
                    raise TimeoutError("llm_response_timeout")
                return super().invoke_json(
                    task=task,
                    prompt_version=prompt_version,
                    messages=messages,
                    required_keys=required_keys,
                )

        degraded_fake = TimeoutOnTerminalNodeLLM(
            "degraded_explanation",
            {
                "business_intent": {
                    "question_family": "custom_baseline_comparison",
                    "pattern_family": "custom_baseline",
                    "target_metric": "paid_amount",
                    "scope": "full_sample",
                    "time_window": "2026-01-01..2026-06-30",
                    "target_claim": "判断 Q2 相比 Q1 日均付费金额是否仍成立",
                },
                "analysis_route": {
                    "requested_nodes": [
                        "data_quality_profile",
                        "compare_periods",
                        "answer_verify",
                    ],
                },
                "next_action": {"next_action": "degrade"},
            },
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            degraded_result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "degraded-terminal-timeout",
                    "llm_client": degraded_fake,
                    "question": "换成日均再看一遍。",
                    "rows": [{"period": "q1", "group": "baseline", "amount": 100}],
                    "pattern_family": "custom_baseline",
                    "pattern_params": {
                        "period_key": "period",
                        "group_key": "group",
                        "target_group": "target",
                        "baseline_group": "baseline",
                        "min_periods": 2,
                    },
                }
            )

        self.assertEqual(degraded_result.status, "failed")
        self.assertIn("degraded_explanation", degraded_fake.calls)
        self.assertEqual(degraded_fake.calls.count("degraded_explanation"), 1)
        self.assertIsNone(degraded_result.answer_package)

        blocked_fake = TimeoutOnTerminalNodeLLM(
            "blocked_explanation",
            {
                "data_coverage_interpretation": {
                    "coverage_status": "blocked",
                    "business_impact": "当前查询没有返回可分析数据。",
                    "decision_summary": "不能发布付费金额变化结论。",
                }
            },
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            blocked_result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "blocked-terminal-timeout",
                    "llm_client": blocked_fake,
                    "question": "昨天付费金额为什么变了？",
                    "rows": [],
                }
            )

        self.assertEqual(blocked_result.status, "failed")
        self.assertIn("blocked_explanation", blocked_fake.calls)
        self.assertEqual(blocked_fake.calls.count("blocked_explanation"), 1)
        self.assertIsNone(blocked_result.answer_package)

    def test_final_business_summary_verification_failure_records_quality_issues_without_local_fallback(self):
        fake = FakeLLMClient(
            {
                "final_business_summary": {
                    "summary_text": "我已经完成检查，但这里没有保留通过校验的业务结论。"
                }
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "final-summary-verification-failed",
                    "llm_client": fake,
                    "question": "Q2 相比 Q1 付费金额为什么变了？",
                }
            )

        self.assertEqual(result.status, "draft")
        self.assertIn("final_business_summary", fake.calls)
        self.assertIn(
            "missing_required_summary_markers",
            result.answer_package["quality_gate"]["final_summary_display_warnings"],
        )
        self.assertIn("missing_verified_claim", result.answer_package["quality_gate"]["issues"])

    def test_final_answer_audit_warning_retries_summary_once_without_blocking_display(self):
        class RetryAuditLLM(FakeLLMClient):
            def __init__(self):
                super().__init__()
                self.summary_inputs = []
                self.audit_count = 0

            def invoke_json(self, *, task, prompt_version, messages, required_keys):
                if task == "final_business_summary":
                    self.calls.append(task)
                    payload = _input_payload(messages)
                    self.summary_inputs.append(payload)
                    summary_text = (
                        "我对问题的理解是：你想看 Q2 相比 Q1 的付费金额变化。\n"
                        "分析脉络：我检查了目标窗口、基线窗口和贡献证据。\n"
                        "关键发现：Q2 相比 Q1 的付费金额提升 20.0%。\n"
                        "最终结论：已验证结论是：Q2 相比 Q1 的付费金额提升 20.0%。\n"
                        "需要注意：还不能直接说这是唯一原因或已被因果证明。"
                    )
                    if payload.get("final_answer_retry_instruction"):
                        summary_text = (
                            "我对问题的理解是：你想看 Q2 相比 Q1 的付费金额变化。\n"
                            "分析脉络：我检查了目标窗口、基线窗口和贡献证据。\n"
                            "关键发现：Q2 相比 Q1 的付费金额提升 20.0%，当前证据能把排查方向收敛到渠道贡献方向。\n"
                            "最终结论：已验证结论是：Q2 相比 Q1 的付费金额提升 20.0%。"
                            "当前证据能把排查方向收敛到渠道贡献方向。\n"
                            "需要注意：还不能直接说这是唯一原因或已被因果证明。"
                        )
                    return FakeLLMResult(
                        {"summary_text": summary_text, "display_summary": "已生成最终业务总结。"},
                        {
                            "task": task,
                            "provider": "fake",
                            "model": "fake-model",
                            "prompt_version": prompt_version,
                            "response_id": f"fake-{task}-{len(self.summary_inputs)}",
                            "messages": [dict(message) for message in messages],
                            "required_keys": list(required_keys),
                            "raw_response_content": "{}",
                            "started_at": "2026-01-01T00:00:00+00:00",
                            "finished_at": "2026-01-01T00:00:00+00:00",
                            "duration_ms": 0.0,
                            "input_hash": f"input-{task}-{len(self.summary_inputs)}",
                            "output_hash": f"output-{task}-{len(self.summary_inputs)}",
                            "usage": {},
                            "structured_output": {"summary_text": summary_text},
                        },
                    )
                if task == "final_answer_audit":
                    self.calls.append(task)
                    self.audit_count += 1
                    output = {
                        "display_status": "ready_with_warnings",
                        "hard_blockers": [],
                        "repairable_warnings": ["missing_business_interpretation"],
                        "retry_instruction": "补一句业务排查方向。",
                        "business_audit_summary": "答案可展示，但业务洞察还不够完整。",
                        "display_summary": "答案可展示，但建议补强业务洞察。",
                    }
                    if self.audit_count > 1:
                        output = {
                            "display_status": "ready",
                            "hard_blockers": [],
                            "repairable_warnings": [],
                            "retry_instruction": "",
                            "business_audit_summary": "答案满足当前展示边界。",
                            "display_summary": "答案满足当前展示边界。",
                        }
                    return FakeLLMResult(
                        output,
                        {
                            "task": task,
                            "provider": "fake",
                            "model": "fake-model",
                            "prompt_version": prompt_version,
                            "response_id": f"fake-{task}-{self.audit_count}",
                            "messages": [dict(message) for message in messages],
                            "required_keys": list(required_keys),
                            "raw_response_content": "{}",
                            "started_at": "2026-01-01T00:00:00+00:00",
                            "finished_at": "2026-01-01T00:00:00+00:00",
                            "duration_ms": 0.0,
                            "input_hash": f"input-{task}-{self.audit_count}",
                            "output_hash": f"output-{task}-{self.audit_count}",
                            "usage": {},
                            "structured_output": output,
                        },
                    )
                return super().invoke_json(
                    task=task,
                    prompt_version=prompt_version,
                    messages=messages,
                    required_keys=required_keys,
                )

        def _input_payload(messages):
            for message in messages:
                content = message.get("content", "") if isinstance(message, dict) else ""
                if "<input_json>" not in content:
                    continue
                start = content.index("<input_json>") + len("<input_json>")
                end = content.index("</input_json>")
                return json.loads(content[start:end].strip())
            return {}

        fake = RetryAuditLLM()
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "final-answer-audit-retry",
                    "llm_client": fake,
                    "question": "Q2 相比 Q1 付费金额为什么变了？",
                }
            )

        self.assertEqual(result.status, "draft")
        self.assertEqual(fake.calls.count("final_business_summary"), 2)
        self.assertEqual(fake.calls.count("final_answer_audit"), 2)
        self.assertEqual(fake.summary_inputs[0].get("final_answer_retry_instruction"), "")
        self.assertEqual(fake.summary_inputs[1].get("final_answer_retry_instruction"), "补一句业务排查方向。")
        self.assertFalse(result.answer_package["quality_gate"]["blocks_display"])
        self.assertEqual(result.answer_package["quality_gate"]["display_status"], "ready")
        self.assertEqual(result.answer_package["quality_gate"]["repairable_warnings"], [])
        self.assertIn("当前证据能把排查方向收敛到渠道贡献方向", result.answer_package["final_answer"])

    def test_final_business_summary_timeout_keeps_answer_synthesis_with_audit_marker(self):
        class TimeoutOnFinalSummaryLLM(FakeLLMClient):
            def invoke_json(self, *, task, prompt_version, messages, required_keys):
                if task == "final_business_summary":
                    self.calls.append(task)
                    raise TimeoutError("llm_response_timeout")
                return super().invoke_json(
                    task=task,
                    prompt_version=prompt_version,
                    messages=messages,
                    required_keys=required_keys,
                )

        fake = TimeoutOnFinalSummaryLLM()
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "final-summary-timeout-keeps-answer",
                    "llm_client": fake,
                    "question": "Q2 相比 Q1 付费金额为什么变了？",
                }
            )

        self.assertEqual(result.status, "draft")
        self.assertIn("final_business_summary", fake.calls)
        summary = result.answer_package["sections"][0]["payload"]["final_business_summary"]
        self.assertEqual(summary, result.answer_package["sections"][0]["payload"]["answer_text"])
        self.assertIn(
            "final_summary_timeout",
            result.answer_package["quality_gate"]["final_summary_display_warnings"],
        )

    def test_terminal_explanation_rejected_output_fails_without_local_fallback(self):
        degraded_fake = FakeLLMClient(
            {
                "next_action": {"next_action": "synthesize_answer"},
                "degraded_explanation": {
                    "status": "degraded",
                    "explanation": "pattern_status: low; wording_limit: insufficient.",
                    "owner": "pattern_scan:intra_period",
                    "repair_path": "Inspect evidence_ref.",
                },
            }
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            degraded_result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "degraded-rejected-output",
                    "llm_client": degraded_fake,
                    "rows": [{"month": "2026-01", "phase": "start", "amount": 100}],
                    "time_window": "2026-01..2026-06",
                    "pattern_params": {"target_phase": "start", "min_periods": 6},
                }
            )

        self.assertEqual(degraded_result.status, "failed")
        self.assertIn("degraded_explanation", degraded_fake.calls)
        self.assertIsNone(degraded_result.answer_package)

        blocked_fake = FakeLLMClient(
            {
                "data_coverage_interpretation": {
                    "coverage_status": "blocked",
                    "business_impact": "当前查询没有返回可分析数据。",
                    "decision_summary": "不能发布付费金额变化结论。",
                },
                "blocked_explanation": {
                    "status": "not_blocked",
                    "explanation": "所有检查已通过，无需阻塞。",
                    "owner": "业务分析师",
                    "repair_path": "无需修复。",
                },
            }
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            blocked_result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "blocked-rejected-output",
                    "llm_client": blocked_fake,
                    "rows": [],
                }
            )

        self.assertEqual(blocked_result.status, "failed")
        self.assertIn("blocked_explanation", blocked_fake.calls)
        self.assertIsNone(blocked_result.answer_package)

    def test_business_intent_llm_timeout_falls_back_to_local_intent(self):
        class TimeoutOnIntentLLM(FakeLLMClient):
            def invoke_json(self, *, task, prompt_version, messages, required_keys):
                if task == "business_intent":
                    self.calls.append(task)
                    raise TimeoutError("llm_response_timeout")
                return super().invoke_json(
                    task=task,
                    prompt_version=prompt_version,
                    messages=messages,
                    required_keys=required_keys,
                )

        fake = TimeoutOnIntentLLM()
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "intent-timeout-fallback",
                    "llm_client": fake,
                    "question": "Q2相比Q1付费金额提升的主要原因是什么？",
                }
            )

        self.assertEqual(result.status, "draft")
        intent_audit = next(
            call
            for call in result.answer_package["admin_audit"]["llm_calls"]
            if call["task"] == "business_intent"
        )
        self.assertEqual(intent_audit["provider"], "local_fallback")
        self.assertEqual(intent_audit["failure_type"], "llm_unavailable")
        self.assertIn("driver_decomposition", result.answer_package["accepted_graph"])

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

    def test_route_normalization_preserves_joint_attribution_for_combination_asks(self):
        normalized = _normalize_route_requested_nodes(
            ("joint_attribution", "answer_verify"),
            {
                "question_family": "segment_or_factor_attribution",
                "target_claim": "判断渠道和月内阶段组合是否解释主要变化",
                "pattern_params": {"joint_dimension_keys": ("channel", "phase")},
            },
        )

        self.assertIn("joint_attribution", normalized)
        self.assertIn("segment_contribution", normalized)

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

    def test_answer_synthesis_context_includes_capability_business_findings(self):
        state = {
            "intent": {
                "question_family": "custom_baseline_comparison",
                "pattern_family": "custom_baseline",
                "target_metric": "paid_amount",
                "scope": "full_sample",
                "time_window": "2026-01-01..2026-06-30",
                "target_claim": "去掉异常后方向是否还成立",
                "baseline": {"label": "Q1"},
                "target": {"label": "Q2"},
            },
            "request": {"question": "去掉异常后还成立吗？"},
            "evidence_brief": {"limitations": []},
            "causal_evidence_dossier": {},
            "causal_audit": {},
            "evidence": [
                {
                    "evidence_ref": "outlier_contribution:inline",
                    "capability_id": "outlier_contribution",
                    "strength": "medium",
                    "wording_limit": "contextual",
                    "typed_payload": {
                        "business_readout": "移除最大正向日期后，方向仍为上升。",
                        "claim_boundary": "只能说明异常敏感性，不能当作因果证明。",
                        "top_positive_share": 0.6,
                        "remaining_delta_after_top_positive": 20.0,
                    },
                    "limitations": [],
                    "result_refs": ["sqlhash-1"],
                }
            ],
        }

        context = _answer_synthesis_context(state)

        self.assertEqual(
            context["capability_business_findings"],
            [
                {
                    "capability": "outlier_contribution",
                    "business_readout": "移除最大正向日期后，方向仍为上升。",
                    "claim_boundary": "只能说明异常敏感性，不能当作因果证明。",
                    "evidence_refs": ["sqlhash-1"],
                }
            ],
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

    def test_route_normalization_adds_answer_verify_for_change_reason_questions(self):
        nodes = _normalize_route_requested_nodes(
            ("driver_decomposition",),
            {
                "question": "Q2 相比 Q1 付费金额为什么变了？",
                "question_family": "paid_amount_change_explanation",
                "pattern_family": "custom_baseline",
                "target_metric": "paid_amount",
            },
        )

        self.assertIn("driver_decomposition", nodes)
        self.assertIn("answer_verify", nodes)

    def test_route_normalization_adds_answer_verify_for_main_reason_attribution(self):
        nodes = _normalize_route_requested_nodes(
            ("pattern_scan",),
            {
                "question": "这些渠道里 WajeSpecial 是主要原因吗？",
                "question_family": "pattern_explanation",
                "pattern_family": "custom_baseline",
                "target_metric": "paid_amount",
            },
        )

        self.assertIn("joint_attribution", nodes)
        self.assertIn("answer_verify", nodes)

    def test_route_normalization_keeps_llm_requested_segment_for_compiler_audit(self):
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
        self.assertIn("segment_contribution", nodes)

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

    def test_route_normalization_adds_joint_attribution_for_major_channel_followups(self):
        nodes = _normalize_route_requested_nodes(
            ("data_quality_profile", "segment_contribution", "answer_verify"),
            {
                "question": "这些渠道里 WajeSpecial 是主要原因吗？",
                "question_family": "segment_or_factor_attribution",
                "primary_question_family": "segment_or_factor_attribution",
                "pattern_family": "custom_baseline",
                "target_claim": "判断渠道贡献最大项是否能解释付费金额变化",
                "target_metric": "paid_amount",
            },
        )

        self.assertIn("segment_contribution", nodes)
        self.assertIn("joint_attribution", nodes)

    def test_route_normalization_adds_joint_attribution_for_most_obvious_channel_change(self):
        nodes = _normalize_route_requested_nodes(
            ("data_quality_profile", "segment_contribution"),
            {
                "question": "这些变化在哪些渠道最明显？",
                "question_family": "segment_or_factor_attribution",
                "primary_question_family": "segment_or_factor_attribution",
                "pattern_family": "custom_baseline",
                "target_claim": "识别变化最明显的渠道",
                "target_metric": "paid_amount",
            },
        )

        self.assertIn("joint_attribution", nodes)

    def test_route_normalization_adds_outlier_recalc_for_daily_removal_clarification(self):
        nodes = _normalize_route_requested_nodes(
            ("data_quality_profile", "compare_periods", "answer_verify"),
            {
                "question": "按日粒度，移除贡献最大的正向日期后复算，不做订单级明细剔除。",
                "question_family": "custom_baseline_comparison",
                "primary_question_family": "custom_baseline_comparison",
                "pattern_family": "custom_baseline",
                "target_claim": "移除贡献最大的正向日期后复算付费金额方向",
                "target_metric": "paid_amount",
            },
        )

        self.assertIn("outlier_scan", nodes)
        self.assertIn("outlier_contribution", nodes)

    def test_route_normalization_keeps_compare_and_verify_for_daily_average_corrections(self):
        nodes = _normalize_route_requested_nodes(
            ("data_quality_profile", "driver_decomposition"),
            {
                "question": "换成日均再看一遍。",
                "question_family": "revenue_health_review",
                "primary_question_family": "revenue_health_review",
                "pattern_family": "custom_baseline",
                "target_claim": "按日均付费金额重新比较 Q2 和 Q1",
                "target_metric": "paid_amount",
            },
        )

        self.assertIn("compare_periods", nodes)
        self.assertIn("answer_verify", nodes)

    def test_route_normalization_keeps_compare_and_verify_for_weekly_corrections(self):
        nodes = _normalize_route_requested_nodes(
            ("data_quality_profile",),
            {
                "question": "口径改成按周看，还一样吗？",
                "question_family": "pattern_explanation",
                "primary_question_family": "pattern_explanation",
                "pattern_family": "rolling",
                "target_claim": "按周粒度复核付费金额方向",
                "target_metric": "paid_amount",
            },
        )

        self.assertIn("compare_periods", nodes)
        self.assertIn("answer_verify", nodes)

    def test_weekly_grain_correction_without_weekday_target_uses_period_compare(self):
        fake = FakeLLMClient(
            {
                "business_intent": {
                    "question_family": "pattern_explanation",
                    "pattern_family": "weekly",
                    "target_metric": "paid_amount",
                    "scope": "full_sample",
                    "time_window": "2024-01..2026-05",
                    "target_claim": "按周粒度复核付费金额方向是否一致",
                },
                "analysis_route": {
                    "requested_nodes": [
                        "data_quality_profile",
                        "compare_periods",
                        "answer_verify",
                    ],
                },
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "weekly-grain-period-compare",
                    "llm_client": fake,
                    "question": "口径改成按周看，还一样吗？",
                    "rows": [
                        {"period": "2026-W01", "group": "baseline", "amount": 100},
                        {"period": "2026-W02", "group": "target", "amount": 120},
                    ],
                    "pattern_params": {
                        "period_key": "period",
                        "group_key": "group",
                        "target_group": "target",
                        "baseline_group": "baseline",
                    },
                }
            )

        self.assertEqual(result.status, "draft")
        self.assertIn("compare_periods", result.answer_package["accepted_graph"])
        errors = result.answer_package["admin_audit"]["verifier"].get("errors", [])
        self.assertFalse(
            any("target_weekday" in str(error) for error in errors)
        )

    def test_business_intent_preserves_llm_pattern_params(self):
        fake = FakeLLMClient(
            {
                "business_intent": {
                    "question_family": "pattern_explanation",
                    "pattern_family": "weekly",
                    "pattern_params": {
                        "week_key": "week",
                        "weekday_key": "weekday",
                        "target_weekdays": [6, 7],
                        "baseline_weekdays": [1, 2, 3, 4, 5],
                    },
                }
            }
        )
        state = {
            "request": {"question": "最近付费金额是不是周末更高？"},
            "run_id": "intent-pattern-params",
            "llm_client": fake,
            "llm_calls": [],
        }

        _understand_business_intent(state)

        self.assertEqual(state["intent"]["pattern_family"], "weekly")
        self.assertEqual(state["intent"]["pattern_params"]["target_weekdays"], [6, 7])
        self.assertEqual(state["intent"]["pattern_params"]["baseline_weekdays"], [1, 2, 3, 4, 5])

    def test_business_intent_binds_weekend_pattern_params_from_business_text(self):
        fake = FakeLLMClient(
            {
                "business_intent": {
                    "question_family": "pattern_explanation",
                    "pattern_family": "weekly",
                    "pattern_params": {},
                }
            }
        )
        state = {
            "request": {"question": "最近付费金额是否存在固定规律，比如周末更高？"},
            "run_id": "intent-weekend-default",
            "llm_client": fake,
            "llm_calls": [],
        }

        _understand_business_intent(state)

        self.assertEqual(state["intent"]["pattern_params"]["target_weekdays"], [6, 7])
        self.assertEqual(state["intent"]["pattern_params"]["baseline_weekdays"], [1, 2, 3, 4, 5])

    def test_route_normalization_keeps_answer_verify_for_actionability_challenges(self):
        nodes = _normalize_route_requested_nodes(
            ("data_quality_profile",),
            {
                "question": "这个结果能不能直接指导投放？",
                "question_family": "revenue_health_review",
                "primary_question_family": "revenue_health_review",
                "target_claim": "判断当前结果能否直接指导投放",
                "target_metric": "paid_amount",
            },
        )

        self.assertIn("answer_verify", nodes)

    def test_route_normalization_keeps_answer_verify_for_stability_challenges(self):
        nodes = _normalize_route_requested_nodes(
            ("data_quality_profile",),
            {
                "question": "这些结果有多稳？",
                "question_family": "pattern_explanation",
                "primary_question_family": "pattern_explanation",
                "target_claim": "判断当前结果稳健性",
                "target_metric": "paid_amount",
            },
        )

        self.assertIn("answer_verify", nodes)

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

    def test_requested_compare_periods_infers_custom_baseline_family(self):
        intent = {
            "question_family": "revenue_health_review",
            "primary_question_family": "revenue_health_review",
            "question_families": ["revenue_health_review"],
            "secondary_question_families": [],
        }

        _infer_question_families_from_requested_nodes(
            intent,
            ("data_quality_profile", "compare_periods", "answer_verify"),
        )

        self.assertIn("custom_baseline_comparison", intent["question_families"])
        self.assertIn("custom_baseline_comparison", intent["secondary_question_families"])

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

    def test_custom_baseline_pattern_route_preserves_rolling_and_adds_period_compare(self):
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
        self.assertIn("rolling_window_compare", result.answer_package["accepted_graph"])
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

    def test_degrade_override_rewrites_next_action_for_audit(self):
        state = {
            "checkpoint_events": [{"node": "decide_next_action"}],
            "next_action": {
                "next_action": "degrade",
                "decision_summary": "数据缺失，无法回答。",
            },
            "intent": {
                "pattern_family": "custom_baseline",
                "scope": "all_users",
                "time_window": "2026-01-01..2026-06-30",
            },
            "evidence": [
                {
                    "evidence_ref": "joint_attribution:inline",
                    "capability_id": "joint_attribution",
                    "strength": "medium",
                    "wording_limit": "candidate",
                    "typed_payload": {},
                }
            ],
        }

        route = _route_after_next_action(state)

        self.assertEqual(route, "synthesize")
        self.assertEqual(state["next_action"]["next_action"], "synthesize_answer")
        self.assertIn("不能把可回答结果降级", state["next_action"]["decision_summary"])
        self.assertEqual(
            state["checkpoint_events"][-1]["route"],
            "degrade_overridden_to_bounded_answer",
        )

    def test_degraded_execution_emits_auditable_evidence_and_claim(self):
        fake = FakeLLMClient(
            {
                "business_intent": {
                    "question_family": "custom_baseline_comparison",
                    "pattern_family": "custom_baseline",
                    "target_metric": "paid_amount",
                    "scope": "full_sample",
                    "time_window": "2026-01-01..2026-06-30",
                    "target_claim": "判断 Q2 相比 Q1 日均付费金额是否仍成立",
                },
                "analysis_route": {
                    "requested_nodes": [
                        "data_quality_profile",
                        "compare_periods",
                        "answer_verify",
                    ],
                },
                "next_action": {"next_action": "degrade"},
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "degraded-audit",
                    "llm_client": fake,
                    "question": "换成日均再看一遍。",
                    "rows": [{"period": "q1", "group": "baseline", "amount": 100}],
                    "pattern_family": "custom_baseline",
                    "pattern_params": {
                        "period_key": "period",
                        "group_key": "group",
                        "target_group": "target",
                        "baseline_group": "baseline",
                        "min_periods": 2,
                    },
                }
            )

        summary = result.answer_package["sections"][0]["payload"]
        evidence = result.answer_package["sections"][1]["payload"]["evidence"]

        self.assertEqual(result.status, "draft")
        self.assertTrue(evidence)
        self.assertTrue(summary["claims"])
        evidence_refs = {item["evidence_ref"] for item in evidence}
        self.assertTrue(set(summary["claims"][0]["evidence_refs"]).issubset(evidence_refs))
        self.assertTrue(result.answer_package["quality_gate"]["has_verified_claims"])

    def test_synthesize_next_action_conflict_text_is_repaired_for_audit(self):
        state = {
            "checkpoint_events": [{"node": "decide_next_action"}],
            "next_action": {
                "next_action": "synthesize_answer",
                "decision_summary": "由于缺少渠道字段，联合归因无法执行。",
                "display_summary": "证据不足以完成归因。",
            },
            "intent": {
                "pattern_family": "custom_baseline",
                "scope": "all_users",
                "time_window": "2026-01-01..2026-06-30",
            },
            "evidence": [
                {
                    "evidence_ref": "joint_attribution:inline",
                    "capability_id": "joint_attribution",
                    "strength": "medium",
                    "wording_limit": "candidate",
                    "typed_payload": {},
                }
            ],
        }

        route = _route_after_next_action(state)

        self.assertEqual(route, "synthesize")
        self.assertIn("继续生成答案", state["next_action"]["decision_summary"])
        self.assertNotIn("无法执行", state["next_action"]["decision_summary"])
        self.assertEqual(
            state["checkpoint_events"][-1]["route"],
            "synthesize_action_text_repaired",
        )

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

        self.assertEqual(result.status, "failed")
        self.assertIn("degraded_explanation_rejected:internal_tokens", result.failure_reason)
        self.assertIsNone(result.answer_package)

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

        with self.assertRaisesRegex(WorkflowFailure, "materiality_drift"):
            _sanitize_terminal_explanation(output, state, "degraded")

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

        with self.assertRaisesRegex(WorkflowFailure, "materiality_drift"):
            _sanitize_terminal_explanation(output, state, "degraded")

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

        with self.assertRaisesRegex(WorkflowFailure, "materiality_drift"):
            _sanitize_terminal_explanation(output, state, "degraded")

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

    def test_llm_block_without_local_data_failure_continues_as_answerable_warning(self):
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
            "coverage_gap_but_answerable",
        )
        self.assertEqual(
            coverage["local_override"],
            "blocked_without_local_evidence",
        )

    def test_blocked_coverage_emits_auditable_evidence_and_claim(self):
        fake = FakeLLMClient(
            {
                "data_coverage_interpretation": {
                    "coverage_status": "blocked",
                    "business_impact": "当前查询没有返回可分析数据。",
                    "decision_summary": "不能发布付费金额变化结论。",
                },
                "blocked_explanation": {
                    "status": "blocked",
                    "explanation": "当前查询没有返回可分析数据，不能发布付费金额变化结论。",
                    "owner": "业务分析师",
                    "repair_path": "先恢复支付金额聚合数据后重跑。",
                },
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "coverage-blocked-audit",
                    "llm_client": fake,
                    "rows": [],
                }
            )

        summary = result.answer_package["sections"][0]["payload"]
        evidence = result.answer_package["sections"][1]["payload"]["evidence"]

        self.assertEqual(result.status, "draft")
        self.assertTrue(evidence)
        self.assertTrue(summary["claims"])
        claim = summary["claims"][0]
        self.assertEqual(evidence[0]["evidence_type"], "insufficient")
        self.assertEqual(evidence[0]["strength"], "insufficient")
        self.assertEqual(claim["evidence_refs"], [evidence[0]["evidence_ref"]])
        self.assertIn("当前数据覆盖不足", claim["text"])
        self.assertNotIn("无需阻塞", result.answer_package["final_explanation"]["explanation"])
        self.assertTrue(result.answer_package["quality_gate"]["has_verified_claims"])
        self.assertIn(claim["text"], result.answer_package["final_answer"])

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

    def test_answer_repair_receives_semantic_audit_failure_reason(self):
        fake = FakeLLMClient(
            {
                "semantic_audit": {
                    "audit_status": "needs_revision",
                    "extracted_claims": [],
                    "issues": [{"type": "unsupported_claim", "message": "答案声明超出证据"}],
                }
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {"artifact_root": tmpdir, "run_id": "semantic-retry-reason", "llm_client": fake}
            )

        payload = _llm_input_payload(result.answer_package, "answer_repair")
        retry = payload["retry_context"]
        self.assertEqual(retry["failed_node"], "semantic_audit")
        self.assertEqual(retry["failure_type"], "semantic_audit")
        self.assertIn("答案声明超出证据", retry["failure_reason"])

    def test_answer_repair_receives_verifier_failure_reason(self):
        fake = FakeLLMClient(
            {
                "answer_synthesis": {
                    "answer_text": "这里引用了不存在的证据。",
                }
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "verifier-retry-reason",
                    "llm_client": fake,
                    "draft_claims": [
                        {
                            "text": "这里保留了错误数字。",
                            "evidence_refs": ["pattern_scan:intra_period"],
                            "numbers": {"median_uplift": 9.9},
                            "scope": "full_sample",
                            "time_window": "2024-01..2026-05",
                        }
                    ],
                }
            )

        payload = _llm_input_payload(result.answer_package, "answer_repair")
        retry = payload["retry_context"]
        self.assertEqual(retry["failed_node"], "hard_verify_answer")
        self.assertEqual(retry["failure_type"], "verifier")
        self.assertIn("number_mismatch", retry["failure_reason"])

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

    def test_final_summary_allows_causal_question_wording_without_published_cause(self):
        state = {
            "intent": {"pattern_family": "custom_baseline"},
            "evidence": [
                {
                    "evidence_ref": "event_evidence:inline",
                    "evidence_type": "insufficient_evidence",
                    "typed_payload": {},
                }
            ],
            "draft_claims": [
                {
                    "text": "活动窗口证据只能作为候选机制检查。",
                    "evidence_refs": ["event_evidence:inline"],
                    "numbers": {},
                }
            ],
        }
        summary = (
            "我对问题的理解是：你想判断活动是否导致付费金额变化。\n"
            "分析脉络：我检查了事件窗口和付费金额变化。\n"
            "关键发现：目前只能看到事件窗口和指标变化的对应关系。\n"
            "最终结论：当前证据不能把活动写成已证明原因。\n"
            "需要注意：还需要补充事件和投放证据。"
        )

        self.assertFalse(_final_summary_has_unsupported_wording(summary, state))
        self.assertTrue(
            _final_summary_has_unsupported_wording(
                summary.replace("不能把活动写成已证明原因", "活动导致了付费金额变化"),
                state,
            )
        )

    def _run_q2_q1_joint_attribution_workflow(self):
        fake = FakeLLMClient(
            {
                "business_intent": {
                    "question_family": "segment_or_factor_attribution",
                    "pattern_family": "custom_baseline",
                    "target_metric": "paid_amount",
                    "scope": "full_sample",
                    "time_window": "2026-01-01..2026-06-30",
                    "target_claim": "判断渠道和月内阶段组合是否解释主要变化",
                },
                "analysis_route": {
                    "requested_nodes": ["joint_attribution", "answer_verify"],
                },
                "answer_synthesis": {
                    "answer_text": "草稿结论：WajeSpecial 月初组合贡献最大。",
                    "claims": [
                        {
                            "text": "WajeSpecial 月初组合是当前 Q2 相比 Q1 付费金额变化里贡献最大的候选组合。",
                            "evidence_refs": ["joint_attribution:inline"],
                            "numbers": {"top_combination_share": 0.8974},
                            "scope": "full_sample",
                            "time_window": "2026-01-01..2026-06-30",
                        }
                    ],
                },
            }
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            return run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "q2-q1-joint-quality",
                    "llm_client": fake,
                    "question": "Q2 相比 Q1 付费金额为什么变了？哪些渠道和月内阶段组合贡献最大？",
                    "pattern_family": "custom_baseline",
                    "time_window": "2026-01-01..2026-06-30",
                    "baseline": {"label": "Q1"},
                    "target": {"label": "Q2"},
                    "rows": [
                        {
                            "period": "q1",
                            "group": "baseline",
                            "channel": "WajeSpecial",
                            "phase": "start",
                            "amount": 100,
                        },
                        {
                            "period": "q2",
                            "group": "target",
                            "channel": "WajeSpecial",
                            "phase": "start",
                            "amount": 170,
                        },
                        {
                            "period": "q1",
                            "group": "baseline",
                            "channel": "Organic",
                            "phase": "mid",
                            "amount": 80,
                        },
                        {
                            "period": "q2",
                            "group": "target",
                            "channel": "Organic",
                            "phase": "mid",
                            "amount": 88,
                        },
                    ],
                    "required_fields": ["period", "group", "channel", "phase", "amount"],
                    "pattern_params": {
                        "period_key": "period",
                        "group_key": "group",
                        "target_group": "target",
                        "baseline_group": "baseline",
                        "joint_dimension_keys": ("channel", "phase"),
                    },
                }
            )

    def test_final_answer_contains_business_insight_and_verified_claim(self):
        result = self._run_q2_q1_joint_attribution_workflow()
        answer = result.answer_package["final_answer"]
        self.assertIn("当前证据能把排查方向收敛到", answer)
        self.assertIn("还不能直接说", answer)
        self.assertTrue(result.answer_package["quality_gate"]["business_insight_present"])

    def test_followup_questions_are_single_intent(self):
        result = self._run_q2_q1_joint_attribution_workflow()
        questions = result.answer_package["follow_up_questions"]
        self.assertEqual(len(questions), 3)
        for question in questions:
            self.assertLessEqual(question.count("，"), 2)
            self.assertNotIn("以及", question)

    def test_quality_gate_repair_preserves_verified_claim_text(self):
        claim_text = "Q2 相比 Q1 的付费金额提升 20.0%，当前只支持窗口对比结论。"
        repaired = repair_final_answer_with_verified_claim(
            {
                "request": {"question": "Q2 相比 Q1 付费金额为什么变了？"},
                "intent": {
                    "target_metric": "paid_amount",
                    "scope": "full_sample",
                    "time_window": "2026-01-01..2026-06-30",
                    "pattern_family": "custom_baseline",
                    "target": {"label": "Q2"},
                    "baseline": {"label": "Q1"},
                },
                "final_business_summary": (
                    "我对问题的理解是：你想看 Q2 相比 Q1 的付费金额变化。\n"
                    "分析脉络：我检查了目标窗口和基线窗口的聚合证据。\n"
                    "关键发现：当前有可发布证据。\n"
                    "最终结论：通过了校验。\n"
                    "需要注意：不能写成因果证明。"
                ),
                "draft_claims": [{"text": claim_text}],
                "verifier": {"errors": []},
            },
            {},
        )

        self.assertIn(claim_text, repaired)
        self.assertIn("当前证据能把排查方向收敛到", repaired)
        self.assertIn("还不能直接说", repaired)

    def test_quality_gate_rejects_answers_without_verified_claims(self):
        quality = evaluate_answer_quality(
            user_question="Q2 相比 Q1 付费金额为什么变了？",
            verified_claims=[],
            final_answer="最终结论：当前证据能把排查方向收敛到渠道贡献。",
            follow_up_questions=[
                "要看渠道贡献吗？",
                "要复核异常日期吗？",
                "要换成日均口径吗？",
            ],
        )

        self.assertFalse(quality["has_verified_claims"])
        self.assertFalse(quality["verified_claim_preserved"])
        self.assertIn("missing_verified_claim", quality["issues"])


if __name__ == "__main__":
    unittest.main()
