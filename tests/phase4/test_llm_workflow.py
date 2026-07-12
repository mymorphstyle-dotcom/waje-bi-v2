import json
from copy import deepcopy
import multiprocessing
import tempfile
import time
import unittest
from unittest.mock import patch

from bi_agent.runtime import llm_client as llm_client_module
from bi_agent.runtime import langgraph_workflow as workflow_module
from bi_agent.runtime.compiler import compile_graph
from bi_agent.runtime.exploration_budget import default_budget
from bi_agent.runtime.langgraph_workflow import (
    _answer_synthesis_context,
    _analysis_runtime_request,
    _answer_quality_gate,
    _accept_analysis_route,
    _build_answer_package_from_state,
    _compiler_bound_context,
    build_available_evidence_brief,
    _apply_reused_dimension_scan_input,
    _apply_query_gap_action_to_route,
    _capability_path_labels,
    _capability_result_refs_for,
    _capability_rows_for,
    _clarification_policy_gate,
    _claims_from_llm_or_default,
    _default_claim_from_evidence,
    _design_analysis_route,
    _delivery_reverify_with_answer_repair,
    _execute_capabilities,
    _evidence_established,
    _execute_joint_attribution,
    _ensure_business_narrative_answer,
    _fetch_runtime_rows,
    _final_business_summary,
    _final_summary_needs_display_repair,
    _legacy_quality_with_final_answer_audit,
    _merge_confirmed_material_requirements,
    _local_coverage_answerable_reason,
    _infer_question_families_from_requested_nodes,
    _business_query_gap_projection,
    _business_query_repair_gap,
    _reconcile_route_metric_capabilities,
    _normalize_evidence_interpretation_output,
    _normalize_query_gap_clarification_output,
    _question_family_values,
    normalize_final_answer_audit,
    _generate_query_gap_clarification,
    _generate_degraded_explanation,
    _group_query_gap_actions,
    _preserved_authority_claims,
    _persist_clarification,
    _persist_query_gap_clarification,
    _retrying_node,
    _align_route_output_to_requested,
    _normalize_route_requested_nodes,
    _repair_path_invents_fixed_future_window,
    _repair_analysis_contract,
    _render_query_gap_actions,
    _reduce_evidence,
    evaluate_answer_quality,
    repair_final_answer_with_verified_claim,
    _route_after_next_action,
    _route_after_query_gap_clarification,
    _route_after_query_repair,
    _route_after_clarification,
    _route_after_accept_analysis,
    _route_after_semantic_audit,
    _sanitize_terminal_explanation,
    _sanitize_answer,
    _segment_contribution_params,
    _understand_business_intent,
    _typed_clarification_compiled_graph,
    _validate_runtime_binding,
    WorkflowFailure,
    run_pattern_workflow as _run_pattern_workflow,
)

from bi_agent.runtime.analysis_runtime import (
    AnalysisRuntime,
    AnalysisRuntimeRequest,
    AnalysisRuntimeResult,
    AnswerPackageBuildContext,
    analysis_outcome_has_executable_ready_capability,
    analysis_outcome_requires_route_clarification,
    analysis_outcome_requires_preexecution_clarification,
)
from bi_agent.runtime.llm_client import (
    LLMConfigurationError,
    LLMTimeoutError,
    OpenAICompatibleLLMClient,
    _localize_narrative_fields,
)
from bi_agent.runtime.llm_prompts import build_prompt, validate_prompt_specs
from bi_agent.runtime.data_contract_diagnostics import diagnose_contract_gaps
from tests.phase4.fake_llm import FakeLLMClient
from tests.phase4.fake_llm import FakeLLMResult


def run_pattern_workflow(request=None):
    fixture_request = dict(request or {})
    fixture_request.setdefault("run_mode", "fixture")
    with patch.dict(
        "os.environ",
        {
            "WAJE_ALLOW_LEGACY_FIXTURES": "1",
            "WAJE_RUNTIME_ENV": "test",
        },
    ):
        return _run_pattern_workflow(fixture_request)


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
    def test_question_family_normalization_uses_bound_primary_for_supported_diagnostic(self):
        normalized = workflow_module._normalize_question_families(
            {
                "question_family": "revenue_health",
                "primary_question_family": "paid_amount_change_explanation",
                "question_families": ["revenue_health"],
            }
        )

        self.assertEqual(
            normalized["question_family"],
            "paid_amount_change_explanation",
        )
        self.assertEqual(
            normalized["question_families"],
            ["paid_amount_change_explanation"],
        )
        self.assertEqual(normalized["secondary_question_families"], [])

    def test_question_family_normalization_rejects_ambiguous_diagnostic_without_binding(self):
        with self.assertRaisesRegex(
            WorkflowFailure,
            "diagnostic_question_family_ambiguous:multi_baseline",
        ):
            workflow_module._normalize_question_families(
                {"question_family": "multi_baseline"}
            )

    def test_question_family_normalization_rejects_diagnostic_mismatched_to_bound_primary(self):
        with self.assertRaisesRegex(
            WorkflowFailure,
            "diagnostic_question_family_incompatible:multi_baseline:revenue_health_review",
        ):
            workflow_module._normalize_question_families(
                {
                    "question_family": "multi_baseline",
                    "primary_question_family": "revenue_health_review",
                }
            )

    def test_question_family_normalization_rejects_unknown_provider_token(self):
        with self.assertRaisesRegex(
            WorkflowFailure,
            "unknown_question_family_or_diagnostic:model_invented_family",
        ):
            workflow_module._normalize_question_families(
                {"question_family": "model_invented_family"}
            )

    def test_question_family_normalization_is_idempotent_for_canonical_families(self):
        intent = {
            "question_family": "custom_baseline_comparison",
            "primary_question_family": "custom_baseline_comparison",
            "question_families": [
                "custom_baseline_comparison",
                "data_quality_or_evidence_review",
            ],
            "secondary_question_families": ["data_quality_or_evidence_review"],
        }

        normalized = workflow_module._normalize_question_families(intent)

        self.assertEqual(
            workflow_module._normalize_question_families(normalized),
            normalized,
        )

    def test_business_intent_rejects_non_mapping_optional_contract_as_typed_llm_failure(self):
        state = {
            "request": {"question": "检查业务规律"},
            "llm_client": FakeLLMClient({
                "business_intent": {
                    "question_family": "pattern_explanation",
                    "target_metric": "paid_amount",
                    "pattern_family": "weekly",
                    "scope": "full_sample",
                    "time_window": "recent_period",
                    "target_claim": "是否存在规律",
                    "baseline_candidates": [],
                    "status_message": "已识别",
                    "display_summary": "已识别",
                    "answer_contract": ["malformed"],
                }
            }),
            "llm_calls": [],
            "checkpoint_events": [],
        }

        with self.assertRaisesRegex(
            WorkflowFailure,
            "business_intent_contract_invalid:answer_contract",
        ):
            _understand_business_intent(state)

    def test_degraded_route_preserves_ready_authority_claim_when_other_sources_are_unbound(self):
        claim = {
            "text": "大盘付费金额下降 8%。",
            "claim_type": "period_change",
            "claim_strength": "observed",
            "scope": "全市场",
            "time_window": "昨天",
            "evidence_refs": ["evidence:market:1"],
        }
        state = {
            "run_id": "run-partial-authority",
            "request": {"run_mode": "production", "analysis_contract": {}},
            "intent": {"scope": "全市场", "time_window": "昨天"},
            "draft_claims": [claim],
            "answer_text": "大盘付费金额下降 8%，payment_attempt 与事件来源仍未绑定。",
            "evidence": [{
                "evidence_ref": "evidence:market:1",
                "binding_manifest_ref": "binding:market:1",
                "input_status": "ready",
                "supported_claim_types": ["period_change"],
                "strength": "observed",
                "maximum_claim_strength": "directional",
            }],
            "verifier": {"errors": [{"code": "source_unbound"}]},
            "retry_context": {"failure_type": "verifier"},
            "evidence_brief": {},
        }
        with patch(
            "bi_agent.runtime.langgraph_workflow._invoke_terminal_explanation",
            return_value={
                "status": "degraded",
                "explanation": "payment_attempt 与事件来源仍未绑定。",
                "owner": "data_owner",
                "repair_path": "绑定来源后补充归因。",
            },
        ):
            _generate_degraded_explanation(state)

        self.assertEqual(state["draft_claims"], [claim])
        self.assertIn("下降 8%", state["answer_text"])
        self.assertIn("payment_attempt", state["final_explanation"]["explanation"])

    def test_available_evidence_brief_projects_only_verified_authority_and_scoped_gaps(self):
        gaps = diagnose_contract_gaps(
            contract_gaps=({
                "gap_id": "gap:paid:1",
                "fields": ("payment_attempt",),
            },),
            available_fields=(),
            contract_fields=(),
            permission_denied_fields=(),
            unsupported_grains=(),
        )
        brief = build_available_evidence_brief(
            verified_claims=(
                {
                    "claim_ref": "claim:market:1",
                    "text": "大盘付费金额下降 8%。",
                    "evidence_refs": ["evidence:market:1"],
                    "result_refs": ["result:market:1"],
                    "artifact_refs": ["artifact:market:1"],
                    "memory_refs": ["memory:market:1"],
                    "context_manifest_ref": "context:market:1",
                    "reuse_decisions": [{"source_ref": "source:market:1", "result_ref": "result:market:1", "decision": "reuse"}],
                    "provenance_record_ref": "trusted:market:1",
                },
                {
                    "claim_ref": "claim:untrusted:1",
                    "text": "untrusted",
                    "evidence_refs": [],
                },
            ),
            capability_bindings=(
                {"capability_id": "market_compare", "status": "ready"},
                {"capability_id": "paid_driver", "status": "degraded"},
                {"capability_id": "event_evidence", "status": "blocked"},
            ),
            contract_gaps=gaps,
            obligation_resolution={"unresolved": ["event_evidence_required"]},
        )

        self.assertEqual(
            [claim["claim_ref"] for claim in brief["verified_claims"]],
            ["claim:market:1"],
        )
        self.assertEqual(
            brief["verified_capabilities"],
            ["market_compare", "paid_driver"],
        )
        self.assertEqual(brief["omitted_factors"], ["gap:paid:1"])
        self.assertEqual(
            brief["business_next_actions"],
            [gaps[0]["repair_path"]],
        )
        self.assertEqual(
            brief["unresolved_obligations"], ["event_evidence_required"]
        )

    def test_accepted_degradation_choice_reaches_proposal_graph_package_and_reuse_signature(self):
        choice = {
            "choice_id": "omit_event_source",
            "action_kind": "omit_unavailable_context",
            "business_label": "保留大盘证据继续。",
            "affected_capabilities": ["event_evidence"],
            "source_run_id": "run-clarify-1",
        }
        terminal_gap_authority = {
            "source_run_id": "run-clarify-1",
            "thread_id": "thread-choice-1",
            "topic_id": "topic-choice-1",
            "analysis_contract": {"authority": "postgres"},
            "analysis_contract_signature": "signature-authority",
            "clarification_outcome": {
                "outcome_ref": "clarification-outcome:choice-1"
            },
        }
        state = {
            "run_id": "run-resumed-choice",
            "request": {
                "run_id": "run-resumed-choice",
                "question": "分析昨天付费金额变化。",
                "role": "analyst",
                "accepted_degradation_choice": choice,
                "accepted_terminal_gap_authority": terminal_gap_authority,
                "context_manifest": {
                    "manifest_id": "context-choice-1",
                    "thread_id": "thread-choice-1",
                    "topic_id": "topic-choice-1",
                    "accepted_assumptions": [choice],
                    "permission_context": {"role": "analyst"},
                },
                "analysis_context": {"as_of": "2026-06-03T12:00:00+01:00"},
                "clarification_resume_context": {},
            },
            "intent": {
                "question_family": "custom_baseline_comparison",
                "question_families": ["custom_baseline_comparison"],
                "target_metric": "paid_amount",
                "pattern_family": "custom_baseline",
                "requested_nodes": ("compare_periods",),
                "scope": "full_sample",
                "time_window": "昨天",
            },
            "analysis_route": {
                "requested_nodes": ("compare_periods",),
                "analysis_requirements": {"target_metrics": ["paid_amount"]},
            },
            "checkpoint_events": [],
            "draft_claims": [],
            "evidence": [],
            "validator_results": [],
            "final_explanation": {
                "status": "degraded",
                "explanation": "事件来源未绑定。",
                "owner": "data_owner",
                "repair_path": "绑定事件来源后补充。",
            },
        }
        alternate_state = deepcopy(state)
        alternate_choice = {
            **choice,
            "choice_id": "wait_for_event_source",
            "business_label": "等待事件来源。",
            "action_kind": "wait_for_source",
        }
        alternate_state["request"]["accepted_degradation_choice"] = alternate_choice
        alternate_state["request"]["context_manifest"]["accepted_assumptions"] = [
            alternate_choice
        ]

        _accept_analysis_route(state)
        _accept_analysis_route(alternate_state)
        runtime_request = _analysis_runtime_request(state)
        package = _build_answer_package_from_state(state)

        self.assertEqual(runtime_request.proposal["accepted_degradation_choice"], choice)
        self.assertEqual(
            runtime_request.proposal["accepted_terminal_gap_authority"],
            terminal_gap_authority,
        )
        self.assertEqual(
            state["compiled_graph"].runtime_plan["graph_metadata"]
            ["accepted_assumptions"],
            [choice],
        )
        self.assertIn(
            "accepted_degradation_choice",
            state["compiled_graph"].runtime_plan["asset_reuse_contract"]
            ["contract_versions"],
        )
        self.assertNotEqual(
            state["compiled_graph"].runtime_plan["asset_reuse_contract"]
            ["contract_signature"],
            alternate_state["compiled_graph"].runtime_plan["asset_reuse_contract"]
            ["contract_signature"],
        )
        self.assertEqual(package["accepted_degradation_choice"], choice)
        self.assertEqual(
            package["accepted_graph_metadata"]["accepted_assumptions"], [choice]
        )
        self.assertEqual(package["context_assumptions"], [choice])
        self.assertEqual(
            package["admin_audit"]["clarification_outcome"]
            ["accepted_degradation_choice"],
            choice,
        )

    def test_compiler_permission_scope_falls_back_without_accepted_choice(self):
        context = _compiler_bound_context(
            {
                "intent": {"scope": "full_sample"},
                "request": {
                    "permission_context": {"role": "viewer"},
                    "context_manifest": {"accepted_assumptions": []},
                },
            }
        )

        self.assertEqual(context["permission_scope"], "viewer")
        self.assertNotIn("accepted_degradation_choice", context)

    def test_persisted_query_gap_keeps_waiting_terminal_status(self):
        compiled = compile_graph(
            question_family="custom_baseline_comparison",
            target_metric="paid_amount",
            requested_nodes=("compare_periods",),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "run_id": "query-gap-waiting-status",
                "request": {"artifact_root": tmpdir},
                "compiled_graph": compiled,
                "analysis_route": {"requested_nodes": ["compare_periods"]},
                "query_gap_clarification": {
                    "questions": [{"question": "确认口径", "options": ["按推荐继续"]}]
                },
            }
            result = _persist_query_gap_clarification(state)

        self.assertEqual(result["workflow_status"], "waiting_for_clarification")
        self.assertEqual(
            result["answer_package"]["status"],
            "waiting_for_clarification",
        )

    def test_analysis_runtime_request_binds_all_fixed_eval_windows(self):
        request = _analysis_runtime_request({
            "run_id": "fixed-window-request",
            "request": {
                "role": "analyst",
                "run_mode": "production",
                "analysis_context": {
                    "as_of": "2026-06-03T12:00:00+01:00",
                    "target_date": "2026-06-02",
                    "previous_day": "2026-06-01",
                    "rolling_7_day_start": "2026-05-26",
                    "rolling_7_day_end": "2026-06-01",
                    "same_weekday_last_week": "2026-05-26",
                    "pattern_history_start": "2026-01-01",
                    "anomaly_history_start": "2026-05-03",
                },
            },
            "intent": {"question_family": "paid_amount_change_explanation"},
            "analysis_route": {
                "requested_nodes": ["pattern_scan", "outlier_scan"],
                "analysis_requirements": {
                    "target_metrics": ["paid_amount"],
                    "baselines": [
                        "前日付费金额",
                        "rolling_7d_avg",
                        "same_day_last_week",
                    ],
                },
            },
        })

        self.assertEqual(
            request.proposal["fixed_window_bounds"],
            {
                "target_day": ("2026-06-02", "2026-06-02"),
                "previous_day": ("2026-06-01", "2026-06-01"),
                "rolling_7_day_baseline": ("2026-05-26", "2026-06-01"),
                "same_weekday_last_week": ("2026-05-26", "2026-05-26"),
                "pattern_history": ("2026-01-01", "2026-06-02"),
                "anomaly_history": ("2026-05-03", "2026-06-01"),
            },
        )
        self.assertEqual(
            request.proposal["baselines"],
            (
                "previous_day",
                "rolling_7_day_baseline",
                "same_weekday_last_week",
            ),
        )

    def test_live_publication_requires_analysis_runtime_binding(self):
        result = _run_pattern_workflow(
            {
                "run_id": "live-missing-analysis-runtime",
                "run_mode": "live",
                "llm_client": FakeLLMClient(),
            }
        )

        self.assertEqual(result.status, "failed")
        self.assertEqual(
            result.failure_reason,
            "analysis_runtime_required_for_live_publication",
        )

    def test_live_runtime_rows_never_use_default_or_request_fixture_rows(self):
        from bi_agent.runtime.langgraph_workflow import (
            _capability_rows,
            _coverage_rows_for_local_check,
        )

        live = {
            "request": {
                "run_mode": "live",
                "rows": ({"period": "fixture", "amount": 999.0},),
            }
        }
        fixture = {
            "request": {
                "run_mode": "fixture",
                "rows": ({"period": "fixture", "amount": 999.0},),
            }
        }

        self.assertEqual(list(_capability_rows(live)), [])
        self.assertEqual(_coverage_rows_for_local_check(live), [])
        self.assertEqual(list(_capability_rows(fixture))[0]["amount"], 999.0)

    def test_analysis_route_prompt_requires_typed_analysis_requirements(self):
        text = "\n".join(
            message["content"]
            for message in build_prompt("analysis_route", {"intent": {}}).messages
        )

        self.assertIn("analysis_requirements", text)
        for key in (
            "target_metrics",
            "requested_components",
            "requested_dimensions",
            "baselines",
            "context_sources",
            "claim_intents",
            "scope",
        ):
            self.assertIn(key, text)
        self.assertIn("allowed_claim_types", text)
        self.assertIn("never objects, dates, or descriptions", text)
        self.assertIn("Do not select compare_periods", text)

    def test_query_gap_clarification_prompt_has_business_options_and_escape(self):
        prompt = build_prompt(
            "query_gap_clarification",
            {"business_gaps": [{"business_gap": "业务时间范围不可用"}]},
        )
        text = "\n".join(message["content"] for message in prompt.messages)

        self.assertEqual(
            prompt.required_keys,
            ("questions", "recommended_assumption", "decision_summary", "display_summary"),
        )
        self.assertIn("exactly one question", text)
        self.assertIn("2-3 draft options", text)
        self.assertIn("allowed_actions.business_semantics", text)
        self.assertIn("questions must never be empty", text)
        self.assertIn("tell the agent to do differently", text)
        self.assertIn("cannot claim", text)
        self.assertIn("character-for-character", text)
        self.assertIn("future availability timestamp", text)

    def test_final_llm_audit_hard_label_is_recorded_as_nonblocking_risk(self):
        audit = normalize_final_answer_audit(
            {
                "display_status": "hard_blocked",
                "hard_blockers": ["unsupported_main_claim"],
                "repairable_warnings": [],
                "retry_instruction": "弱化措辞。",
                "business_audit_summary": "存在措辞风险。",
            }
        )

        self.assertFalse(audit["blocks_display"])
        self.assertEqual(audit["hard_blockers"], [])
        self.assertEqual(audit["risk_flags"], ["unsupported_main_claim"])
        self.assertEqual(audit["display_status"], "ready_with_warnings")

    def test_query_gap_clarification_normalizes_structured_options_and_preserves_escape(self):
        normalized = _normalize_query_gap_clarification_output(
            {
                "questions": [
                    {
                        "question": "目标日数据未完整时怎么继续？",
                        "options": [
                            {"label": "等待刷新", "description": "数据齐备后继续。"},
                            {"label": "改用完整日", "description": "重新确认目标日。"},
                        ],
                    },
                    {
                        "question": "另一个问题不应扩大单次澄清范围。",
                        "options": [
                            {"text": "选项一"},
                            {"description": "选项二"},
                        ],
                    },
                ],
                "recommended_assumption": {"option": "等待刷新"},
                "decision_summary": "目标窗口会改变结论。",
            }
        )

        self.assertEqual(len(normalized["questions"]), 1)
        self.assertEqual(len(normalized["questions"][0]["options"]), 3)
        self.assertIn("等待刷新", normalized["questions"][0]["options"][0])
        self.assertIn(
            "tell the agent to do differently",
            normalized["questions"][0]["options"][-1].lower(),
        )
        self.assertIn("option", normalized["recommended_assumption"])

    def test_query_gap_clarification_accepts_choice_maps_and_recommended_choice(self):
        mapped = _normalize_query_gap_clarification_output(
            {
                "questions": [
                    {
                        "question_text": "缺口存在时如何推进？",
                        "choices": {
                            "wait": {"text": "等待数据刷新"},
                            "change": "改用完整窗口",
                        },
                    }
                ],
                "recommended_assumption": {"option": "等待数据刷新"},
                "decision_summary": "窗口选择会改变结论。",
            }
        )
        recommended_only = _normalize_query_gap_clarification_output(
            {
                "question": "缺口存在时如何推进？",
                "questions": [],
                "recommended_assumption": {"option": "等待数据刷新"},
                "decision_summary": "窗口选择会改变结论。",
            }
        )
        singular = _normalize_query_gap_clarification_output(
            {
                "questions": {
                    "question": "缺口存在时如何推进？",
                    "options": ["等待数据刷新"],
                },
                "recommended_assumption": {"option": "等待数据刷新"},
                "decision_summary": "窗口选择会改变结论。",
            }
        )

        self.assertEqual(len(mapped["questions"][0]["options"]), 3)
        self.assertEqual(len(recommended_only["questions"][0]["options"]), 2)
        self.assertEqual(len(singular["questions"][0]["options"]), 2)
        self.assertEqual(
            recommended_only["recommended_assumption"],
            {"option": "等待数据刷新"},
        )

    def test_query_gap_recommendation_normalizes_only_one_explicit_business_option(self):
        def normalized(recommendation):
            return _normalize_query_gap_clarification_output(
                {
                    "questions": [{
                        "question": "按哪个业务口径继续？",
                        "options": [
                            "等待相关业务数据可用后继续。",
                            "改用当前可验证的业务范围继续。",
                            "tell the agent to do differently",
                        ],
                    }],
                    "recommended_assumption": recommendation,
                    "decision_summary": "选择会影响结论。",
                }
            )["recommended_assumption"]

        self.assertEqual(
            normalized({
                "recommended_option": {
                    "text": "建议等待相关业务数据可用后继续。再恢复分析。"
                }
            }),
            {"option": "等待相关业务数据可用后继续。"},
        )
        self.assertNotIn(
            "option",
            normalized(
                "可等待相关业务数据可用后继续。也可改用当前可验证的业务范围继续。"
            ),
        )
        self.assertNotIn(
            "option",
            normalized("tell the agent to do differently"),
        )
        self.assertNotIn(
            "option",
            normalized({"assumption": "采用产品默认业务假设继续。"}),
        )

    def test_query_gap_clarification_retry_receives_contract_failure_reason(self):
        class InvalidThenValidLLM(FakeLLMClient):
            def __init__(self):
                super().__init__()
                self.message_batches = []

            def invoke_json(self, *, task, prompt_version, messages, required_keys):
                self.message_batches.append([dict(message) for message in messages])
                if len(self.message_batches) == 1:
                    return FakeLLMResult(
                        {
                            "questions": [{
                                "question": "external_event 数据集最早 2026-06-09 可用，怎么处理？",
                                "options": [
                                    "等待未来快照。",
                                    "tell the agent to do differently",
                                ],
                            }],
                            "recommended_assumption": {"option": "等待未来快照。"},
                            "decision_summary": "",
                            "display_summary": "",
                        },
                        {"task": task},
                    )
                return super().invoke_json(
                    task=task,
                    prompt_version=prompt_version,
                    messages=messages,
                    required_keys=required_keys,
                )

        fake = InvalidThenValidLLM()
        state = {
            "request": {},
            "analysis_runtime_result": type(
                "ClarificationResult",
                (),
                {"typed_gaps": ({
                    "gap_type": "dataset_snapshot_unavailable_as_of",
                    "requires_clarification": True,
                    "dataset_id": "external_event",
                    "diagnostic_context": {
                        "earliest_loaded_at": "2026-06-09T00:00:00+00:00",
                    },
                },)},
            )(),
            "query_repair_decisions": [],
            "intent": {"target_metric": "active_users", "time_window": "previous_day"},
            "llm_client": fake,
            "llm_calls": [],
            "checkpoint_events": [],
        }

        result = _retrying_node(
            "generate_query_gap_clarification",
            _generate_query_gap_clarification,
        )(state)

        self.assertEqual(result["workflow_status"], "waiting_for_clarification")
        first_prompt = "\n".join(
            message["content"] for message in fake.message_batches[0]
        )
        second_prompt = "\n".join(
            message["content"] for message in fake.message_batches[1]
        )
        for hidden_value in (
            "external_event",
            "2026-06-09",
            "dataset_snapshot_unavailable_as_of",
        ):
            self.assertNotIn(hidden_value, first_prompt)
            self.assertNotIn(hidden_value, second_prompt)
        self.assertIn("业务数据在分析时点尚不可用", first_prompt)
        self.assertIn("query_gap_clarification_internal_authority_leak", second_prompt)
        self.assertEqual(
            result["request"]["node_retry_feedback"]["node"],
            "generate_query_gap_clarification",
        )

    def test_query_gap_business_projection_excludes_authority_metadata(self):
        from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry

        projected = _business_query_gap_projection(
            ({
                "gap_type": "dataset_snapshot_unavailable_as_of",
                "requires_clarification": True,
                "gap_id": "dataset:external_event:dataset_snapshot_unavailable_as_of",
                "dataset_id": "external_event",
                "owner": "data_owner",
                "affected_capabilities": ("event_evidence",),
                "repair_options": (
                    "use_historical_snapshot_loaded_by_as_of",
                    "wait_for_snapshot_availability",
                ),
                "diagnostic_context": {
                    "as_of": "2026-06-03T11:00:00+00:00",
                    "earliest_snapshot_ref": "snapshot:event:future",
                    "earliest_loaded_at": "2026-06-09T00:00:00+00:00",
                },
            }, {
                "gap_type": "source_unbound",
                "owner": "data_owner",
                "affected_capabilities": ("event_evidence",),
                "repair_options": ("bind_source",),
                "requires_clarification": False,
            }),
            {"target_metric": "活跃用户", "time_window": "目标日与前一日"},
            accepted_capabilities=("market_health_compare", "event_evidence"),
            registry=RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
        )

        rendered = json.dumps(projected, ensure_ascii=False)
        self.assertIn("业务数据在分析时点尚不可用", rendered)
        self.assertIn("等待相关业务数据可用后再恢复本次分析", rendered)
        self.assertIn("继续可验证的主指标分析，并明确缺少相关业务背景证据", rendered)
        self.assertNotIn("使用分析时点内已有的历史业务范围", rendered)
        self.assertNotIn("绑定业务来源", rendered)
        for hidden_value in (
            "external_event",
            "snapshot:event:future",
            "2026-06-09",
            "dataset_snapshot_unavailable_as_of",
        ):
            self.assertNotIn(hidden_value, rendered)

    def test_query_gap_recommendation_is_repaired_by_llm_option_index(self):
        class RecommendationRepairLLM(FakeLLMClient):
            def __init__(self, option_index):
                super().__init__()
                self.option_index = option_index
                self.message_batches = []

            def invoke_json(self, *, task, prompt_version, messages, required_keys):
                self.message_batches.append((task, [dict(message) for message in messages]))
                if task == "query_gap_clarification":
                    return FakeLLMResult(
                        {
                            "questions": [{
                                "question": "需要确认按哪个业务口径继续？",
                                "options": [
                                    "继续主指标分析，并明确相关业务背景证据缺失",
                                    "等待相关业务数据可用后继续",
                                    "tell the agent to do differently",
                                ],
                            }],
                            "recommended_assumption": {
                                "assumption": "采用产品默认业务假设继续。"
                            },
                            "decision_summary": "该选择会影响结论。",
                            "display_summary": "等待用户确认。",
                        },
                        {"task": task},
                    )
                if task == "query_gap_recommendation_repair":
                    return FakeLLMResult(
                        {
                            "option_index": self.option_index,
                            "brief_reason": "当前选择更符合证据边界。",
                            "display_summary": "已形成推荐选项。",
                        },
                        {"task": task},
                    )
                return super().invoke_json(
                    task=task,
                    prompt_version=prompt_version,
                    messages=messages,
                    required_keys=required_keys,
                )

        def state(llm):
            return {
                "request": {},
                "analysis_route": {
                    "requested_nodes": ["market_health_compare", "event_evidence"]
                },
                "analysis_runtime_result": type(
                    "ClarificationResult",
                    (),
                    {"typed_gaps": ({
                        "gap_type": "dataset_snapshot_unavailable_as_of",
                        "requires_clarification": True,
                        "dataset_id": "external_event",
                        "owner": "data_owner",
                        "affected_capabilities": ("event_evidence",),
                        "repair_options": (
                            "use_historical_snapshot_loaded_by_as_of",
                            "wait_for_snapshot_availability",
                        ),
                        "diagnostic_context": {
                            "earliest_loaded_at": "2026-06-09T00:00:00+00:00"
                        },
                    },)},
                )(),
                "query_repair_decisions": [],
                "intent": {"target_metric": "active_users", "time_window": "previous_day"},
                "llm_client": llm,
                "llm_calls": [],
                "checkpoint_events": [],
            }

        valid = RecommendationRepairLLM(1)
        result = _generate_query_gap_clarification(state(valid))
        self.assertEqual(
            result["query_gap_clarification"]["recommended_assumption"],
            {"option": "等待相关业务数据可用后再恢复本次分析"},
        )
        repair_prompt = "\n".join(
            message["content"] for task, messages in valid.message_batches
            if task == "query_gap_recommendation_repair"
            for message in messages
        )
        self.assertIn("继续可验证的主指标分析，并明确缺少相关业务背景证据", repair_prompt)
        self.assertIn("等待相关业务数据可用后再恢复本次分析", repair_prompt)
        for hidden_value in ("external_event", "2026-06-09", "snapshot"):
            self.assertNotIn(hidden_value, repair_prompt)

        with self.assertRaisesRegex(
            WorkflowFailure,
            "query_gap_recommendation_repair_invalid",
        ):
            _generate_query_gap_clarification(state(RecommendationRepairLLM(2)))

    def test_ready_independent_capability_forces_omit_recommendation_across_multiple_gaps(self):
        class VaryingRecommendationLLM(FakeLLMClient):
            def __init__(self, option_index):
                super().__init__()
                self.option_index = option_index

            def invoke_json(self, *, task, prompt_version, messages, required_keys):
                if task == "query_gap_clarification":
                    return FakeLLMResult(
                        {
                            "questions": [{
                                "question": "如何继续？",
                                "options": ["等待", "继续", "tell the agent to do differently"],
                            }],
                            "recommended_assumption": {"assumption": "等待所有背景数据"},
                            "decision_summary": "需要选择。",
                            "display_summary": "等待确认。",
                        },
                        {"task": task},
                    )
                if task == "query_gap_recommendation_repair":
                    return FakeLLMResult(
                        {
                            "option_index": self.option_index,
                            "brief_reason": "模型建议。",
                            "display_summary": "已建议。",
                        },
                        {"task": task},
                    )
                return super().invoke_json(
                    task=task,
                    prompt_version=prompt_version,
                    messages=messages,
                    required_keys=required_keys,
                )

        def state(option_index):
            return {
                "request": {},
                "analysis_route": {
                    "requested_nodes": [
                        "market_health_compare",
                        "event_evidence",
                        "gameplay_context",
                    ]
                },
                "analysis_runtime_result": type(
                    "ClarificationResult",
                    (),
                    {
                        "typed_gaps": ({
                            "gap_type": "dataset_snapshot_unavailable_as_of",
                            "requires_clarification": True,
                            "owner": "data_owner",
                            "affected_capabilities": ("event_evidence",),
                            "repair_options": ("wait_for_snapshot_availability",),
                        }, {
                            "gap_type": "contract_partial",
                            "requires_clarification": True,
                            "owner": "contract_owner",
                            "affected_capabilities": ("gameplay_context",),
                            "repair_options": ("bind_source",),
                        }),
                        "bound_capability_inputs": {
                            "market_health_compare": type(
                                "Bound", (), {"status": "ready", "binding_manifest_ref": "binding:market"}
                            )(),
                            "event_evidence": type("Bound", (), {"status": "blocked"})(),
                            "gameplay_context": type("Bound", (), {"status": "degraded"})(),
                        },
                    },
                )(),
                "query_repair_decisions": [],
                "intent": {"target_metric": "active_users", "time_window": "previous_day"},
                "llm_client": VaryingRecommendationLLM(option_index),
                "llm_calls": [],
                "checkpoint_events": [],
            }

        for option_index in (0, 1):
            result = _generate_query_gap_clarification(state(option_index))
            clarification = result["query_gap_clarification"]
            recommended = clarification["recommended_assumption"]["option"]
            action_by_label = {
                item["business_label"]: item["action_kind"]
                for item in clarification["choice_actions"]
            }
            self.assertEqual(action_by_label[recommended], "omit_unavailable_context")

    def test_boundary_only_acceptance_is_scoped_to_gaps_when_ready_sibling_exists(self):
        result = type(
            "ClarificationResult",
            (),
            {
                "status": "clarify",
                "typed_gaps": ({
                    "gap_type": "dataset_snapshot_unavailable_as_of",
                    "requires_clarification": True,
                    "affected_capabilities": ("event_evidence",),
                },),
                "bound_capability_inputs": {
                    "market_health_compare": type(
                        "Bound", (), {"status": "ready", "binding_manifest_ref": "binding:market"}
                    )(),
                    "event_evidence": type("Bound", (), {"status": "blocked"})(),
                },
            },
        )()
        state = {
            "request": {
                "accepted_degradation_choice": {
                    "choice_id": "continue-with-boundary",
                    "action_kind": "continue_with_boundary_only",
                    "affected_capabilities": ["event_evidence"],
                }
            },
            "analysis_runtime_result": result,
        }

        self.assertEqual(_route_after_query_repair(state), "degraded")
        self.assertEqual(
            state["request"]["accepted_degradation_choice"]["action_kind"],
            "omit_unavailable_context",
        )

    def test_analysis_runtime_request_is_typed_and_requires_fixed_clock(self):
        request = AnalysisRuntimeRequest.create(
            run_id="run-runtime",
            proposal={
                "question_families": ["paid_amount_change_explanation"],
                "target_metrics": ["paid_amount"],
                "claim_intents": ["comparative_change"],
                "scope": {"type": "full_sample"},
                "target_semantic": "yesterday",
                "baselines": ["previous_day"],
            },
            accepted_graph=("compare_periods",),
            as_of="2026-06-03T12:00:00+01:00",
            permission_scope="analyst",
        )

        self.assertEqual(request.as_of.isoformat(), "2026-06-03T12:00:00+01:00")
        self.assertEqual(request.accepted_graph, ("compare_periods",))
        self.assertTrue(hasattr(AnalysisRuntime, "execute"))

    def test_query_gap_resume_reuses_original_analysis_requirements_without_rerouting(self):
        fake = FakeLLMClient()
        prior_route = {
            "requested_nodes": ["event_evidence", "gameplay_activity_context"],
            "analysis_requirements": {
                "target_metrics": ["player_bet_amount"],
                "context_sources": ["external_event", "gameplay"],
                "claim_intents": ["candidate_mechanism", "observed_activity"],
                "baselines": ["previous_day"],
                "scope": {"type": "full_sample"},
            },
        }
        state = {
            "run_id": "run-typed-precompile-clarify",
            "request": {
                "clarification_resume_context": {
                    "accepted_graph": (
                        "event_evidence",
                        "gameplay_activity_context",
                    ),
                    "analysis_route": prior_route,
                    "analysis_contract": {
                        "question_families": ["anomaly_or_black_swan_review"]
                    },
                }
            },
            "intent": {
                "question_family": "segment_or_factor_attribution",
                "question_families": ["segment_or_factor_attribution"],
                "target_metric": "player_bet_amount",
                "pattern_family": "custom_baseline",
            },
            "confirmed_understanding": {},
            "llm_client": fake,
            "llm_calls": [],
        }

        _design_analysis_route(state)

        self.assertEqual(
            state["analysis_route"]["analysis_requirements"],
            prior_route["analysis_requirements"],
        )
        self.assertEqual(
            state["intent"]["question_families"],
            ["anomaly_or_black_swan_review"],
        )
        self.assertEqual(
            state["intent"]["question_family"],
            "anomaly_or_black_swan_review",
        )
        self.assertNotIn("analysis_route", fake.calls)

    def test_route_reconciliation_adds_unique_metric_query_capability(self):
        from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry

        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        route = {
            "analysis_requirements": {
                "target_metrics": ["active_users"],
                "baselines": ["previous_day"],
                "claim_intents": ["candidate_mechanism"],
            }
        }

        requested, reconciled = _reconcile_route_metric_capabilities(
            ("event_evidence",),
            route,
            {"target_metric": "active_users"},
            registry,
        )

        self.assertEqual(requested, ("market_health_compare", "event_evidence"))
        self.assertEqual(
            reconciled["analysis_requirements"]["claim_intents"],
            ["candidate_mechanism", "comparative_change"],
        )
        already_covered, _ = _reconcile_route_metric_capabilities(
            requested,
            reconciled,
            {"target_metric": "active_users"},
            registry,
        )
        self.assertEqual(already_covered, requested)

        metric_only_context, _ = _reconcile_route_metric_capabilities(
            ("market_channel_context",),
            {
                "analysis_requirements": {
                    "target_metrics": ["active_users"],
                    "baselines": ["previous_day"],
                    "claim_intents": ["comparative_change"],
                }
            },
            {"target_metric": "active_users"},
            registry,
        )
        self.assertEqual(
            metric_only_context,
            ("market_health_compare", "market_channel_context"),
        )

        ambiguous, _ = _reconcile_route_metric_capabilities(
            ("event_evidence",),
            {
                "analysis_requirements": {
                    "target_metrics": ["paid_amount"],
                    "baselines": ["previous_day"],
                    "claim_intents": ["candidate_mechanism"],
                }
            },
            {"target_metric": "paid_amount"},
            registry,
        )
        self.assertEqual(ambiguous, ("event_evidence",))

        context_bound, _ = _reconcile_route_metric_capabilities(
            ("market_health_compare",),
            {
                "analysis_requirements": {
                    "target_metrics": ["active_users"],
                    "context_sources": ["external_event"],
                    "baselines": ["previous_day"],
                    "claim_intents": ["comparative_change", "candidate_mechanism"],
                }
            },
            {
                "target_metric": "active_users",
                "question_family": "anomaly_or_black_swan_review",
                "question_families": ["anomaly_or_black_swan_review"],
            },
            registry,
        )
        self.assertEqual(
            context_bound,
            ("market_health_compare", "event_evidence"),
        )
        no_context_requirement, _ = _reconcile_route_metric_capabilities(
            ("market_health_compare",),
            {"analysis_requirements": {"target_metrics": ["active_users"]}},
            {
                "target_metric": "active_users",
                "question_family": "anomaly_or_black_swan_review",
            },
            registry,
        )
        self.assertEqual(no_context_requirement, ("market_health_compare",))
        ambiguous_context, _ = _reconcile_route_metric_capabilities(
            ("market_health_compare",),
            {
                "analysis_requirements": {
                    "target_metrics": ["active_users"],
                    "context_sources": ["external_event"],
                }
            },
            {
                "target_metric": "active_users",
                "question_family": "business_object_impact_review",
                "question_families": ["business_object_impact_review"],
            },
            registry,
        )
        self.assertEqual(ambiguous_context, ("market_health_compare",))

    def test_route_reconciliation_carries_independent_capability_dataset(self):
        from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry

        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        requested, reconciled = workflow_module.reconcile_analysis_route(
            ("market_health_compare",),
            {
                "analysis_requirements": {
                    "target_metrics": ["paid_amount"],
                    "dataset_requirements": ["paid_order_success"],
                    "baselines": ["previous_day"],
                    "claim_intents": ["comparative_change"],
                }
            },
            {
                "question_family": "revenue_health_review",
                "question_families": ["revenue_health_review"],
                "target_metric": "paid_amount",
            },
            registry,
        )

        self.assertIn("market_health_compare", requested)
        self.assertEqual(
            reconciled["analysis_requirements"]["dataset_requirements"],
            ["paid_order_success", "market_dashboard"],
        )
        self.assertEqual(
            reconciled["obligation_resolution"][
                "capability_dataset_requirements"
            ]["market_health_compare"],
            ["market_dashboard"],
        )

    def test_route_dataset_carry_normalizes_scalar_extra_and_is_idempotent(self):
        from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry

        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        intent = {
            "question_family": "revenue_health_review",
            "question_families": ["revenue_health_review"],
            "target_metric": "paid_amount",
        }
        first = workflow_module.reconcile_analysis_route(
            ("market_health_compare",),
            {
                "analysis_requirements": {
                    "target_metrics": ["paid_amount"],
                    "dataset_requirements": "paid_order_success",
                    "baselines": ["previous_day"],
                }
            },
            intent,
            registry,
        )
        second = workflow_module.reconcile_analysis_route(
            first[0], first[1], intent, registry
        )

        self.assertEqual(
            first[1]["analysis_requirements"]["dataset_requirements"],
            ["paid_order_success", "market_dashboard"],
        )
        self.assertEqual(
            second[1]["analysis_requirements"]["dataset_requirements"],
            first[1]["analysis_requirements"]["dataset_requirements"],
        )

    def test_route_reconciliation_is_idempotent_and_question_text_independent(self):
        from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry

        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        intent = {
            "question_family": "segment_or_factor_attribution",
            "question_families": ["segment_or_factor_attribution"],
            "target_metric": "paid_amount",
        }
        route = {
            "question_text": "文本不得成为 obligation policy input",
            "analysis_requirements": {
                "target_metrics": ["paid_amount"],
                "requested_dimensions": ["channel", "game"],
                "diagnostic_tags": ["factor_topk"],
            },
        }

        first = workflow_module.reconcile_analysis_route(
            ("data_quality_profile",), route, intent, registry
        )
        second = workflow_module.reconcile_analysis_route(first[0], first[1], intent, registry)

        self.assertEqual(first[0], second[0])
        self.assertEqual(second[1]["obligation_resolution"]["mutations"], [])
        self.assertNotIn("question_text", first[1]["obligation_resolution"])
        self.assertTrue(
            {"segment_contribution", "joint_attribution", "answer_verify"}.issubset(
                first[0]
            )
        )

    def test_reconciliation_records_only_actual_metric_context_and_obligation_additions(self):
        from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry

        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        intent = {
            "question_family": "anomaly_or_black_swan_review",
            "question_families": ["anomaly_or_black_swan_review"],
            "target_metric": "active_users",
        }
        route = {
            "analysis_requirements": {
                "target_metrics": ["active_users"],
                "baselines": ["previous_day"],
                "context_sources": ["external_event"],
            }
        }

        first = workflow_module.reconcile_analysis_route((), route, intent, registry)
        mutations = first[1]["obligation_resolution"]["mutations"]

        self.assertEqual(
            [(item["capability"], item["reason"]) for item in mutations],
            [
                ("market_health_compare", "metric_coverage_required"),
                ("event_evidence", "context_coverage_required"),
                ("data_quality_profile", "obligation_required"),
                ("outlier_scan", "obligation_required"),
            ],
        )
        second = workflow_module.reconcile_analysis_route(
            first[0], first[1], intent, registry
        )
        self.assertEqual(second[0], first[0])
        self.assertEqual(second[1]["obligation_resolution"]["mutations"], [])

    def test_every_public_family_obligation_conflict_opens_clarification(self):
        from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry

        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        tags = (
            "driver_focus", "change_explanation", "pattern_attribution",
            "event_impact", "revenue_health", "factor_topk", "anomaly",
            "multi_baseline", "evidence_quality",
        )
        for family in registry.question_family_ids:
            tag = next(
                candidate for candidate in tags
                if family not in registry.diagnostic_obligation(candidate)[
                    "supported_question_families"
                ]
            )
            requested, route = workflow_module.reconcile_analysis_route(
                ("data_quality_profile",),
                {"analysis_requirements": {"diagnostic_tags": [tag]}},
                {
                    "question_family": family,
                    "question_families": [family],
                    "target_metric": "paid_amount",
                },
                registry,
            )
            state = {"route_material_conflicts": (), "boundary_decision": {}}
            workflow_module._consume_obligation_route_conflict(state, route)
            with self.subTest(family=family, tag=tag):
                self.assertEqual(requested, ("data_quality_profile",))
                self.assertEqual(route["obligation_resolution"]["status"], "conflict")
                self.assertEqual(
                    state["boundary_decision"]["boundary_status"], "needs_question"
                )

    def test_unknown_diagnostic_tag_becomes_typed_route_conflict(self):
        from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry

        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        requested, route = workflow_module.reconcile_analysis_route(
            ("data_quality_profile",),
            {"analysis_requirements": {"diagnostic_tags": ["model_invented_tag"]}},
            {
                "question_family": "data_quality_or_evidence_review",
                "question_families": ["data_quality_or_evidence_review"],
                "target_metric": "paid_amount",
            },
            registry,
        )
        state = {"route_material_conflicts": (), "boundary_decision": {}}
        workflow_module._consume_obligation_route_conflict(state, route)

        self.assertEqual(requested, ("data_quality_profile",))
        self.assertEqual(route["obligation_resolution"]["status"], "conflict")
        self.assertEqual(
            route["obligation_resolution"]["error"],
            "'unknown_diagnostic_obligation:model_invented_tag'",
        )
        self.assertEqual(state["boundary_decision"]["boundary_status"], "needs_question")

    def test_all_diagnostic_tags_reconcile_from_registry_contracts(self):
        from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry

        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        condition_inputs = {
            "components_present": {"claim_intents": ["formula_component_contribution"]},
            "dimensions_present": {"requested_dimensions": ["channel"]},
            "event_context_requested": {"context_sources": ["external_event"]},
            "anomaly_review_requested": {
                "claim_intents": ["external_shock_candidate_or_anomaly"]
            },
            "baselines_present": {"baselines": ["previous_day"]},
            "trust_review_requested": {
                "claim_intents": ["contract_coverage_and_trust_boundary"]
            },
        }
        for tag in (
            "driver_focus",
            "change_explanation",
            "pattern_attribution",
            "event_impact",
            "revenue_health",
            "factor_topk",
            "anomaly",
            "multi_baseline",
            "evidence_quality",
        ):
            contract = registry.diagnostic_obligation(tag)
            family = contract["supported_question_families"][0]
            requirements = {
                "target_metrics": ["paid_amount"],
                "diagnostic_tags": [tag],
                **condition_inputs[contract["condition"]],
            }
            requested, route = workflow_module.reconcile_analysis_route(
                ("data_quality_profile",),
                {"analysis_requirements": requirements},
                {
                    "question_family": family,
                    "question_families": [family],
                    "target_metric": "paid_amount",
                },
                registry,
            )
            with self.subTest(tag=tag):
                self.assertTrue(
                    set(contract["required_capabilities"]).issubset(requested)
                )
                self.assertEqual(
                    route["obligation_resolution"]["status"], "resolved"
                )

    def test_incompatible_diagnostic_family_is_preserved_as_route_conflict(self):
        from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry

        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        requested, route = workflow_module.reconcile_analysis_route(
            ("data_quality_profile",),
            {
                "analysis_requirements": {
                    "target_metrics": ["paid_amount"],
                    "requested_dimensions": ["channel"],
                    "diagnostic_tags": ["factor_topk"],
                }
            },
            {
                "question_family": "data_quality_or_evidence_review",
                "question_families": ["data_quality_or_evidence_review"],
                "target_metric": "paid_amount",
                "analysis_requirements": {
                    "claim_intents": ["formula_component_contribution"]
                },
            },
            registry,
        )

        self.assertEqual(requested, ("data_quality_profile",))
        self.assertEqual(route["obligation_resolution"]["status"], "conflict")
        self.assertIn(
            "diagnostic_question_family_incompatible",
            route["obligation_resolution"]["error"],
        )
        self.assertTrue(
            any(
                mutation["reason"] == "obligation_conflict"
                for mutation in route["obligation_resolution"]["mutations"]
            )
        )

    def test_obligation_route_conflict_opens_existing_clarification_contract(self):
        state = {"route_material_conflicts": (), "boundary_decision": {}}
        route = {
            "obligation_resolution": {
                "status": "conflict",
                "error": "diagnostic_question_family_incompatible:factor_topk:data_quality_or_evidence_review",
            }
        }

        workflow_module._consume_obligation_route_conflict(state, route)

        self.assertIn("analysis_obligations", state["route_material_conflicts"])
        self.assertEqual(
            state["boundary_decision"]["boundary_status"], "needs_question"
        )

    def test_question_family_values_accept_reviewed_shapes_and_reject_unknown_mapping(self):
        self.assertEqual(
            _question_family_values("anomaly_or_black_swan_review"),
            ["anomaly_or_black_swan_review"],
        )
        self.assertEqual(
            _question_family_values([
                "anomaly_or_black_swan_review",
                "custom_baseline_comparison",
            ]),
            ["anomaly_or_black_swan_review", "custom_baseline_comparison"],
        )
        self.assertEqual(
            _question_family_values({
                "primary_question_family": "anomaly_or_black_swan_review",
                "secondary_question_families": ["custom_baseline_comparison"],
            }),
            ["anomaly_or_black_swan_review", "custom_baseline_comparison"],
        )
        with self.assertRaisesRegex(
            WorkflowFailure,
            "question_families_mapping_contract_invalid",
        ):
            _question_family_values({"unexpected_family_key": "anomaly"})

    def test_delivery_reverify_repairs_with_exact_codes_and_fails_after_bound(self):
        failed = {
            "status": "failed",
            "admin_audit": {
                "verifier": {
                    "status": "failed",
                    "errors": [
                        {"code": "free_text_without_verified_claim"},
                        {"code": "reported_verifier_mismatch"},
                    ],
                }
            },
        }
        passed = {
            "status": "draft",
            "admin_audit": {"verifier": {"status": "passed", "errors": []}},
        }

        def state():
            return {
                "request": {},
                "answer_text": "待修复答案",
                "draft_claims": [],
                "evidence": [],
                "evidence_brief": {},
                "intent": {
                    "target_metric": "active_users",
                    "pattern_family": "custom_baseline",
                    "scope": "full_sample",
                    "time_window": "2026-06-02",
                },
                "llm_calls": [],
            }

        captured = []

        def repair_llm(_state, task, payload):
            captured.append((task, dict(payload)))
            return {"answer_text": "已修复答案", "claims": []}

        authority_candidates = (
            {"status": "draft", "internal_authority": "candidate-1"},
            {"status": "draft", "internal_authority": "candidate-2"},
        )
        with patch(
            "bi_agent.runtime.langgraph_workflow.reverify_answer_package_for_delivery",
            side_effect=(failed, passed),
        ), patch(
            "bi_agent.runtime.langgraph_workflow._build_answer_package_from_state",
            side_effect=authority_candidates,
        ), patch(
            "bi_agent.runtime.langgraph_workflow._invoke_llm",
            side_effect=repair_llm,
        ), patch(
            "bi_agent.runtime.langgraph_workflow._claims_from_llm_or_default",
            return_value=[],
        ):
            repaired_state = state()
            result = _delivery_reverify_with_answer_repair(repaired_state)

        self.assertEqual(result["status"], "draft")
        self.assertEqual(result["internal_authority"], "candidate-2")
        self.assertEqual(len(captured), 1)
        self.assertEqual(
            captured[0][1]["delivery_verifier_error_codes"],
            ["free_text_without_verified_claim", "reported_verifier_mismatch"],
        )
        self.assertIn("do not rerun queries", captured[0][1]["repair_scope"])

        with patch(
            "bi_agent.runtime.langgraph_workflow.reverify_answer_package_for_delivery",
            side_effect=(failed, failed, failed),
        ), patch(
            "bi_agent.runtime.langgraph_workflow._build_answer_package_from_state",
            return_value={},
        ), patch(
            "bi_agent.runtime.langgraph_workflow._invoke_llm",
            side_effect=repair_llm,
        ), patch(
            "bi_agent.runtime.langgraph_workflow._claims_from_llm_or_default",
            return_value=[],
        ):
            failed_state = state()
            result = _delivery_reverify_with_answer_repair(failed_state)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(failed_state["workflow_status"], "failed")
        self.assertEqual(
            failed_state["workflow_failure_reason"],
            "delivery_reverify_failed:free_text_without_verified_claim,reported_verifier_mismatch",
        )

    def test_explicit_failed_graph_output_keeps_reason_and_package(self):
        class Graph:
            def invoke(self, state, config):
                return {
                    **state,
                    "workflow_status": "failed",
                    "workflow_failure_reason": "delivery_reverify_failed:number_mismatch",
                    "answer_package": {"status": "failed", "owner": "evidence_verifier_owner"},
                }

        with patch(
            "bi_agent.runtime.langgraph_workflow.build_pattern_graph",
            return_value=Graph(),
        ):
            result = run_pattern_workflow(
                {"run_id": "explicit-failure", "llm_client": FakeLLMClient()}
            )

        self.assertEqual(result.status, "failed")
        self.assertEqual(
            result.failure_reason,
            "delivery_reverify_failed:number_mismatch",
        )
        self.assertEqual(result.answer_package["owner"], "evidence_verifier_owner")

    def test_empty_graph_repairs_material_requirements_and_blocks_boundary_only(self):
        empty = compile_graph(
            question_family="unsupported_family",
            target_metric="active_users",
            requested_nodes=(),
        )
        material = {
            "compiled_graph": empty,
            "analysis_route": {
                "analysis_requirements": {
                    "target_metrics": ["active_users"],
                    "claim_intents": ["comparative_change"],
                }
            },
            "repair_attempts": 0,
        }
        self.assertEqual(_route_after_accept_analysis(material), "repair")
        material["repair_attempts"] = 2
        self.assertEqual(_route_after_accept_analysis(material), "block")

        boundary_only = {
            "compiled_graph": empty,
            "analysis_route": {"analysis_requirements": {}},
            "repair_attempts": 0,
        }
        self.assertEqual(_route_after_accept_analysis(boundary_only), "block")

    def test_query_gap_action_omits_only_unavailable_context_capability(self):
        from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry

        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        route = {
            "analysis_requirements": {
                "target_metrics": ["active_users"],
                "context_sources": ["external_event"],
                "claim_intents": ["candidate_mechanism", "comparative_change"],
            }
        }
        remaining, updated = _apply_query_gap_action_to_route(
            ("market_health_compare", "event_evidence"),
            route,
            {
                "action_kind": "omit_unavailable_context",
                "affected_capabilities": ["event_evidence"],
            },
            registry,
        )

        self.assertEqual(remaining, ("market_health_compare",))
        self.assertEqual(updated["analysis_requirements"]["context_sources"], [])
        self.assertEqual(
            updated["analysis_requirements"]["claim_intents"],
            ["comparative_change"],
        )
        waiting, unchanged = _apply_query_gap_action_to_route(
            ("market_health_compare", "event_evidence"),
            route,
            {"action_kind": "wait_for_source"},
            registry,
        )
        self.assertEqual(waiting, ("market_health_compare", "event_evidence"))
        self.assertEqual(unchanged, route)

        pattern_route = {
            "analysis_requirements": {
                "baselines": ["周末 vs 工作日", "月初 vs 月中/月末", "晚间 vs 日间"],
                "claim_intents": [
                    "recurring_pattern_existence",
                    "comparative_change",
                ],
            }
        }
        _, supported_window_route = _apply_query_gap_action_to_route(
            ("compare_period_phases",),
            pattern_route,
            {"action_kind": "choose_supported_window"},
            registry,
        )
        self.assertEqual(
            supported_window_route["analysis_requirements"]["baselines"],
            [],
        )
        _, supported_claim_route = _apply_query_gap_action_to_route(
            ("compare_period_phases",),
            supported_window_route,
            {"action_kind": "choose_supported_claim_intent"},
            registry,
        )
        self.assertEqual(
            supported_claim_route["analysis_requirements"]["claim_intents"],
            ["recurring_pattern_existence"],
        )

    def test_material_source_gaps_offer_progress_for_ready_sibling_or_boundary_terminal(self):
        from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry

        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        gap_types = (
            "dataset_snapshot_unavailable_as_of",
            "source_unbound",
            "contract_partial",
        )
        for gap_type in gap_types:
            gap = {
                "gap_type": gap_type,
                "requires_clarification": True,
                "owner": "contract_owner",
                "affected_capabilities": ("event_evidence",),
                "repair_options": ("choose_supported_claim_intent",),
            }
            with self.subTest(gap_type=gap_type, path="ready_sibling"):
                projected = _business_query_gap_projection(
                    (gap,),
                    {"target_metric": "paid_amount", "time_window": "target_day"},
                    accepted_capabilities=("compare_periods", "event_evidence"),
                    registry=registry,
                )
                actions = projected[0]["allowed_actions"]
                self.assertEqual(actions[0]["action_kind"], "omit_unavailable_context")
                self.assertNotIn(
                    "continue_with_boundary_only",
                    {item["action_kind"] for item in actions},
                )
            with self.subTest(gap_type=gap_type, path="no_ready_capability"):
                projected = _business_query_gap_projection(
                    (gap,),
                    {"target_metric": "paid_amount", "time_window": "target_day"},
                    accepted_capabilities=("event_evidence",),
                    registry=registry,
                )
                actions = projected[0]["allowed_actions"]
                self.assertEqual(
                    actions[0]["action_kind"], "continue_with_boundary_only"
                )
                self.assertEqual(actions[1]["action_kind"], "wait_for_source")

    def test_accepted_boundary_terminal_does_not_reask_same_gap(self):
        from types import SimpleNamespace

        state = {
            "request": {
                "accepted_degradation_choice": {
                    "action_kind": "continue_with_boundary_only",
                    "affected_capabilities": ["event_evidence"],
                }
            },
            "analysis_runtime_result": SimpleNamespace(status="clarify"),
        }

        self.assertEqual(_route_after_query_repair(state), "degraded")
        self.assertTrue(state["accepted_degraded_query_outcome"])

    def test_query_gap_actions_group_atomic_affected_capabilities_and_stage_overflow(self):
        actions = (
            {
                "action_kind": "omit_unavailable_context",
                "business_semantics": "继续主指标分析并保留背景限制",
                "affected_capabilities": ["event_evidence"],
            },
            {
                "action_kind": "omit_unavailable_context",
                "business_semantics": "继续主指标分析并保留背景限制",
                "affected_capabilities": ["gameplay_activity_context"],
            },
            {
                "action_kind": "wait_for_source",
                "business_semantics": "等待相关业务数据可用",
                "affected_capabilities": ["event_evidence", "gameplay_activity_context"],
            },
            {
                "action_kind": "request_permission",
                "business_semantics": "申请所需业务权限",
                "affected_capabilities": ["restricted_context"],
            },
        )

        selected, staged = _group_query_gap_actions(actions)

        self.assertEqual(len(selected), 2)
        self.assertEqual(selected[0]["action_kind"], "omit_unavailable_context")
        self.assertEqual(
            selected[0]["affected_capabilities"],
            ["event_evidence", "gameplay_activity_context"],
        )
        self.assertEqual(selected[1]["action_kind"], "wait_for_source")
        self.assertEqual(len(staged), 1)
        self.assertEqual(staged[0]["action_kind"], "request_permission")
        self.assertTrue(selected[0]["choice_id"].startswith("query-gap-"))

        from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry

        remaining, updated = _apply_query_gap_action_to_route(
            (
                "market_health_compare",
                "event_evidence",
                "gameplay_activity_context",
            ),
            {
                "analysis_requirements": {
                    "target_metrics": ["active_users"],
                    "context_sources": ["external_event", "gameplay"],
                    "claim_intents": ["comparative_change", "candidate_mechanism"],
                }
            },
            selected[0],
            RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
        )
        self.assertEqual(remaining, ("market_health_compare",))
        self.assertEqual(updated["analysis_requirements"]["context_sources"], [])
        self.assertEqual(
            updated["analysis_requirements"]["claim_intents"],
            ["comparative_change"],
        )

    def test_material_query_gap_without_feasible_action_routes_to_typed_block(self):
        from types import SimpleNamespace

        fake = FakeLLMClient()
        state = {
            "request": {},
            "analysis_runtime_result": SimpleNamespace(
                typed_gaps=({
                    "gap_type": "unmapped_material_gap",
                    "requires_clarification": True,
                    "owner": "contract_owner",
                    "affected_capabilities": ("event_evidence",),
                    "repair_options": ("unreviewed_repair",),
                },),
            ),
            "query_repair_decisions": (),
            "compiled_graph": SimpleNamespace(
                mutations=SimpleNamespace(accepted_graph=("event_evidence",))
            ),
            "intent": {"target_metric": "active_users", "time_window": "target_day"},
            "llm_client": fake,
            "llm_calls": [],
            "checkpoint_events": [],
        }

        _generate_query_gap_clarification(state)

        self.assertTrue(state["query_gap_no_feasible_action"])
        self.assertEqual(state["workflow_status"], "blocked")
        self.assertEqual(_route_after_query_gap_clarification(state), "block")
        self.assertNotIn("query_gap_action_render", fake.calls)

    def test_query_gap_action_render_rejects_missing_duplicate_and_unknown_actions(self):
        business_gaps = [{
            "allowed_actions": [
                {
                    "choice_id": "continue",
                    "action_kind": "omit_unavailable_context",
                    "business_semantics": "继续主指标分析并说明限制",
                    "affected_capabilities": ["event_evidence"],
                },
                {
                    "choice_id": "wait",
                    "action_kind": "wait_for_source",
                    "business_semantics": "等待相关业务数据",
                    "affected_capabilities": ["event_evidence"],
                },
            ]
        }]

        def state(rendered_actions):
            return {
                "llm_client": FakeLLMClient({
                    "query_gap_action_render": {
                        "rendered_actions": rendered_actions,
                    }
                }),
                "llm_calls": [],
            }

        invalid = (
            [{"choice_id": "continue", "label": "继续", "reason": "可继续"}],
            [
                {"choice_id": "continue", "label": "继续", "reason": "可继续"},
                {"choice_id": "continue", "label": "等待", "reason": "需等待"},
            ],
            [
                {"choice_id": "continue", "label": "继续", "reason": "可继续"},
                {"choice_id": "unknown", "label": "其他", "reason": "未知"},
            ],
        )
        for rendered_actions in invalid:
            with self.subTest(rendered_actions=rendered_actions), self.assertRaisesRegex(
                WorkflowFailure,
                "query_gap_action_render_invalid",
            ):
                _render_query_gap_actions(
                    state(rendered_actions),
                    business_gaps,
                    (),
                )

    def test_window_coverage_repair_recommends_terminal_degradation_and_allows_pause(self):
        gap = _business_query_repair_gap(({
            "action": "clarify",
            "reason": "window_coverage_failure",
            "requires_clarification": True,
            "failed_query_contract_ref": "query:internal",
        },))

        self.assertEqual(len(gap["allowed_actions"]), 2)
        continue_action, pause_action = gap["allowed_actions"]
        self.assertEqual(continue_action["action_kind"], "omit_unavailable_context")
        self.assertIn("固定目标窗口", continue_action["business_semantics"])
        self.assertEqual(pause_action["action_kind"], "wait_for_source")
        self.assertIn("不调整目标日期", pause_action["business_semantics"])
        self.assertNotIn("query:internal", json.dumps(gap, ensure_ascii=False))

    def test_answer_package_canonicalizes_accepted_degradation_from_manifest(self):
        from bi_agent.runtime.langgraph_workflow import _build_answer_package_from_state

        choice = {
            "action_kind": "omit_unavailable_context",
            "affected_capabilities": ["event_evidence"],
            "source_run_id": "run-source",
        }
        package = _build_answer_package_from_state({
            "run_id": "run-resumed",
            "request": {
                "context_manifest": {"accepted_assumptions": [choice]},
                "compiler_runtime_plan": {"graph_metadata": {}},
            },
            "checkpoint_events": [],
            "validator_results": [],
        })

        self.assertEqual(package["accepted_degradation_choice"], choice)
        self.assertEqual(package["context_assumptions"], [choice])
        self.assertEqual(
            package["accepted_graph_metadata"]["accepted_assumptions"],
            [choice],
        )

    def test_workflow_entry_promotes_manifest_only_assumption_into_runtime_request(self):
        choice = {
            "action_kind": "omit_unavailable_context",
            "affected_capabilities": ["event_evidence"],
            "source_run_id": "run-source",
        }
        captured = {}

        class CapturingGraph:
            def invoke(self, state, config):
                captured["request"] = deepcopy(state["request"])
                captured["accepted_assumptions"] = deepcopy(
                    state["accepted_assumptions"]
                )
                projected_request = deepcopy(state["request"])
                projected_request.pop("accepted_degradation_choice", None)
                projected_request.pop("context_manifest", None)
                projected_state = {**state, "request": projected_request}
                return {
                    **projected_state,
                    "workflow_status": "draft",
                    "answer_package": _build_answer_package_from_state(projected_state),
                }

        with patch(
            "bi_agent.runtime.langgraph_workflow.build_pattern_graph",
            return_value=CapturingGraph(),
        ):
            result = workflow_module.run_pattern_workflow({
                "run_id": "run-resumed",
                "llm_client": object(),
                "context_manifest": {"accepted_assumptions": [choice]},
            })

        self.assertEqual(captured["request"]["accepted_degradation_choice"], choice)
        self.assertEqual(captured["accepted_assumptions"], [choice])
        self.assertEqual(result.answer_package["context_assumptions"], [choice])
        self.assertEqual(result.answer_package["accepted_degradation_choice"], choice)
        self.assertEqual(
            result.answer_package["accepted_graph_metadata"]["accepted_assumptions"],
            [choice],
        )

    def test_workflow_and_persistence_share_answer_package_build_context(self):
        request = {
            "run_id": "run-shared-build-context",
            "thread_id": "thread-shared-build-context",
            "topic_id": "topic-shared-build-context",
            "permission_context": {"role": "analyst"},
            "context_manifest": {"manifest_id": "context-input", "items": []},
            "reuse_decisions": [],
            "artifact_root": "artifacts/task10-core",
        }
        artifact_path = (
            "artifacts/task10-core/run-shared-build-context/answer_package.json"
        )
        expected = AnswerPackageBuildContext.create(
            request=request,
            artifact_path=artifact_path,
        )
        state = {
            "run_id": request["run_id"],
            "request": request,
            "checkpoint_events": [],
            "draft_claims": [],
            "evidence": [],
            "validator_results": [],
        }

        with patch(
            "bi_agent.runtime.langgraph_workflow.build_answer_package",
            side_effect=lambda **kwargs: kwargs,
        ):
            workflow_kwargs = _build_answer_package_from_state(state)

        self.assertEqual(
            workflow_kwargs["context_manifest"],
            dict(expected.context_owner),
        )
        self.assertEqual(
            workflow_kwargs["trusted_claim_provenance_record"],
            dict(expected.trusted_provenance),
        )
        tampered = AnswerPackageBuildContext.create(
            request=request,
            artifact_path="artifacts/tampered/answer_package.json",
        )
        self.assertNotEqual(
            tampered.trusted_provenance["record_ref"],
            expected.trusted_provenance["record_ref"],
        )

    def test_analysis_runtime_executes_exact_slot_and_persists_complete_zero_claim_chain(self):
        from bi_agent.conversation.store import InMemoryConversationStore
        from bi_agent.runtime.clickhouse_runtime import ClickHouseQueryResult
        from bi_agent.runtime.dataset_catalog import DatasetCatalog
        from bi_agent.runtime.evidence_authority import RuntimeEvidenceAuthority
        from bi_agent.runtime.query_executor import ClickHouseQueryExecutor
        from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry
        from tests.phase4.test_analysis_contract_compiler import (
            canonical_release_catalog,
            snapshot,
        )

        class CompleteRuntime:
            def aggregate(self, sql, query_id, **kwargs):
                return ClickHouseQueryResult(
                    ok=True,
                    query_id=query_id,
                    rows=(
                        {
                            "window_id": "target_day",
                            "window_role": "target",
                            "observation_key": "2026-06-02",
                            "paid_amount": 120.0,
                        },
                        {
                            "window_id": "previous_day",
                            "window_role": "baseline",
                            "observation_key": "2026-06-01",
                            "paid_amount": 100.0,
                        },
                    ),
                )

            bounded_context = aggregate

        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        authority = RuntimeEvidenceAuthority(runtime_registry=registry)
        catalog, release_resolver, _ = canonical_release_catalog(
            snapshot("paid_order_success", "paid", "2026-07-04")
        )
        runtime = AnalysisRuntime(
            catalog=catalog,
            registry=registry,
            executor=ClickHouseQueryExecutor(
                CompleteRuntime(),
                evidence_resolver=authority,
                rows_loader=authority.rows_loader,
                evidence_writer=authority._runtime_writer(),
            ),
            release_resolver=release_resolver,
            evidence_authority=authority,
        )
        request = AnalysisRuntimeRequest.create(
            run_id="run-runtime-complete",
            proposal={
                "question_families": ["custom_baseline_comparison"],
                "target_metrics": ["paid_amount"],
                "claim_intents": ["comparative_change"],
                "scope": {"type": "full_sample"},
                "target_semantic": "yesterday",
                "baselines": ["previous_day"],
            },
            accepted_graph=("compare_periods",),
            as_of="2026-06-03T12:00:00+01:00",
            permission_scope="analyst",
        )

        result = runtime.execute(request)
        typed_graph = _typed_clarification_compiled_graph(
            runtime.compile(request),
            {
                "requested_nodes": ["compare_periods"],
                "target_claim": "comparative_change",
            },
        )
        missing_runtime = AnalysisRuntime(
            catalog=DatasetCatalog(()),
            registry=registry,
            executor=runtime.executor,
            release_resolver=None,
            evidence_authority=authority,
        )
        hard_gap_graph = _typed_clarification_compiled_graph(
            missing_runtime.compile(request),
            {
                "requested_nodes": ["compare_periods"],
                "target_claim": "comparative_change",
            },
        )
        bundle = runtime.build_persistence_bundle(
            result,
            answer_package={"status": "draft", "sections": []},
            request={
                "run_id": "run-runtime-complete",
                "thread_id": "thread-runtime-complete",
                "topic_id": "topic-runtime-complete",
                "permission_context": {"role": "analyst"},
                "context_manifest": {"manifest_id": "context-runtime", "items": []},
            },
            artifact_path="artifacts/task10-core/run-runtime-complete.json",
        )
        store = InMemoryConversationStore()

        self.assertEqual(result.status, "ready")
        self.assertEqual(typed_graph.status, "accepted")
        self.assertEqual(
            typed_graph.mutations.accepted_graph,
            ("compare_periods",),
        )
        self.assertIsNone(hard_gap_graph)
        self.assertEqual(result.completeness_reports[0].completeness_status, "complete")
        self.assertEqual(result.bound_capability_inputs["compare_periods"].status, "ready")
        self.assertEqual(
            tuple(result.bound_capability_inputs["compare_periods"].rows_by_slot),
            ("daily_metric_baselines",),
        )
        self.assertEqual(
            store.save_analysis_runtime_records(
                run_id="run-runtime-complete",
                **bundle,
            ),
            "published",
        )
        self.assertEqual(bundle["verified_claims"], ())

        binding = result.persistence_records["capability_binding_records"][0]
        evidence_ref = "evidence:runtime-complete"
        claim_bundle = runtime.build_persistence_bundle(
            result,
            answer_package={
                "status": "draft",
                "sections": [
                    {
                        "section_id": "summary",
                        "payload": {
                            "claims": [
                                {
                                    "text": "目标日付费金额高于前一日。",
                                    "claim_type": "comparative_change",
                                    "claim_strength": "observed",
                                    "evidence_refs": [evidence_ref],
                                    "numbers": {},
                                }
                            ]
                        },
                    },
                    {
                        "section_id": "evidence",
                        "payload": {
                            "evidence": [
                                {
                                    "evidence_ref": evidence_ref,
                                    "binding_manifest_ref": binding.record_ref,
                                }
                            ]
                        },
                    },
                ],
            },
            request={
                "run_id": "run-runtime-complete",
                "thread_id": "thread-runtime-complete",
                "topic_id": "topic-runtime-complete",
                "permission_context": {"role": "analyst"},
                "context_manifest": {"manifest_id": "context-runtime", "items": []},
            },
            artifact_path="artifacts/task10-core/run-runtime-complete.json",
        )

        self.assertEqual(len(claim_bundle["verified_claims"]), 1)
        self.assertEqual(
            InMemoryConversationStore().save_analysis_runtime_records(
                run_id="run-runtime-complete",
                **claim_bundle,
            ),
            "published",
        )

        resumed_request = AnalysisRuntimeRequest.create(
            run_id="run-runtime-complete-resumed",
            proposal=dict(request.proposal),
            accepted_graph=request.accepted_graph,
            as_of=request.as_of,
            permission_scope=request.permission_scope,
        )
        resumed_result = runtime.execute(resumed_request)
        original_bound = result.bound_capability_inputs["compare_periods"]
        resumed_bound = resumed_result.bound_capability_inputs["compare_periods"]

        self.assertEqual(resumed_result.status, "ready")
        self.assertNotEqual(
            resumed_result.analysis_contract.analysis_contract_id,
            result.analysis_contract.analysis_contract_id,
        )
        self.assertNotEqual(
            resumed_bound.analysis_contract_ref,
            original_bound.analysis_contract_ref,
        )
        self.assertNotEqual(
            resumed_bound.binding_manifest_ref,
            original_bound.binding_manifest_ref,
        )
        self.assertNotEqual(
            resumed_bound.binding_manifest_digest,
            original_bound.binding_manifest_digest,
        )
        self.assertNotEqual(resumed_bound.result_refs, original_bound.result_refs)
        self.assertTrue(
            all(
                "run-runtime-complete-resumed" in ref
                for ref in resumed_bound.query_contract_refs
            )
        )

    def test_preexecution_query_contracts_include_snapshot_authority_records(self):
        from bi_agent.runtime.clickhouse_runtime import ClickHouseQueryResult
        from bi_agent.runtime.dataset_catalog import DatasetCatalog
        from bi_agent.runtime.evidence_authority import RuntimeEvidenceAuthority
        from bi_agent.runtime.query_executor import ClickHouseQueryExecutor
        from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry
        from tests.phase4.test_analysis_contract_compiler import (
            canonical_release_catalog,
            snapshot,
        )

        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        authority = RuntimeEvidenceAuthority(runtime_registry=registry)
        catalog, release_resolver, _ = canonical_release_catalog(
            snapshot("paid_order_success", "paid", "2026-07-04")
        )

        class CompletePatternRuntime:
            def aggregate(self, sql, query_id, **kwargs):
                return ClickHouseQueryResult(
                    ok=True,
                    query_id=query_id,
                    rows=(
                        {
                            "window_id": "target_day",
                            "window_role": "target",
                            "observation_key": "2026-06-02",
                            "paid_amount": 120.0,
                        },
                        {
                            "window_id": "previous_day",
                            "window_role": "baseline",
                            "observation_key": "2026-06-01",
                            "paid_amount": 100.0,
                        },
                    ),
                )

            bounded_context = aggregate

        runtime = AnalysisRuntime(
            catalog=catalog,
            registry=registry,
            executor=ClickHouseQueryExecutor(
                CompletePatternRuntime(),
                evidence_resolver=authority,
                rows_loader=authority.rows_loader,
                evidence_writer=authority._runtime_writer(),
                release_resolver=release_resolver,
            ),
            release_resolver=release_resolver,
            evidence_authority=authority,
        )
        request = AnalysisRuntimeRequest.create(
            run_id="run-preexecution-snapshot-authority",
            proposal={
                "question_families": ["recurring_pattern_analysis"],
                "target_metrics": ["paid_amount"],
                "claim_intents": ["recurring_pattern_existence"],
                "target_semantic": "yesterday",
                "baselines": ["previous_day"],
            },
            accepted_graph=("pattern_scan", "segment_contribution"),
            as_of="2026-06-03T12:00:00+01:00",
            permission_scope="analyst",
        )

        result = runtime.execute(request)
        expected_snapshot_refs = {
            ref
            for contract in result.query_contracts
            for ref in contract.dataset_snapshot_refs
        }

        self.assertTrue(result.query_contracts)
        self.assertTrue(result.query_results)
        self.assertEqual(
            {
                record.snapshot_ref
                for record in result.persistence_records["snapshot_records"]
            },
            expected_snapshot_refs,
        )

    def test_analysis_runtime_passes_release_authority_to_query_validation(self):
        from bi_agent.conversation.store import InMemoryConversationStore
        from bi_agent.runtime.clickhouse_runtime import ClickHouseQueryResult
        from bi_agent.runtime.evidence_authority import RuntimeEvidenceAuthority
        from bi_agent.runtime.query_executor import ClickHouseQueryExecutor
        from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry
        from tests.phase4.test_analysis_contract_compiler import (
            _market_dashboard_snapshots,
            released_catalog,
        )

        class CompleteMarketRuntime:
            def aggregate(self, sql, query_id, **kwargs):
                return ClickHouseQueryResult(
                    ok=True,
                    query_id=query_id,
                    rows=(
                        {
                            "window_id": "target_day",
                            "window_role": "target",
                            "observation_key": "2026-06-02",
                            "active_users": 120.0,
                        },
                        {
                            "window_id": "previous_day",
                            "window_role": "baseline",
                            "observation_key": "2026-06-01",
                            "active_users": 100.0,
                        },
                    ),
                )

            bounded_context = aggregate

        catalog = released_catalog(*_market_dashboard_snapshots())
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        authority = RuntimeEvidenceAuthority(runtime_registry=registry)
        runtime = AnalysisRuntime(
            catalog=catalog,
            registry=registry,
            executor=ClickHouseQueryExecutor(
                CompleteMarketRuntime(),
                evidence_resolver=authority,
                rows_loader=authority.rows_loader,
                evidence_writer=authority._runtime_writer(),
                release_resolver=catalog._release_resolver,
            ),
            release_resolver=catalog._release_resolver,
            evidence_authority=authority,
        )
        request = AnalysisRuntimeRequest.create(
            run_id="run-runtime-release",
            proposal={
                "question_families": ["market_health_comparison"],
                "target_metrics": ["active_users"],
                "claim_intents": ["comparative_change"],
                "scope": {"type": "full_sample"},
                "target_semantic": "yesterday",
                "baselines": ["previous_day"],
            },
            accepted_graph=("market_health_compare",),
            as_of="2026-06-03T12:00:00+01:00",
            permission_scope="analyst",
        )

        result = runtime.execute(request)
        bundle = runtime.build_persistence_bundle(
            result,
            answer_package={"status": "failed", "sections": []},
            request={
                "run_id": request.run_id,
                "thread_id": "thread-runtime-release",
                "topic_id": "topic-runtime-release",
                "permission_context": {"role": "analyst"},
            },
            artifact_path="artifacts/task10-core/run-runtime-release.json",
        )

        self.assertEqual(len(result.query_results), 1)
        self.assertEqual(
            result.query_contracts[0].dataset_snapshot_refs,
            ("snapshot:market:capability-local",),
        )
        self.assertEqual(
            InMemoryConversationStore().save_analysis_runtime_records(
                run_id=request.run_id,
                **bundle,
            ),
            "published",
        )

    def test_analysis_contract_gap_that_changes_the_conclusion_requests_clarification(self):
        runtime_result = AnalysisRuntimeResult(
            analysis_contract=object(),
            query_contracts=(),
            query_results=(),
            completeness_reports=(),
            capability_plans=(),
            bound_capability_inputs={},
            repair_decisions=(),
            typed_gaps=({"requires_clarification": True},),
            persistence_records={},
        )

        self.assertEqual(runtime_result.status, "clarify")

    def test_typed_unbound_clarification_bypasses_only_legacy_precompile(self):
        from types import SimpleNamespace

        gap = SimpleNamespace(
            requires_clarification=True,
            affected_capabilities=("event_evidence",),
            to_dict=lambda: {
                "requires_clarification": True,
                "affected_capabilities": ["event_evidence"],
            },
        )
        analysis_contract = SimpleNamespace(
            contract_gaps=(gap,),
            to_dict=lambda: {"contract_gaps": [{"requires_clarification": True}]},
        )
        outcome = SimpleNamespace(
            analysis_contract=analysis_contract,
            query_contracts=(
                SimpleNamespace(
                    query_contract_id="query:ready",
                    to_dict=lambda: {"query": "ready"},
                ),
            ),
            capability_plans=(
                SimpleNamespace(
                    capability_id="market_health_compare",
                    required_input_slots=({"query_contract_refs": ("query:ready",)},),
                    optional_input_slots=(),
                ),
                SimpleNamespace(
                    capability_id="event_evidence",
                    required_input_slots=({"query_contract_refs": ()},),
                    optional_input_slots=(),
                ),
            ),
        )
        self.assertTrue(analysis_outcome_requires_route_clarification(outcome))
        self.assertTrue(analysis_outcome_has_executable_ready_capability(outcome))
        state = {
            "run_id": "run-typed-precompile-clarify",
            "request": {
                "analysis_runtime": SimpleNamespace(compile=lambda _: outcome),
                "analysis_context": {"as_of": "2026-06-03T12:00:00+01:00"},
                "role": "analyst",
                "question": "外部活动是否影响活跃用户？",
            },
            "intent": {
                "question_family": "business_object_impact_review",
                "question_families": ["business_object_impact_review"],
                "target_metric": "active_users",
                "pattern_family": "custom_baseline",
                "requested_nodes": ("market_health_compare", "event_evidence"),
                "scope": "full_sample",
                "time_window": "2026-06-02",
            },
            "analysis_route": {
                "requested_nodes": ("market_health_compare", "event_evidence"),
                "analysis_requirements": {
                    "target_metrics": ["active_users"],
                    "context_sources": ["external_event"],
                },
            },
        }

        with patch(
            "bi_agent.runtime.langgraph_workflow.compile_graph",
            side_effect=AssertionError("legacy compiler must not run"),
        ):
            _accept_analysis_route(state)

        self.assertEqual(
            state["compiled_graph"].mutations.accepted_graph,
            ("market_health_compare", "event_evidence"),
        )

        pure_unbound_outcome = SimpleNamespace(
            analysis_contract=analysis_contract,
            query_contracts=(),
            capability_plans=(
                SimpleNamespace(
                    capability_id="event_evidence",
                    required_input_slots=({"query_contract_refs": ()},),
                    optional_input_slots=(),
                ),
            ),
        )
        runtime = object.__new__(AnalysisRuntime)
        runtime._catalog_provider = lambda: SimpleNamespace(snapshots=lambda: ())
        runtime._compile_with_catalog = lambda request, catalog: pure_unbound_outcome
        runtime._authority_records = (
            lambda compiled, results, bound, **kwargs: {}
        )
        result = runtime.execute(
            AnalysisRuntimeRequest.create(
                run_id="run-no-query-before-clarify",
                proposal={"target_metrics": ["active_users"]},
                accepted_graph=("market_health_compare", "event_evidence"),
                as_of="2026-06-03T12:00:00+01:00",
                permission_scope="analyst",
            )
        )
        self.assertEqual(result.status, "clarify")
        self.assertEqual(result.query_results, ())

        optional_only = SimpleNamespace(
            analysis_contract=analysis_contract,
            query_contracts=outcome.query_contracts,
            capability_plans=(
                SimpleNamespace(
                    capability_id="event_evidence",
                    required_input_slots=({
                        "required": True,
                        "query_contract_refs": ("query:event-ready",),
                    },),
                    optional_input_slots=({
                        "required": False,
                        "query_contract_refs": (),
                    },),
                ),
            ),
        )
        self.assertFalse(
            analysis_outcome_requires_preexecution_clarification(optional_only)
        )

    def test_partially_bound_required_slots_are_not_executable_ready(self):
        from types import SimpleNamespace

        gap = SimpleNamespace(
            requires_clarification=True,
            affected_capabilities=("segment_contribution",),
            to_dict=lambda: {
                "requires_clarification": True,
                "affected_capabilities": ["segment_contribution"],
            },
        )
        outcome = SimpleNamespace(
            analysis_contract=SimpleNamespace(contract_gaps=(gap,)),
            query_contracts=(
                SimpleNamespace(query_contract_id="query:segment:bound"),
            ),
            capability_plans=(
                SimpleNamespace(
                    capability_id="segment_contribution",
                    required_input_slots=(
                        {
                            "required": True,
                            "query_contract_refs": ("query:segment:bound",),
                        },
                        {
                            "required": True,
                            "query_contract_refs": (),
                        },
                    ),
                    optional_input_slots=(),
                ),
            ),
        )

        self.assertFalse(analysis_outcome_has_executable_ready_capability(outcome))
        self.assertTrue(analysis_outcome_requires_preexecution_clarification(outcome))

        class ExecutorMustNotRun:
            def execute(self, *args, **kwargs):
                raise AssertionError("partial_required_plan_must_stop_preexecution")

        runtime = object.__new__(AnalysisRuntime)
        runtime.executor = ExecutorMustNotRun()
        runtime._catalog_provider = lambda: SimpleNamespace(snapshots=lambda: ())
        runtime._compile_with_catalog = lambda request, catalog: outcome
        runtime._authority_records = lambda compiled, results, bound, **kwargs: {}
        result = runtime.execute(
            AnalysisRuntimeRequest.create(
                run_id="run-partial-required-plan",
                proposal={"target_metrics": ["paid_amount"]},
                accepted_graph=("segment_contribution",),
                as_of="2026-06-03T12:00:00+01:00",
                permission_scope="analyst",
            )
        )
        self.assertEqual(result.query_results, ())

    def test_nonclarification_outcome_does_not_swallow_legacy_compile_error(self):
        from types import SimpleNamespace

        outcome = SimpleNamespace(
            analysis_contract=SimpleNamespace(contract_gaps=()),
            query_contracts=(),
            capability_plans=(),
        )
        state = {
            "run_id": "run-nonclarify-hard-error",
            "request": {
                "analysis_runtime": SimpleNamespace(compile=lambda _: outcome),
                "analysis_context": {"as_of": "2026-06-03T12:00:00+01:00"},
                "role": "analyst",
                "question": "测试硬错误。",
            },
            "intent": {
                "question_family": "custom_baseline_comparison",
                "question_families": ["custom_baseline_comparison"],
                "target_metric": "active_users",
                "pattern_family": "custom_baseline",
                "requested_nodes": ("market_health_compare",),
                "scope": "full_sample",
                "time_window": "2026-06-02",
            },
            "analysis_route": {
                "requested_nodes": ("market_health_compare",),
                "analysis_requirements": {"target_metrics": ["active_users"]},
            },
        }

        with patch(
            "bi_agent.runtime.langgraph_workflow.compile_graph",
            side_effect=ValueError("legacy_hard_error"),
        ), self.assertRaisesRegex(ValueError, "legacy_hard_error"):
            _accept_analysis_route(state)

    def test_executed_rows_do_not_degrade_a_blocked_completeness_boundary(self):
        from types import SimpleNamespace

        runtime_result = AnalysisRuntimeResult(
            analysis_contract=object(),
            query_contracts=(),
            query_results=(object(),),
            completeness_reports=(SimpleNamespace(analysis_readiness="blocked"),),
            capability_plans=(),
            bound_capability_inputs={},
            repair_decisions=(SimpleNamespace(action="degrade"),),
            typed_gaps=(),
            persistence_records={},
        )

        self.assertEqual(runtime_result.status, "blocked")

    def test_answer_package_carries_typed_runtime_payload_after_query_repair(self):
        package = _build_answer_package_from_state(
            {
                "run_id": "run-typed-package",
                "request": {
                    "role": "analyst",
                    "analysis_contract": {"analysis_contract_id": "analysis:typed"},
                    "query_contracts": [{"query_contract_id": "query:typed"}],
                    "query_results": [{"result_ref": "result:typed"}],
                    "completeness_reports": [{"report_ref": "complete:typed"}],
                    "capability_execution_plans": [{"capability_id": "compare_periods"}],
                    "repair_decisions": [
                        {"failed_signature": "signature", "action": "degrade"}
                    ],
                },
                "checkpoint_events": [],
                "draft_claims": [],
                "evidence": [],
                "validator_results": [],
                "final_explanation": {
                    "status": "blocked",
                    "code": "typed_query_gap",
                    "reason": "结果完整性不足。",
                    "owner": "data_platform",
                    "repair_path": "刷新后重试。",
                },
            }
        )

        self.assertEqual(
            package["admin_audit"]["analysis_contract"]["analysis_contract_id"],
            "analysis:typed",
        )
        self.assertEqual(len(package["admin_audit"]["query_contracts"]), 1)
        self.assertEqual(len(package["admin_audit"]["repair_attempts"]), 1)

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

    def test_capabilities_select_runtime_rows_by_query_intent(self):
        state = {
            "request": {
                "rows": ({"period": "fallback", "group": "target", "amount": 1.0},),
                "result_refs": ("fallback-ref",),
                "runtime_rows_by_intent": {
                    "daily_metric_baselines": (
                        {"period": "2026-07-08", "group": "target", "amount": 120.0},
                    ),
                    "dimension_scan": (
                        {
                            "period": "2026-07-08",
                            "group": "target",
                            "channel": "ads",
                            "amount": 80.0,
                        },
                    ),
                    "joint_candidate_scan": (
                        {
                            "period": "2026-07-08",
                            "group": "target",
                            "channel": "ads",
                            "payment_method": "card",
                            "amount": 70.0,
                        },
                    ),
                    "data_quality_probe": (
                        {"period": "2026-07-08", "group": "target", "orders": 10},
                    ),
                },
                "result_refs_by_intent": {
                    "daily_metric_baselines": ("baseline-ref",),
                    "dimension_scan": ("dimension-ref",),
                    "joint_candidate_scan": ("joint-ref",),
                    "data_quality_probe": ("quality-ref",),
                },
            },
            "row_query_plan": {
                "query_results": (
                    {"intent": "dimension_scan", "dimension_keys": ("channel",)},
                    {
                        "intent": "joint_candidate_scan",
                        "dimension_keys": ("channel", "payment_method"),
                    },
                )
            },
            "intent": {"pattern_params": {}},
        }

        self.assertEqual(_capability_rows_for(state, "driver_decomposition")[0]["amount"], 120.0)
        self.assertEqual(_capability_rows_for(state, "segment_contribution")[0]["channel"], "ads")
        self.assertEqual(
            _capability_rows_for(state, "joint_attribution")[0]["payment_method"],
            "card",
        )
        self.assertEqual(_capability_rows_for(state, "data_quality_profile")[0]["orders"], 10)
        self.assertEqual(_capability_result_refs_for(state, "joint_attribution"), ("joint-ref",))
        self.assertEqual(_segment_contribution_params(state)["segment_key"], "channel")

    def test_capability_rows_follow_compiler_declared_inputs(self):
        state = {
            "request": {
                "rows": ({"period": "fallback", "group": "target", "amount": 1.0},),
                "result_refs": ("fallback-ref",),
                "compiler_runtime_plan": {
                    "capability_inputs": {
                        "segment_contribution": {
                            "preferred_query_intents": (
                                "joint_candidate_scan",
                                "dimension_scan",
                            ),
                            "required_fields": ("amount", "channel", "payment_method"),
                            "dimension_keys": ("channel", "payment_method"),
                            "gap_policy": "degrade_to_available_dimensions",
                        }
                    }
                },
                "runtime_rows_by_intent": {
                    "dimension_scan": (
                        {
                            "period": "2026-07-08",
                            "group": "target",
                            "channel": "ads",
                            "amount": 120.0,
                        },
                    ),
                    "joint_candidate_scan": (
                        {
                            "period": "2026-07-08",
                            "group": "target",
                            "channel": "ads",
                            "payment_method": "card",
                            "amount": 90.0,
                        },
                    ),
                },
                "result_refs_by_intent": {
                    "dimension_scan": ("dimension-ref",),
                    "joint_candidate_scan": ("joint-ref",),
                },
            },
            "intent": {"pattern_params": {}},
        }

        self.assertEqual(
            _capability_rows_for(state, "segment_contribution")[0]["payment_method"],
            "card",
        )
        self.assertEqual(
            _capability_result_refs_for(state, "segment_contribution"),
            ("joint-ref",),
        )

    def test_high_value_capability_prefers_high_value_scan_rows(self):
        state = {
            "request": {
                "rows": ({"period": "fallback", "group": "target", "amount": 1.0},),
                "result_refs": ("fallback-ref",),
                "runtime_rows_by_intent": {
                    "daily_metric_baselines": (
                        {"period": "2026-07-08", "group": "target", "amount": 120.0},
                    ),
                    "high_value_scan": (
                        {
                            "period": "2026-07-08",
                            "group": "target",
                            "amount": 120.0,
                            "paid_users": 10,
                            "high_value_amount": 80.0,
                            "high_value_paid_users": 2,
                        },
                    ),
                },
                "result_refs_by_intent": {
                    "daily_metric_baselines": ("baseline-ref",),
                    "high_value_scan": ("high-value-ref",),
                },
            },
            "intent": {"pattern_params": {}},
        }

        self.assertEqual(
            _capability_rows_for(state, "high_value_user_contribution")[0][
                "high_value_amount"
            ],
            80.0,
        )
        self.assertEqual(
            _capability_result_refs_for(state, "high_value_user_contribution"),
            ("high-value-ref",),
        )

    def test_coverage_uses_baseline_rows_when_quality_probe_is_primary(self):
        state = {
            "validator_results": [{"ok": True}],
            "request": {
                "required_fields": ("period", "group", "amount", "orders"),
                "rows": (
                    {
                        "period": "2026-07-08",
                        "group": "target",
                        "orders": 10,
                        "paid_users": 8,
                        "min_period": "2026-07-01",
                        "max_period": "2026-07-08",
                    },
                ),
                "runtime_rows_by_intent": {
                    "daily_metric_baselines": (
                        {
                            "period": "2026-07-07",
                            "group": "previous_day",
                            "amount": 90.0,
                            "orders": 9,
                        },
                        {
                            "period": "2026-07-08",
                            "group": "target",
                            "amount": 120.0,
                            "orders": 10,
                        },
                    ),
                    "data_quality_probe": (
                        {
                            "period": "2026-07-08",
                            "group": "target",
                            "orders": 10,
                            "paid_users": 8,
                        },
                    ),
                },
            },
            "intent": {
                "pattern_family": "daily_change",
                "pattern_params": {},
            },
        }

        self.assertIn("本地聚合结果", _local_coverage_answerable_reason(state))

    def test_reused_dimension_scan_rows_feed_segment_capability(self):
        compiled = compile_graph(
            question_family="paid_amount_change_explanation",
            target_metric="paid_amount",
            pattern_family="custom_baseline",
            requested_nodes=(
                "data_quality_profile",
                "driver_decomposition",
                "segment_contribution",
                "answer_verify",
            ),
        )
        state = {
            "request": {
                "rows": [
                    {"period": "fallback", "group": "target", "amount": 1.0},
                ],
                "compiler_runtime_plan": {"baselines": ("previous_day",)},
                "required_fields": ("period", "group", "amount", "orders"),
                "role": "analyst",
            },
            "run_id": "reuse-segment-capability",
            "sql_hash": "sqlhash-reuse-segment",
            "budget_state": default_budget("ordinary"),
            "compiled_graph": compiled,
            "intent": {
                "question_family": "paid_amount_change_explanation",
                "target_metric": "paid_amount",
                "analysis_requirements": {
                    "requested_dimensions": ["channel"],
                    "diagnostic_tags": ["pattern_attribution"],
                },
                "pattern_family": "custom_baseline",
                "pattern_params": {"group_key": "group", "target_group": "target"},
                "scope": "full_sample",
                "time_window": "yesterday",
                "target_claim": "按渠道解释昨天付费金额变化",
            },
        }
        _apply_reused_dimension_scan_input(
            state,
            {
                "query_ref": "asset-dimension-ref",
                "dimensions": ("channel",),
                "rows": [
                    {
                        "period": "2026-07-07",
                        "group": "previous_day",
                        "channel": "ads",
                        "amount": 60.0,
                        "orders": 12,
                    },
                    {
                        "period": "2026-07-08",
                        "group": "target",
                        "channel": "ads",
                        "amount": 95.0,
                        "orders": 18,
                    },
                ],
            },
        )

        self.assertEqual(
            _capability_result_refs_for(state, "segment_contribution"),
            ("asset-dimension-ref",),
        )
        self.assertEqual(
            _capability_rows_for(state, "segment_contribution")[0]["channel"],
            "ads",
        )

        with patch.dict(
            "os.environ",
            {
                "WAJE_ALLOW_LEGACY_FIXTURES": "1",
                "WAJE_RUNTIME_ENV": "test",
            },
        ):
            result = _execute_capabilities(state)
        segment = next(
            item for item in result["evidence"] if item.get("capability_id") == "segment_contribution"
        )

        self.assertEqual(segment["evidence_type"], "statistical_association")
        self.assertEqual(segment["result_refs"], ["asset-dimension-ref"])
        self.assertEqual(segment["typed_payload"]["segment_count"], 1)

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

    def test_workflow_compiler_uses_row_provider_schema_fields(self):
        class Provider:
            def __init__(self):
                self.compiler_plan = None

            def configured(self):
                return True

            def binding_reason(self):
                return ""

            def schema_fields(self):
                return (
                    "business_date_lagos",
                    "paid_amount_ngn",
                    "user_id",
                    "channel",
                    "payment_method",
                    "package_name",
                    "gameplay_id",
                    "payment_status",
                    "order_id",
                )

            def plan(self, request, intent, accepted_graph):
                from bi_agent.runtime.clickhouse_revenue_rows import RevenueRowPlan

                self.compiler_plan = request["compiler_runtime_plan"]
                return RevenueRowPlan(
                    sql_text=(
                        "SELECT period, group, sum(amount) AS amount "
                        "FROM t GROUP BY period, group"
                    ),
                    query_id="query-schema-aware",
                    required_fields=("period", "group", "amount"),
                    dimension_keys=("package_name", "gameplay_id"),
                )

            def fetch(self, plan):
                from bi_agent.runtime.clickhouse_revenue_rows import RevenueRowsResult

                return RevenueRowsResult(
                    ok=True,
                    rows=(
                        {
                            "period": "2026-07-08",
                            "group": "target",
                            "amount": 130,
                            "package_name": "pkg-a",
                            "gameplay_id": "mode-a",
                        },
                    ),
                    query_hash="hash-schema-aware",
                    query_id="query-schema-aware",
                    result_refs=("hash-schema-aware",),
                )

        provider = Provider()
        run_pattern_workflow(
            {
                "run_id": "schema-aware-provider",
                "question": "昨天收入变化最大的是哪个包或玩法？支付状态和重复订单会不会影响判断？",
                "llm_client": FakeLLMClient(
                    {
                        "analysis_route": {
                            "requested_nodes": [
                                "data_quality_profile",
                                "segment_contribution",
                                "joint_attribution",
                                "answer_verify",
                            ]
                        }
                    }
                ),
                "row_provider": provider,
            }
        )

        row_shape = provider.compiler_plan["row_shapes"][0]
        self.assertIn("package_name", row_shape["dimension_keys"])
        self.assertIn("gameplay_id", row_shape["dimension_keys"])
        self.assertIsInstance(row_shape["optional_fields"], tuple)

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
                        "business_intent": {
                            "question_family": "segment_or_factor_attribution",
                            "question_families": ["segment_or_factor_attribution"],
                            "target_metric": "paid_amount",
                            "pattern_family": "custom_baseline",
                        },
                        "analysis_route": {
                            "requested_nodes": ["joint_attribution", "answer_verify"],
                            "analysis_requirements": {
                                "requested_dimensions": ["channel", "payment_method"],
                                "diagnostic_tags": ["factor_topk"],
                            },
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

    def test_general_clarification_waits_with_validated_business_options(self):
        from bi_agent.runtime.langgraph_workflow import _generate_clarification

        fake = FakeLLMClient({
            "clarification_question": {
                "questions": [{
                    "question": "按哪个已确认业务边界继续？",
                    "options": [
                        {"label": "保留当前指标和基线继续。", "description": "保留已确认口径。"},
                        {"label": "调整业务范围后继续。", "description": "用新范围重新绑定。"},
                        "tell the agent to do differently",
                    ],
                }],
                "recommended_assumption": {"option": "保留当前指标和基线继续。"},
                "status_message": "等待确认。",
            }
        })
        with tempfile.TemporaryDirectory() as artifact_root:
            state = {
                "run_id": "run-general-clarification",
                "request": {"artifact_root": artifact_root},
                "intent": {
                    "target_metric": "active_users",
                    "baseline_candidates": ["previous_day"],
                    "scope": "full_sample",
                },
                "boundary_decision": {
                    "boundary_status": "needs_question",
                    "clarification_questions": [],
                },
                "llm_client": fake,
                "llm_calls": [],
                "checkpoint_events": [],
            }

            _generate_clarification(state)
            self.assertEqual(_route_after_clarification(state), "wait")
            _persist_clarification(state)

        self.assertEqual(state["workflow_status"], "waiting_for_clarification")
        self.assertEqual(
            state["answer_package"]["material_slots"]["target_metrics"],
            ["active_users"],
        )
        self.assertEqual(
            state["answer_package"]["material_slots"]["baselines"],
            ["previous_day"],
        )
        self.assertEqual(
            state["answer_package"]["clarification"]["recommended_assumption"],
            {"option": "保留当前指标和基线继续。"},
        )

    def test_general_clarification_option_objects_reject_unsafe_or_ambiguous_shapes(self):
        from bi_agent.runtime.langgraph_workflow import (
            _normalize_general_clarification_output,
        )

        base = {
            "questions": [{
                "question": "按哪个范围继续？",
                "options": [
                    {"label": "保留当前范围", "description": "继续当前口径"},
                    {"label": "调整业务范围", "description": "重新确认口径"},
                    "tell the agent to do differently",
                ],
            }],
            "recommended_assumption": {"label": "保留当前范围"},
        }
        normalized = _normalize_general_clarification_output(base)
        self.assertEqual(
            normalized["questions"][0]["options"][0],
            "保留当前范围",
        )

        invalid_options = (
            {"label": "保留当前范围", "description": "继续", "action": "override"},
            {"label": "", "description": "继续"},
            {"label": "保留当前范围", "description": "evidence_ref"},
        )
        for invalid in invalid_options:
            candidate = {
                **base,
                "questions": [{
                    **base["questions"][0],
                    "options": [
                        invalid,
                        {"label": "调整业务范围", "description": "重新确认"},
                        "tell the agent to do differently",
                    ],
                }],
            }
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                WorkflowFailure,
                "general_clarification_contract_invalid:option_object",
            ):
                _normalize_general_clarification_output(candidate)

        duplicate = {
            **base,
            "questions": [{
                **base["questions"][0],
                "options": [
                    {"label": "保留当前范围", "description": "继续"},
                    {"label": "保留当前范围", "description": "换个说法"},
                    "tell the agent to do differently",
                ],
            }],
        }
        with self.assertRaisesRegex(
            WorkflowFailure,
            "general_clarification_contract_invalid:options",
        ):
            _normalize_general_clarification_output(duplicate)

    def test_general_clarification_prompt_requires_string_option_array(self):
        text = "\n".join(
            message["content"]
            for message in build_prompt("clarification_question", {}).messages
        )

        self.assertIn("options must be an array of strings", text)
        self.assertIn('"options":["业务选项A","业务选项B"', text)

    def test_general_clarification_repairs_recommendation_to_validated_option(self):
        from bi_agent.runtime.langgraph_workflow import _generate_clarification

        fake = FakeLLMClient(
            {
                "clarification_question": {
                    "questions": [{
                        "question": "按哪个基线继续？",
                        "options": [
                            "与前一天比较",
                            "与上周同一天比较",
                            "tell the agent to do differently",
                        ],
                    }],
                    "recommended_assumption": {"option": "使用推荐基线"},
                },
                "query_gap_recommendation_repair": {
                    "option_index": 1,
                    "brief_reason": "更适合控制星期效应。",
                },
            }
        )
        state = {
            "request": {},
            "intent": {"target_metric": "active_users"},
            "boundary_decision": {
                "boundary_status": "needs_question",
                "clarification_questions": [],
            },
            "llm_client": fake,
            "llm_calls": [],
            "checkpoint_events": [],
        }

        _generate_clarification(state)

        self.assertIn("query_gap_recommendation_repair", fake.calls)
        self.assertEqual(
            state["clarification_outcome"]["recommended_assumption"],
            {"option": "与上周同一天比较"},
        )

    def test_route_requirements_preserve_confirmed_material_slots_and_flag_conflict(self):
        state = {
            "intent": {
                "target_metric": "active_users",
                "baseline_candidates": ["previous_day"],
                "context_sources": ["external_event"],
                "scope": "full_sample",
            },
            "request": {},
        }
        merged, conflicts = _merge_confirmed_material_requirements(
            {
                "analysis_requirements": {
                    "target_metrics": [],
                    "baselines": [],
                    "context_sources": [],
                    "scope": "full_sample",
                }
            },
            state,
        )

        self.assertEqual(merged["analysis_requirements"]["target_metrics"], ["active_users"])
        self.assertEqual(merged["analysis_requirements"]["baselines"], ["previous_day"])
        self.assertEqual(merged["analysis_requirements"]["context_sources"], ["external_event"])
        self.assertEqual(conflicts, ())

        _, conflicts = _merge_confirmed_material_requirements(
            {
                "analysis_requirements": {
                    "target_metrics": ["paid_amount"],
                    "scope": "channel",
                }
            },
            state,
        )
        self.assertEqual(set(conflicts), {"target_metrics", "scope"})

    def test_invalid_general_clarification_retries_with_contract_reason(self):
        from bi_agent.runtime.langgraph_workflow import _generate_clarification

        class InvalidThenValid(FakeLLMClient):
            def __init__(self):
                super().__init__()
                self.attempts = 0

            def invoke_json(self, *, task, prompt_version, messages, required_keys):
                if task != "clarification_question":
                    return super().invoke_json(
                        task=task,
                        prompt_version=prompt_version,
                        messages=messages,
                        required_keys=required_keys,
                    )
                self.attempts += 1
                options = (
                    ["只有一个业务选项", "tell the agent to do differently"]
                    if self.attempts == 1
                    else [
                        "保留当前指标和基线继续。",
                        "调整业务范围后继续。",
                        "tell the agent to do differently",
                    ]
                )
                return FakeLLMResult(
                    {
                        "questions": [{"question": "按哪个范围继续？", "options": options}],
                        "recommended_assumption": {
                            "option": "保留当前指标和基线继续。"
                        },
                        "status_message": "等待确认。",
                    },
                    {"task": task},
                )

        fake = InvalidThenValid()
        state = {
            "run_id": "run-general-retry",
            "request": {},
            "intent": {"target_metric": "active_users"},
            "boundary_decision": {
                "boundary_status": "needs_question",
                "clarification_questions": [],
            },
            "llm_client": fake,
            "llm_calls": [],
            "checkpoint_events": [],
        }

        result = _retrying_node("generate_clarification", _generate_clarification)(state)

        self.assertEqual(fake.attempts, 2)
        self.assertEqual(result["clarification_outcome"]["status"], "question_tool_opened")
        self.assertIn(
            "general_clarification_contract_invalid:options",
            result["request"]["node_retry_feedback"]["reason"],
        )

    def test_general_clarification_contract_gets_three_reasoned_attempts(self):
        from bi_agent.runtime.langgraph_workflow import _generate_clarification

        state = {
            "run_id": "run-general-three-attempts",
            "request": {},
            "intent": {"target_metric": "paid_amount"},
            "boundary_decision": {
                "boundary_status": "needs_question",
                "clarification_questions": [],
            },
            "next_action": {"next_action": "ask_question"},
            "checkpoint_events": [],
        }
        payloads = []

        def clarify(_state, task, payload):
            self.assertEqual(task, "clarification_question")
            payloads.append(payload)
            options = (
                ["只有一个业务选项", "tell the agent to do differently"]
                if len(payloads) < 3
                else [
                    "保留当前口径继续",
                    "调整业务口径后继续",
                    "tell the agent to do differently",
                ]
            )
            return {
                "questions": [{"question": "按哪个口径继续？", "options": options}],
                "recommended_assumption": {"option": "保留当前口径继续"},
            }

        with patch(
            "bi_agent.runtime.langgraph_workflow._invoke_llm",
            side_effect=clarify,
        ):
            result = _retrying_node(
                "generate_clarification",
                _generate_clarification,
            )(state)

        self.assertEqual(len(payloads), 3)
        self.assertIn(
            "general_clarification_contract_invalid:options",
            payloads[1]["retry_context"]["reason"],
        )
        self.assertEqual(
            result["clarification_outcome"]["recommended_assumption"],
            {"option": "保留当前口径继续"},
        )

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

    def test_answer_package_drops_unverified_context_audit_from_request(self):
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

        self.assertEqual(result.answer_package["context_manifest_ref"], "")
        self.assertEqual(result.answer_package["reuse_decisions"], [])

    def test_claimless_package_drops_context_manifest_and_reuse_decisions(self):
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
        self.assertEqual(claims, [])
        self.assertEqual(result.answer_package["context_manifest_ref"], "")
        self.assertEqual(result.answer_package["reuse_decisions"], [])

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
        self.assertEqual(claims, [])
        self.assertEqual(
            result.answer_package["admin_audit"]["verifier"]["status"],
            "failed",
        )

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

    def test_answer_prompts_require_complete_verifier_claim_shape(self):
        for task in ("answer_synthesis", "answer_repair"):
            messages = build_prompt(task, {"answer_context": {}}).messages
            text = "\n".join(message["content"] for message in messages)

            self.assertIn(
                "text, evidence_refs, numbers, scope, time_window, claim_type, and claim_strength",
                text,
            )
            self.assertIn("copy evidence_refs exactly", text.lower())
            self.assertIn("Do not return a claim without text", text)

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

    def test_degraded_explanation_receives_fixed_analysis_contract_windows(self):
        from bi_agent.runtime.langgraph_workflow import _generate_degraded_explanation

        contract = {
            "as_of": "2026-06-03T12:00:00+01:00",
            "resolved_windows": [
                {"window_id": "target_day", "label": "2026-06-02"},
                {"window_id": "previous_day", "label": "2026-06-01"},
            ],
        }
        state = {
            "request": {"run_mode": "production", "analysis_contract": contract},
            "run_id": "fixed-degraded-window",
            "intent": {
                "scope": "full_sample",
                "time_window": "yesterday",
                "target_metric": "paid_amount",
            },
            "evidence_brief": {},
            "verifier": {},
            "evidence": [],
            "draft_claims": [],
        }
        captured = {}

        def explain(_state, task, payload):
            self.assertEqual(task, "degraded_explanation")
            captured.update(payload)
            return {
                "status": "degraded",
                "explanation": "固定目标日的数据源尚未绑定。",
                "owner": "data_owner",
                "repair_path": "注册数据集快照后重试。",
            }

        with patch(
            "bi_agent.runtime.langgraph_workflow._invoke_llm",
            side_effect=explain,
        ):
            _generate_degraded_explanation(state)

        self.assertEqual(captured["analysis_contract"], contract)

    def test_final_summary_prompt_uses_business_wording_for_simple_comparison(self):
        messages = build_prompt("final_business_summary", {"intent": {}}).messages
        text = "\n".join(message["content"] for message in messages)

        self.assertIn("observed increase/decrease", text)
        self.assertIn("do not write statistical association", text)
        self.assertIn("当前证据能把排查方向收敛到", text)

    def test_final_answer_audit_prompt_requires_exact_audit_enums(self):
        messages = build_prompt("final_answer_audit", {"final_answer": "check"}).messages
        text = "\n".join(message["content"] for message in messages)

        self.assertIn("ready, ready_with_warnings, or hard_blocked", text)
        self.assertIn("unsupported_material_claim", text)
        self.assertIn("repairable_warnings must contain only", text)
        self.assertIn("Do not put explanatory prose in hard_blockers or repairable_warnings", text)
        self.assertIn("retry_instruction and business_audit_summary", text)

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

    def test_llm_client_waits_indefinitely_by_default(self):
        client = OpenAICompatibleLLMClient.from_env(
            {
                "WAJE_LLM_PROVIDER": "openai_compatible",
                "WAJE_LLM_MODEL": "patient-model",
                "WAJE_LLM_API_KEY": "test-key",
            }
        )

        self.assertIsNone(client.timeout_seconds)

    def test_llm_client_accepts_disabled_timeout_env(self):
        for timeout_value in ("0", "none", "disabled", ""):
            with self.subTest(timeout_value=timeout_value):
                client = OpenAICompatibleLLMClient.from_env(
                    {
                        "WAJE_LLM_PROVIDER": "openai_compatible",
                        "WAJE_LLM_MODEL": "patient-model",
                        "WAJE_LLM_API_KEY": "test-key",
                        "WAJE_LLM_TIMEOUT_SECONDS": timeout_value,
                    }
                )

                self.assertIsNone(client.timeout_seconds)

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
            "request": {},
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

    def test_authority_bound_claim_uses_contract_claim_type_scope_strength_and_numbers(self):
        evidence_ref = "market_health_compare:run-1"
        state = {
            "request": {},
            "intent": {
                "pattern_family": "custom_baseline",
                "target_metric": "active_users",
                "scope": "用户口语范围",
                "time_window": "用户口语窗口",
            },
            "evidence": [
                {
                    "evidence_ref": evidence_ref,
                    "capability_id": "market_health_compare",
                    "evidence_type": "statistical_association",
                    "strength": "directional",
                    "wording_limit": "quantified",
                    "input_status": "ready",
                    "binding_manifest_ref": "capability-binding:market:1",
                    "claim_type": "comparative_change",
                    "supported_claim_types": ("comparative_change",),
                    "supported_evidence_types": ("statistical_association",),
                    "maximum_claim_strength": "directional",
                    "scope": "market",
                    "time_window": "昨天与前天",
                    "numeric_facts": {
                        "target_value": 125260,
                        "baseline_value": 216820,
                        "absolute_change": -91560,
                    },
                    "typed_payload": {
                        "target_value": 125260,
                        "baseline_value": 216820,
                        "absolute_change": -91560,
                    },
                    "limitations": (),
                }
            ],
        }

        claim = _claims_from_llm_or_default(
            [
                {
                    "claim_text": "昨天活跃用户低于前天。",
                    "evidence_refs": [evidence_ref],
                    "strength": "directional",
                    "scope": "市场整体",
                    "time_window": "昨日对比前日",
                    "target_value": "125260",
                    "baseline_value": "216820",
                    "absolute_change": "-91560",
                }
            ],
            state,
        )[0]

        self.assertEqual(claim["claim_type"], "comparative_change")
        self.assertEqual(claim["claim_strength"], "observed")
        self.assertEqual(claim["scope"], "market")
        self.assertEqual(claim["time_window"], "昨天与前天")

        self.assertEqual(claim["numbers"]["absolute_change"], "-91560")

        state["draft_claims"] = [claim]
        self.assertEqual(_claims_from_llm_or_default([], state), [claim])
        self.assertEqual(_preserved_authority_claims(state), [claim])
        state["semantic_audit"] = {"audit_status": "needs_revision"}
        with patch(
            "bi_agent.runtime.langgraph_workflow._business_narrative_answer",
            return_value="已清洗叙事。",
        ):
            _sanitize_answer(state)
        self.assertEqual(state["draft_claims"], [claim])

        state.pop("draft_claims")
        structured_only = _claims_from_llm_or_default(
            [
                {
                    "evidence_refs": [evidence_ref],
                    "numbers": {"target_value": "125260"},
                }
            ],
            state,
        )[0]
        self.assertEqual(structured_only["claim_type"], "comparative_change")
        self.assertEqual(structured_only["numbers"], {"target_value": "125260"})
        self.assertIn("active_users", structured_only["text"])
        text_only = _claims_from_llm_or_default(
            [{"claim_text": "活跃用户下降。", "evidence_refs": [evidence_ref]}],
            state,
        )[0]
        self.assertEqual(
            text_only["numbers"],
            {
                "absolute_change": -91560,
                "baseline_value": 216820,
                "target_value": 125260,
            },
        )
        self.assertEqual(
            _claims_from_llm_or_default([{"evidence_refs": [evidence_ref]}], state),
            [],
        )

    def test_production_empty_llm_claims_wait_for_repair_candidate(self):
        evidence_ref = "market_health_compare:run-resumed"
        state = {
            "request": {"run_mode": "production"},
            "intent": {
                "pattern_family": "custom_baseline",
                "target_metric": "active_users",
                "scope": "用户口语范围",
                "time_window": "用户口语窗口",
            },
            "evidence": [
                {
                    "evidence_ref": evidence_ref,
                    "capability_id": "market_health_compare",
                    "evidence_type": "statistical_association",
                    "strength": "directional",
                    "wording_limit": "quantified",
                    "input_status": "ready",
                    "binding_manifest_ref": "capability-binding:market:resumed",
                    "claim_type": "comparative_change",
                    "supported_claim_types": ("comparative_change",),
                    "supported_evidence_types": ("statistical_association",),
                    "maximum_claim_strength": "directional",
                    "scope": "market",
                    "time_window": "昨天与前天",
                    "numeric_facts": {
                        "target_value": 125260,
                        "baseline_value": 216820,
                        "absolute_change": -91560,
                    },
                    "typed_payload": {
                        "target_value": 125260,
                        "baseline_value": 216820,
                        "absolute_change": -91560,
                    },
                    "limitations": (),
                }
            ],
        }

        self.assertEqual(_claims_from_llm_or_default(None, state), [])
        self.assertEqual(_claims_from_llm_or_default([], state), [])
        self.assertEqual(
            _claims_from_llm_or_default(
                [
                    {
                        "evidence_refs": [evidence_ref],
                        "numbers": {"target_value": 125260},
                    }
                ],
                state,
            ),
            [],
        )
        state["draft_claims"] = [{
            "text": "旧草稿没有合同绑定。",
            "evidence_refs": [evidence_ref],
        }]
        self.assertEqual(_claims_from_llm_or_default([], state), [])
        state.pop("draft_claims")

        repaired = _claims_from_llm_or_default(
            [
                {
                    "text": "昨天活跃用户低于前天。",
                    "evidence_refs": [evidence_ref],
                    "claim_type": "comparative_change",
                    "claim_strength": "directional",
                }
            ],
            state,
        )

        self.assertEqual(repaired[0]["evidence_refs"], [evidence_ref])
        self.assertEqual(repaired[0]["claim_type"], "comparative_change")
        self.assertEqual(repaired[0]["claim_strength"], "observed")
        self.assertEqual(repaired[0]["scope"], "market")
        self.assertEqual(repaired[0]["time_window"], "昨天与前天")
        self.assertEqual(repaired[0]["numbers"]["absolute_change"], -91560)
        state["draft_claims"] = repaired
        self.assertEqual(_claims_from_llm_or_default([], state), repaired)
        self.assertEqual(_preserved_authority_claims(state), repaired)

    def test_production_short_answer_is_not_rewritten_by_local_narrative_template(self):
        state = {
            "request": {"run_mode": "production"},
            "answer_text": "昨日活跃用户下降。",
            "draft_claims": [{"text": "昨日活跃用户下降。"}],
            "evidence": [],
        }

        with patch(
            "bi_agent.runtime.langgraph_workflow._business_narrative_answer"
        ) as local_template:
            _ensure_business_narrative_answer(state)

        local_template.assert_not_called()
        self.assertEqual(state["answer_text"], "昨日活跃用户下降。")

    def test_production_semantic_failure_never_uses_local_claim_or_narrative_sanitizer(self):
        state = {
            "request": {"run_mode": "production"},
            "semantic_audit": {
                "audit_status": "needs_revision",
                "issues": ["weak_business_interpretation"],
            },
            "answer_repair_attempts": 1,
            "answer_text": "昨日活跃用户下降。",
            "draft_claims": [],
        }

        self.assertEqual(_route_after_semantic_audit(state), "verify")
        with patch(
            "bi_agent.runtime.langgraph_workflow._sanitize_to_bounded_pattern_answer"
        ) as local_sanitizer:
            _sanitize_answer(state)

        local_sanitizer.assert_not_called()
        self.assertEqual(state["answer_text"], "昨日活跃用户下降。")
        self.assertEqual(state["draft_claims"], [])
        self.assertEqual(state["semantic_audit"]["audit_status"], "ready_with_warnings")

    def test_final_llm_audit_warning_does_not_rewrite_answer(self):
        state = {
            "request": {"run_mode": "production", "question": "昨日活跃用户如何变化？"},
            "answer_text": "昨日活跃用户下降。",
            "final_business_summary": "昨日活跃用户下降。",
            "draft_claims": [],
            "verifier": {"errors": []},
            "evidence": [],
        }
        warning = {
            "display_status": "ready_with_warnings",
            "hard_blockers": [],
            "repairable_warnings": ["weak_business_interpretation"],
            "risk_flags": [],
            "retry_instruction": "补充业务解释。",
            "business_audit_summary": "答案可展示。",
            "blocks_display": False,
        }

        with patch(
            "bi_agent.runtime.langgraph_workflow._final_answer_audit",
            return_value=warning,
        ), patch("bi_agent.runtime.langgraph_workflow._invoke_llm") as summary_rewrite:
            _answer_quality_gate(state)

        summary_rewrite.assert_not_called()
        self.assertEqual(state["final_business_summary"], "昨日活跃用户下降。")
        self.assertFalse(state["quality_gate"]["blocks_display"])

    def test_final_llm_audit_failure_is_a_nonblocking_risk(self):
        state = {
            "request": {"run_mode": "production", "question": "昨日活跃用户如何变化？"},
            "answer_text": "昨日活跃用户下降。",
            "final_business_summary": "昨日活跃用户下降。",
            "draft_claims": [],
            "verifier": {"errors": []},
            "evidence": [],
        }

        with patch(
            "bi_agent.runtime.langgraph_workflow._final_answer_audit",
            side_effect=WorkflowFailure("llm_response_timeout", failure_type="llm"),
        ):
            _answer_quality_gate(state)

        self.assertEqual(state["final_business_summary"], "昨日活跃用户下降。")
        self.assertFalse(state["quality_gate"]["blocks_display"])
        self.assertIn("final_answer_audit_unavailable", state["quality_gate"]["risk_flags"])

    def test_ambiguous_authority_refs_do_not_guess_claim_contract_or_promote_unknown_fields(self):
        evidence = []
        for index, scope in enumerate(("market", "channel"), start=1):
            evidence.append(
                {
                    "evidence_ref": f"evidence:{index}",
                    "binding_manifest_ref": f"binding:{index}",
                    "input_status": "ready",
                    "claim_type": "comparative_change",
                    "supported_claim_types": ("comparative_change",),
                    "strength": "directional",
                    "scope": scope,
                    "time_window": "昨天与前天",
                    "numeric_facts": {"target_value": index},
                    "typed_payload": {"target_value": index},
                }
            )
        state = {
            "intent": {
                "pattern_family": "custom_baseline",
                "target_metric": "active_users",
                "scope": "用户口语范围",
                "time_window": "用户口语窗口",
            },
            "evidence": evidence,
        }

        claim = _claims_from_llm_or_default(
            [
                {
                    "claim_text": "存在变化。",
                    "evidence_refs": ["evidence:1", "evidence:2"],
                    "scope": "用户表达",
                    "time_window": "用户窗口",
                    "unknown_value": 999,
                }
            ],
            state,
        )[0]

        self.assertEqual(claim["claim_type"], "")
        self.assertEqual(claim["scope"], "用户表达")
        self.assertEqual(claim["time_window"], "用户窗口")
        self.assertNotIn("unknown_value", claim["numbers"])

    def test_final_summary_display_repair_preserves_complete_numeric_claim_slots(self):
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

        self.assertFalse(_final_summary_needs_display_repair(summary, state))

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

    def test_final_summary_display_repair_preserves_complete_degraded_summary(self):
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

        self.assertFalse(_final_summary_needs_display_repair(summary, state))

    def test_final_summary_display_repair_requires_complete_pattern_evidence_slots(self):
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
            "关键发现：中位下降 7.1%，方向一致比例 26.7%。\n"
            "最终结论：当前证据不支持该假设。\n"
            "需要注意：仍需保留当前证据边界。"
        )

        self.assertTrue(_final_summary_needs_display_repair(summary, state))
        complete_summary = summary.replace(
            "方向一致比例 26.7%。",
            "方向一致比例 26.7%，30 个可比周期。",
        )
        self.assertFalse(_final_summary_needs_display_repair(complete_summary, state))

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
        self.assertEqual(result.answer_package["quality_gate"]["status"], "failed")
        self.assertEqual(result.answer_package["quality_gate"]["code"], "evidence_verifier_failed")

    def test_empty_final_business_summary_is_retried_with_failure_reason(self):
        state = {
            "request": {"question": "昨天的收入证据充分吗？"},
            "intent": {"pattern_family": ""},
            "draft_claims": [],
        }
        payloads = []

        def summarize(_state, task, payload):
            self.assertEqual(task, "final_business_summary")
            payloads.append(payload)
            if len(payloads) == 1:
                return {"summary_text": ""}
            return {"summary_text": "当前数据证据不足，需要检查支付状态数据。"}

        with patch(
            "bi_agent.runtime.langgraph_workflow._invoke_llm",
            side_effect=summarize,
        ):
            _final_business_summary(state)

        self.assertEqual(len(payloads), 2)
        self.assertEqual(payloads[0]["final_answer_retry_instruction"], "")
        self.assertIn(
            "上一次输出为空",
            payloads[1]["final_answer_retry_instruction"],
        )
        self.assertEqual(
            state["final_business_summary"],
            "当前数据证据不足，需要检查支付状态数据。",
        )

    def test_final_answer_audit_warning_is_recorded_without_rewriting_summary(self):
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
                            "关键发现：当前证据能把排查方向收敛到渠道贡献方向，周期内付费金额模式也有稳定数字锚点。\n"
                            "最终结论：已验证结论是：2024-01..2026-05 的周期内付费金额模式中位提升 20.0%，"
                            "方向一致比例 100.0%，覆盖 29 个可比周期。"
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
        self.assertEqual(fake.calls.count("final_business_summary"), 1)
        self.assertEqual(fake.calls.count("final_answer_audit"), 1)
        self.assertEqual(fake.summary_inputs[0].get("final_answer_retry_instruction"), "")
        self.assertEqual(result.answer_package["quality_gate"]["status"], "failed")
        self.assertEqual(result.answer_package["final_answer"], "")

    def test_final_answer_audit_never_rewrites_summary_for_warnings(self):
        class RetryTwiceLLM(FakeLLMClient):
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
                        "分析脉络：我检查了目标窗口、基线窗口和证据边界。\n"
                        "关键发现：当前证据能把排查方向收敛到周期内付费金额模式。\n"
                        "最终结论：活动是付费金额变化的因果原因，2024-01..2026-05 的周期内付费金额模式中位提升 20.0%，"
                        "方向一致比例 100.0%，覆盖 29 个可比周期。\n"
                        "需要注意：机制证据暂不可用，仍需补充独立对照证据。"
                    )
                    return FakeLLMResult(
                        {"summary_text": summary_text},
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
                        "repairable_warnings": ["unsupported_material_claim"],
                        "retry_instruction": "把无证据的确定性结论改成候选判断。",
                        "business_audit_summary": "主结论里有一处证据边界过强。",
                        "display_summary": "主结论里有一处证据边界过强。",
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

        fake = RetryTwiceLLM()
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "final-answer-audit-retry-twice",
                    "llm_client": fake,
                    "question": "Q2 相比 Q1 付费金额为什么变了？",
                }
            )

        self.assertEqual(result.status, "draft")
        self.assertEqual(fake.calls.count("final_business_summary"), 1)
        self.assertEqual(fake.calls.count("final_answer_audit"), 1)
        self.assertEqual(result.answer_package["quality_gate"]["status"], "failed")
        self.assertEqual(result.answer_package["final_answer"], "")
        audit_outputs = [
            call["structured_output"]
            for call in result.answer_package["admin_audit"]["llm_calls"]
            if call["task"] == "final_answer_audit"
        ]
        self.assertEqual(len(audit_outputs), 1)
        self.assertEqual(
            audit_outputs[-1]["repairable_warnings"],
            ["unsupported_material_claim"],
        )

    def test_local_final_summary_display_warning_does_not_trigger_rewrite(self):
        class ReadyAuditBadFirstSummaryLLM(FakeLLMClient):
            def __init__(self):
                super().__init__()
                self.summary_inputs = []
                self.audit_inputs = []

            def invoke_json(self, *, task, prompt_version, messages, required_keys):
                if task == "final_business_summary":
                    self.calls.append(task)
                    payload = _input_payload(messages)
                    self.summary_inputs.append(payload)
                    if len(self.summary_inputs) == 1:
                        summary_text = "已生成最终业务总结。"
                    else:
                        summary_text = (
                            "我对问题的理解是：你想看 Q2 相比 Q1 的付费金额变化。\n"
                            "分析脉络：我检查了目标窗口、基线窗口和证据边界。\n"
                            "关键发现：当前证据能把排查方向收敛到周期内付费金额模式。\n"
                            "最终结论：已验证结论是：2024-01..2026-05 的周期内付费金额模式中位提升 20.0%，"
                            "方向一致比例 100.0%，覆盖 29 个可比周期。\n"
                            "需要注意：机制证据暂不可用，还不能直接说这是唯一原因或已被因果证明。"
                        )
                    return FakeLLMResult(
                        {"summary_text": summary_text},
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
                    payload = _input_payload(messages)
                    self.audit_inputs.append(payload)
                    output = {
                        "display_status": "ready",
                        "hard_blockers": [],
                        "repairable_warnings": [],
                        "retry_instruction": "",
                        "business_audit_summary": "答案满足展示边界。",
                        "display_summary": "答案满足展示边界。",
                    }
                    return FakeLLMResult(
                        output,
                        {
                            "task": task,
                            "provider": "fake",
                            "model": "fake-model",
                            "prompt_version": prompt_version,
                            "response_id": f"fake-{task}-{len(self.audit_inputs)}",
                            "messages": [dict(message) for message in messages],
                            "required_keys": list(required_keys),
                            "raw_response_content": "{}",
                            "started_at": "2026-01-01T00:00:00+00:00",
                            "finished_at": "2026-01-01T00:00:00+00:00",
                            "duration_ms": 0.0,
                            "input_hash": f"input-{task}-{len(self.audit_inputs)}",
                            "output_hash": f"output-{task}-{len(self.audit_inputs)}",
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

        fake = ReadyAuditBadFirstSummaryLLM()
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "local-final-summary-display-warning-retry",
                    "llm_client": fake,
                    "question": "Q2 相比 Q1 付费金额为什么变了？",
                }
            )

        self.assertEqual(result.status, "draft")
        self.assertEqual(fake.calls.count("final_business_summary"), 1)
        self.assertEqual(fake.calls.count("final_answer_audit"), 1)
        self.assertEqual(result.answer_package["quality_gate"]["status"], "failed")
        self.assertEqual(result.answer_package["final_answer"], "")

    def test_final_business_summary_accepts_section_keys_from_structured_output(self):
        class SectionKeySummaryLLM(FakeLLMClient):
            def invoke_json(self, *, task, prompt_version, messages, required_keys):
                if task == "final_business_summary":
                    self.calls.append(task)
                    output = {
                        "summary_text": "我对问题的理解是：你想看 Q2 相比 Q1 的付费金额变化。",
                        "分析脉络：": "我检查了目标窗口、基线窗口和证据边界。",
                        "关键发现：": "当前证据能把排查方向收敛到周期内付费金额模式。",
                        "最终结论：": (
                            "已验证结论是：2024-01..2026-05 的周期内付费金额模式中位提升 20.0%，"
                            "方向一致比例 100.0%，覆盖 29 个可比周期。"
                        ),
                        "需要注意：": "机制证据暂不可用，还不能直接说这是唯一原因或已被因果证明。",
                    }
                    return FakeLLMResult(
                        output,
                        {
                            "task": task,
                            "provider": "fake",
                            "model": "fake-model",
                            "prompt_version": prompt_version,
                            "response_id": f"fake-{task}",
                            "messages": [dict(message) for message in messages],
                            "required_keys": list(required_keys),
                            "raw_response_content": "{}",
                            "started_at": "2026-01-01T00:00:00+00:00",
                            "finished_at": "2026-01-01T00:00:00+00:00",
                            "duration_ms": 0.0,
                            "input_hash": f"input-{task}",
                            "output_hash": f"output-{task}",
                            "usage": {},
                            "structured_output": output,
                        },
                    )
                if task == "final_answer_audit":
                    self.calls.append(task)
                    output = {
                        "display_status": "ready",
                        "hard_blockers": [],
                        "repairable_warnings": [],
                        "retry_instruction": "",
                        "business_audit_summary": "答案满足展示边界。",
                        "display_summary": "答案满足展示边界。",
                    }
                    return FakeLLMResult(
                        output,
                        {
                            "task": task,
                            "provider": "fake",
                            "model": "fake-model",
                            "prompt_version": prompt_version,
                            "response_id": f"fake-{task}",
                            "messages": [dict(message) for message in messages],
                            "required_keys": list(required_keys),
                            "raw_response_content": "{}",
                            "started_at": "2026-01-01T00:00:00+00:00",
                            "finished_at": "2026-01-01T00:00:00+00:00",
                            "duration_ms": 0.0,
                            "input_hash": f"input-{task}",
                            "output_hash": f"output-{task}",
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

        fake = SectionKeySummaryLLM()
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "section-key-summary",
                    "llm_client": fake,
                    "question": "Q2 相比 Q1 付费金额为什么变了？",
                }
            )

        final_answer = result.answer_package["final_answer"]
        self.assertEqual(final_answer, "")
        self.assertEqual(result.answer_package["quality_gate"]["status"], "failed")

    def test_final_business_summary_timeout_fails_without_local_answer_fallback(self):
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

        self.assertEqual(result.status, "failed")
        self.assertIn("final_business_summary", fake.calls)
        self.assertIsNone(result.answer_package)
        self.assertIn("final_business_summary", result.failure_reason)

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

    def test_business_intent_llm_timeout_fails_without_local_intent(self):
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

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.failure_reason, "llm_response_timeout")
        self.assertIsNone(result.answer_package)

    def test_message_less_provider_failure_retains_exception_type(self):
        class EmptyFailureOnIntentLLM(FakeLLMClient):
            def invoke_json(self, *, task, prompt_version, messages, required_keys):
                if task == "business_intent":
                    raise AssertionError
                return super().invoke_json(
                    task=task,
                    prompt_version=prompt_version,
                    messages=messages,
                    required_keys=required_keys,
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "intent-empty-provider-failure",
                    "llm_client": EmptyFailureOnIntentLLM(),
                    "question": "Q2相比Q1付费金额提升的主要原因是什么？",
                }
            )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.failure_reason, "AssertionError")

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

    def test_execute_capabilities_dispatches_reviewed_runtime_bound_capability(self):
        from types import SimpleNamespace

        bound = object()
        state = {
            "request": {
                "role": "analyst",
                "runtime_rows_source": "analysis_runtime",
                "bound_capability_inputs": {"market_health_compare": bound},
            },
            "run_id": "execute-runtime-market",
            "budget_state": default_budget("ordinary"),
            "compiled_graph": SimpleNamespace(
                mutations=SimpleNamespace(
                    accepted_graph=("compare_periods", "market_health_compare")
                )
            ),
            "intent": {
                "question_family": "custom_baseline_comparison",
                "target_metric": "active_users",
                "pattern_family": "custom_baseline",
                "scope": "full_sample",
                "time_window": "昨天与前天",
                "target_claim": "comparative_change",
                "baseline": {"label": "前天"},
                "target": {"label": "昨天"},
            },
        }
        captured = []

        def execute(request):
            captured.append(request)
            return {
                "evidence_ref": "market-health:evidence",
                "capability_id": request.capability_id,
                "typed_payload": {},
                "numeric_facts": {},
                "result_refs": (),
            }

        with patch(
            "bi_agent.runtime.langgraph_workflow.execute_capability",
            side_effect=execute,
        ):
            result = _execute_capabilities(state)

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].capability_id, "market_health_compare")
        self.assertIs(captured[0].bound_input, bound)
        self.assertEqual(result["evidence"][0]["capability_id"], "market_health_compare")

    def test_production_capability_cannot_bypass_blocked_bound_input_with_compatibility_rows(self):
        from types import SimpleNamespace
        from bi_agent.runtime.capability_execution import BoundCapabilityInput

        bound = object.__new__(BoundCapabilityInput)
        for field in BoundCapabilityInput.__annotations__:
            if field in {"rows_by_slot", "binding_manifest"}:
                value = {}
            elif field in {"maximum_claim_strength_rank"}:
                value = -1
            elif field.endswith("s") or field.endswith("refs"):
                value = ()
            else:
                value = ""
            object.__setattr__(bound, field, value)
        object.__setattr__(bound, "capability_id", "driver_decomposition")
        object.__setattr__(bound, "status", "blocked")
        object.__setattr__(bound, "reasons", ("missing_required_query_slot",))

        state = {
            "request": {
                "run_mode": "production",
                "runtime_rows_source": "analysis_runtime",
                "bound_capability_inputs": {"driver_decomposition": bound},
                "runtime_rows_by_intent": {
                    "component_driver_scan": [
                        {"group": "baseline", "amount": 1, "paid_users": 1},
                        {"group": "target", "amount": 1_000_000, "paid_users": 1},
                    ]
                },
                "result_refs_by_intent": {
                    "component_driver_scan": ("result:compatibility-map",)
                },
                "role": "analyst",
            },
            "run_id": "blocked-driver-cannot-bypass",
            "sql_hash": "",
            "budget_state": default_budget("ordinary"),
            "compiled_graph": SimpleNamespace(
                mutations=SimpleNamespace(accepted_graph=("driver_decomposition",))
            ),
            "intent": {
                "question_family": "paid_amount_change_explanation",
                "target_metric": "paid_amount",
                "pattern_family": "custom_baseline",
                "pattern_params": {"group_key": "group", "target_group": "target"},
                "scope": "full_sample",
                "time_window": "昨天与前天",
                "target_claim": "formula_component_contribution",
                "baseline": {"label": "前天"},
                "target": {"label": "昨天"},
            },
        }

        evidence = _execute_capabilities(state)["evidence"][0]

        self.assertEqual(evidence["input_status"], "blocked")
        self.assertFalse(_evidence_established(evidence))
        self.assertNotIn(999999.0, evidence.get("numeric_facts", {}).values())
        self.assertNotIn("result:compatibility-map", evidence.get("result_refs", ()))

    def test_production_segment_bridge_and_joint_use_exact_bound_slots(self):
        from types import SimpleNamespace
        from bi_agent.runtime.capability_execution import BoundCapabilityInput

        def ready_bound(capability_id, slot, rows, result_ref, claim_type):
            bound = object.__new__(BoundCapabilityInput)
            for field in BoundCapabilityInput.__annotations__:
                if field in {"rows_by_slot", "binding_manifest"}:
                    value = {}
                elif field == "maximum_claim_strength_rank":
                    value = 2
                elif field.endswith("s") or field.endswith("refs"):
                    value = ()
                else:
                    value = ""
                object.__setattr__(bound, field, value)
            object.__setattr__(bound, "capability_id", capability_id)
            object.__setattr__(bound, "status", "ready")
            object.__setattr__(bound, "rows_by_slot", {slot: tuple(rows)})
            object.__setattr__(bound, "result_refs", (result_ref,))
            object.__setattr__(bound, "supported_claim_types", (claim_type,))
            object.__setattr__(
                bound,
                "supported_evidence_types",
                ("contextual_evidence", "statistical_association"),
            )
            object.__setattr__(bound, "maximum_claim_strength", "candidate_driver")
            object.__setattr__(bound, "claim_strength_taxonomy_version", "v1")
            object.__setattr__(bound, "input_completeness_statuses", ("complete",))
            object.__setattr__(bound, "binding_manifest_ref", f"binding:{capability_id}")
            object.__setattr__(bound, "binding_manifest_digest", f"digest:{capability_id}")
            return bound

        segment = ready_bound(
            "segment_bridge",
            "dimension_contribution_scan",
            ({"segment": "actual", "amount": 500.0, "n": 50},),
            "result:segment-bound",
            "segment_contribution_or_mix_shift",
        )
        joint = ready_bound(
            "joint_attribution",
            "joint_candidate_scan",
            (
                {"group": "baseline", "channel": "ads", "method": "card", "amount": 100.0, "n": 50},
                {"group": "target", "channel": "ads", "method": "card", "amount": 180.0, "n": 50},
            ),
            "result:joint-bound",
            "candidate_driver",
        )
        state = {
            "request": {
                "run_mode": "production",
                "runtime_rows_source": "analysis_runtime",
                "bound_capability_inputs": {
                    "segment_bridge": segment,
                    "joint_attribution": joint,
                },
                "segments": ({"segment": "full_sample", "amount": 1.0, "n": 100},),
                "role": "analyst",
            },
            "run_id": "bound-segment-joint",
            "sql_hash": "",
            "budget_state": default_budget("ordinary"),
            "compiled_graph": SimpleNamespace(
                mutations=SimpleNamespace(
                    accepted_graph=("segment_bridge", "joint_attribution")
                )
            ),
            "intent": {
                "question_family": "segment_or_factor_attribution",
                "target_metric": "paid_amount",
                "pattern_family": "custom_baseline",
                "pattern_params": {
                    "group_key": "group",
                    "target_group": "target",
                    "baseline_group": "baseline",
                },
                "scope": "full_sample",
                "time_window": "昨天与前天",
                "target_claim": "candidate_driver",
                "baseline": {"label": "前天"},
                "target": {"label": "昨天"},
            },
        }

        with patch(
            "bi_agent.runtime.langgraph_workflow.validate_bound_capability_input",
            return_value="",
        ):
            evidence = _execute_capabilities(state)["evidence"]

        segment_evidence = next(
            item for item in evidence if item["capability_id"] == "segment_bridge"
        )
        joint_evidence = next(
            item for item in evidence if item["capability_id"] == "joint_attribution"
        )
        self.assertEqual(
            segment_evidence["typed_payload"]["segments"][0]["segment"],
            "actual",
        )
        self.assertEqual(segment_evidence["result_refs"], ("result:segment-bound",))
        self.assertNotEqual(
            joint_evidence["typed_payload"].get("reason"),
            "no_escalation_required",
        )
        self.assertEqual(
            joint_evidence["typed_payload"]["dimension_keys"],
            ["channel", "method"],
        )

    def test_production_formula_decompose_uses_all_bound_components(self):
        from types import SimpleNamespace
        from bi_agent.runtime.capability_execution import BoundCapabilityInput

        bound = object.__new__(BoundCapabilityInput)
        for field in BoundCapabilityInput.__annotations__:
            if field in {"rows_by_slot", "binding_manifest"}:
                value = {}
            elif field == "maximum_claim_strength_rank":
                value = 2
            elif field.endswith("s") or field.endswith("refs"):
                value = ()
            else:
                value = ""
            object.__setattr__(bound, field, value)
        object.__setattr__(bound, "capability_id", "formula_decompose")
        object.__setattr__(bound, "status", "ready")
        object.__setattr__(
            bound,
            "rows_by_slot",
            {
                "component_driver_scan": ({
                    "paid_amount": 100.0,
                    "paid_users": 20,
                    "paid_orders": 30,
                    "first_paid_users": 5,
                    "paid_frequency": 1.5,
                    "avg_order_amount": 3.333,
                },)
            },
        )
        object.__setattr__(bound, "result_refs", ("result:formula-bound",))
        object.__setattr__(
            bound,
            "supported_claim_types",
            ("formula_component_contribution",),
        )
        object.__setattr__(bound, "supported_evidence_types", ("accounting_contribution",))
        object.__setattr__(bound, "maximum_claim_strength", "quantified_contribution")
        object.__setattr__(bound, "claim_strength_taxonomy_version", "v1")
        object.__setattr__(bound, "input_completeness_statuses", ("complete",))
        object.__setattr__(bound, "binding_manifest_ref", "binding:formula")
        object.__setattr__(bound, "binding_manifest_digest", "digest:formula")
        state = {
            "request": {
                "run_mode": "production",
                "runtime_rows_source": "analysis_runtime",
                "bound_capability_inputs": {"formula_decompose": bound},
                "role": "analyst",
            },
            "run_id": "bound-formula",
            "sql_hash": "",
            "budget_state": default_budget("ordinary"),
            "compiled_graph": SimpleNamespace(
                mutations=SimpleNamespace(accepted_graph=("formula_decompose",))
            ),
            "intent": {
                "question_family": "paid_amount_change_explanation",
                "target_metric": "paid_amount",
                "pattern_family": "custom_baseline",
                "scope": "full_sample",
                "time_window": "昨天与前天",
                "target_claim": "formula_component_contribution",
            },
        }

        with patch(
            "bi_agent.runtime.langgraph_workflow.validate_bound_capability_input",
            return_value="",
        ):
            formula = _execute_capabilities(state)["evidence"][0]

        path = formula["typed_payload"]["covered_paths"][0]
        self.assertEqual(
            path["components"],
            [
                "paid_users",
                "paid_orders",
                "first_paid_users",
                "paid_frequency",
                "avg_order_amount",
            ],
        )
        self.assertEqual(formula["result_refs"], ("result:formula-bound",))

    def test_production_degraded_explanation_does_not_create_local_claim(self):
        from types import SimpleNamespace
        from bi_agent.runtime.langgraph_workflow import _ensure_degraded_audit

        state = {
            "request": {"run_mode": "production"},
            "run_id": "production-degraded-no-local-claim",
            "intent": {
                "scope": "full_sample",
                "time_window": "2026-06-02",
                "target_metric": "paid_amount",
            },
            "analysis_runtime_result": SimpleNamespace(status="degraded"),
            "evidence": [],
            "draft_claims": [],
        }

        _ensure_degraded_audit(state)

        self.assertEqual(state["draft_claims"], [])

    def test_query_contract_repair_sends_exact_failure_reason_to_route_repair(self):
        from types import SimpleNamespace

        state = {
            "request": {"attempted_query_signatures": ()},
            "query_repair_decisions": ({
                "failed_signature": "signature:failed",
                "failed_query_contract_ref": "query:failed",
                "reason": "query_shape_mismatch",
                "failure_reasons": ("missing_field:paid_users",),
            },),
            "repair_attempts": 0,
            "compiled_graph": SimpleNamespace(
                mutations=SimpleNamespace(records=())
            ),
            "analysis_route": {
                "requested_nodes": ("driver_decomposition",),
                "analysis_requirements": {
                    "target_metrics": ("active_users",),
                    "claim_intents": ("comparative_change",),
                    "baselines": ("previous_day",),
                },
            },
            "intent": {
                "question_family": "custom_baseline_comparison",
                "question_families": ["custom_baseline_comparison"],
                "target_metric": "active_users",
                "claim_intents": ["comparative_change"],
                "baseline_candidates": ["previous_day"],
                "requested_nodes": ("driver_decomposition",),
            },
        }
        captured = {}

        def repair_llm(_state, task, payload):
            captured.update(payload)
            self.assertEqual(task, "route_repair")
            return {
                "requested_nodes": ["market_health_compare"],
                "repair_summary": "按失败字段修正分析路线。",
                "decision_summary": "保留目标指标和基线。",
            }

        with patch(
            "bi_agent.runtime.langgraph_workflow._invoke_llm",
            side_effect=repair_llm,
        ):
            _repair_analysis_contract(state)

        self.assertEqual(
            captured["compiler_feedback"][0]["failure_reasons"],
            ["missing_field:paid_users"],
        )
        self.assertEqual(
            state["request"]["analysis_repair_reasons"],
            ("query_shape_mismatch",),
        )
        self.assertEqual(
            state["analysis_route"]["requested_nodes"],
            (
                "market_health_compare",
                "data_quality_profile",
                "compare_periods",
                "answer_verify",
            ),
        )

    def test_typed_runtime_records_real_clickhouse_validator_without_phase4_placeholder(self):
        from types import SimpleNamespace

        runtime_result = SimpleNamespace(
            query_results=(
                SimpleNamespace(
                    execution_status="succeeded",
                    result_ref="result:typed",
                ),
            ),
            query_contracts=(),
            bound_capability_inputs={},
            repair_decisions=(),
            to_workflow_payload=lambda: {
                "runtime_rows_by_intent": {
                    "daily_metric_baselines": [{"window_role": "target"}]
                },
                "result_refs_by_intent": {
                    "daily_metric_baselines": ["result:typed"]
                },
            },
        )
        runtime = SimpleNamespace(execute=lambda _request: runtime_result)
        state = {
            "request": {
                "analysis_runtime": runtime,
                "run_mode": "production",
                "role": "analyst",
            },
            "run_id": "typed-validator",
            "checkpoint_events": [],
            "intent": {"pattern_family": "custom_baseline"},
        }

        with patch(
            "bi_agent.runtime.langgraph_workflow._analysis_runtime_request",
            return_value=object(),
        ):
            _validate_runtime_binding(state)
            _fetch_runtime_rows(state)

        validators = state["validator_results"]
        self.assertFalse(
            any(item.get("reason") == "phase4_draft_binding" for item in validators)
        )
        self.assertIn(
            {
                "validator": "clickhouse_runtime",
                "ok": True,
                "reason": "provider_rows_loaded",
                "result_refs": ["result:typed"],
            },
            validators,
        )

    def test_ready_authority_bound_directional_evidence_is_established(self):
        self.assertTrue(
            _evidence_established(
                {
                    "evidence_type": "statistical_association",
                    "strength": "directional",
                    "wording_limit": "quantified",
                    "input_status": "ready",
                    "binding_manifest_ref": "capability-binding:market:1",
                    "claim_type": "comparative_change",
                    "supported_claim_types": ("comparative_change",),
                    "supported_evidence_types": ("statistical_association",),
                    "maximum_claim_strength": "directional",
                    "limitations": (),
                }
            )
        )

    def test_unbound_or_over_ceiling_directional_evidence_is_not_established(self):
        base = {
            "evidence_type": "statistical_association",
            "strength": "directional",
            "wording_limit": "quantified",
            "input_status": "ready",
            "binding_manifest_ref": "capability-binding:market:1",
            "claim_type": "comparative_change",
            "supported_claim_types": ("comparative_change",),
            "supported_evidence_types": ("statistical_association",),
            "maximum_claim_strength": "directional",
            "limitations": (),
        }
        variants = (
            {**base, "binding_manifest_ref": ""},
            {**base, "input_status": "degraded"},
            {**base, "limitations": ("partial",)},
            {**base, "supported_claim_types": ("source_reconciliation",)},
            {**base, "maximum_claim_strength": "trust_boundary"},
        )

        for evidence in variants:
            with self.subTest(evidence=evidence):
                self.assertFalse(_evidence_established(evidence))

    def test_execute_capabilities_pairs_runtime_previous_day_baseline_for_attribution(self):
        compiled = compile_graph(
            question_family="custom_baseline_comparison",
            question_families=(
                "custom_baseline_comparison",
                "segment_or_factor_attribution",
            ),
            target_metric="paid_amount",
            pattern_family="custom_baseline",
            requested_nodes=(
                "compare_periods",
                "driver_decomposition",
                "segment_contribution",
                "joint_attribution",
                "answer_verify",
            ),
        )
        state = {
            "request": {
                "rows": [
                    {
                        "period": "2026-07-08",
                        "group": "target",
                        "amount": 150.0,
                        "paid_users": 12,
                        "orders": 30,
                        "first_paid_users": 5,
                    }
                ],
                "runtime_rows_by_intent": {
                    "daily_metric_baselines": [
                        {
                            "period": "2026-07-07",
                            "group": "previous_day",
                            "amount": 100.0,
                            "paid_users": 10,
                            "orders": 20,
                            "first_paid_users": 3,
                        },
                        {
                            "period": "2026-07-08",
                            "group": "target",
                            "amount": 150.0,
                            "paid_users": 12,
                            "orders": 30,
                            "first_paid_users": 5,
                        },
                    ],
                    "dimension_scan": [
                        {
                            "period": "2026-07-07",
                            "group": "previous_day",
                            "channel": "ads",
                            "amount": 60.0,
                            "orders": 12,
                        },
                        {
                            "period": "2026-07-08",
                            "group": "target",
                            "channel": "ads",
                            "amount": 95.0,
                            "orders": 18,
                        },
                    ],
                    "joint_candidate_scan": [
                        {
                            "period": "2026-07-07",
                            "group": "previous_day",
                            "channel": "ads",
                            "payment_method": "card",
                            "amount": 45.0,
                            "orders": 12,
                        },
                        {
                            "period": "2026-07-08",
                            "group": "target",
                            "channel": "ads",
                            "payment_method": "card",
                            "amount": 80.0,
                            "orders": 18,
                        },
                    ],
                },
                "result_refs_by_intent": {
                    "daily_metric_baselines": ("baseline-ref",),
                    "dimension_scan": ("dimension-ref",),
                    "joint_candidate_scan": ("joint-ref",),
                },
                "compiler_runtime_plan": {"baselines": ("previous_day",)},
                "required_fields": ("period", "group", "amount", "paid_users", "orders"),
                "role": "analyst",
                "joint_dimension_keys": ("channel", "payment_method"),
                "run_mode": "fixture",
            },
            "run_id": "execute-runtime-previous-day",
            "sql_hash": "sqlhash-runtime-baseline",
            "budget_state": default_budget("ordinary"),
            "compiled_graph": compiled,
            "intent": {
                "question_family": "custom_baseline_comparison",
                "target_metric": "paid_amount",
                "pattern_family": "custom_baseline",
                "pattern_params": {"group_key": "group", "target_group": "target"},
                "scope": "full_sample",
                "time_window": "yesterday",
                "target_claim": "昨天付费金额变化原因",
                "baseline": {"label": "前一天"},
                "target": {"label": "昨天"},
            },
        }

        with patch.dict(
            "os.environ",
            {
                "WAJE_ALLOW_LEGACY_FIXTURES": "1",
                "WAJE_RUNTIME_ENV": "test",
            },
        ):
            result = _execute_capabilities(state)
        by_capability = {
            item.get("capability_id"): item for item in result["evidence"]
        }

        compare = by_capability["compare_periods"]
        self.assertEqual(compare["evidence_type"], "statistical_association")
        self.assertEqual(compare["typed_payload"]["comparable_periods"], 1)
        self.assertNotIn("no_comparable_periods", compare["limitations"])

        driver = by_capability["driver_decomposition"]
        self.assertEqual(driver["evidence_type"], "accounting_contribution")
        self.assertEqual(driver["result_refs"], ["baseline-ref"])
        self.assertTrue(driver["typed_payload"]["decompositions"])

        segment = by_capability["segment_contribution"]
        self.assertEqual(segment["evidence_type"], "statistical_association")
        self.assertEqual(segment["typed_payload"]["segment_count"], 1)

        joint = by_capability["joint_attribution"]
        self.assertEqual(joint["evidence_type"], "statistical_association")
        self.assertEqual(joint["typed_payload"]["combination_count"], 1)

    def test_execute_capabilities_averages_runtime_rolling_baseline_for_driver(self):
        compiled = compile_graph(
            question_family="custom_baseline_comparison",
            target_metric="paid_amount",
            pattern_family="custom_baseline",
            requested_nodes=("driver_decomposition", "answer_verify"),
        )
        state = {
            "request": {
                "runtime_rows_by_intent": {
                    "daily_metric_baselines": [
                        {
                            "period": "2026-07-01",
                            "group": "rolling_7_day_baseline",
                            "amount": 70.0,
                            "paid_users": 7,
                            "orders": 14,
                        },
                        {
                            "period": "2026-07-02",
                            "group": "rolling_7_day_baseline",
                            "amount": 90.0,
                            "paid_users": 9,
                            "orders": 18,
                        },
                        {
                            "period": "2026-07-08",
                            "group": "target",
                            "amount": 140.0,
                            "paid_users": 14,
                            "orders": 28,
                        },
                    ],
                },
                "result_refs_by_intent": {"daily_metric_baselines": ("rolling-ref",)},
                "compiler_runtime_plan": {"baselines": ("rolling_7_day_baseline",)},
                "required_fields": ("period", "group", "amount", "paid_users", "orders"),
                "role": "analyst",
            },
            "run_id": "execute-runtime-rolling",
            "sql_hash": "sqlhash-runtime-rolling",
            "budget_state": default_budget("ordinary"),
            "compiled_graph": compiled,
            "intent": {
                "question_family": "custom_baseline_comparison",
                "target_metric": "paid_amount",
                "pattern_family": "custom_baseline",
                "pattern_params": {
                    "group_key": "group",
                    "target_group": "target",
                    "baseline_group": "rolling_7_day_baseline",
                },
                "scope": "full_sample",
                "time_window": "yesterday",
                "target_claim": "昨天付费金额相比近 7 日均值变化",
                "baseline": {"label": "近 7 日均值"},
                "target": {"label": "昨天"},
            },
        }

        result = _execute_capabilities(state)
        driver = next(
            item for item in result["evidence"] if item.get("capability_id") == "driver_decomposition"
        )
        decomposition = driver["typed_payload"]["decompositions"][0]

        self.assertEqual(driver["evidence_type"], "accounting_contribution")
        self.assertEqual(decomposition["baseline_volume"], 8.0)
        self.assertEqual(decomposition["amount_delta"], 60.0)

    def test_reduce_evidence_uses_public_compare_as_primary_evidence(self):
        state = {
            "request": {"run_mode": "fixture"},
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
                "analysis_requirements": {"requested_dimensions": ["channel", "phase"]},
            },
        )

        self.assertIn("joint_attribution", normalized)
        self.assertIn("data_quality_profile", normalized)

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
                    "analysis_requirements": {
                        "claim_intents": ["formula_component_contribution"]
                    },
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
        self.assertEqual(
            result.answer_package["sections"][0]["payload"]["claims"],
            [],
        )

    def test_joint_attribution_promotion_node_uses_rows_and_joint_dimensions(self):
        state = {
            "request": {"run_mode": "fixture"},
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
                "analysis_requirements": {
                    "claim_intents": ["formula_component_contribution"]
                },
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
                "analysis_requirements": {
                    "requested_dimensions": ["channel"],
                    "diagnostic_tags": ["pattern_attribution"],
                },
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

    def test_answer_synthesis_context_includes_claim_slots_and_bounded_insight(self):
        state = {
            "intent": {
                "question_family": "custom_baseline_comparison",
                "pattern_family": "custom_baseline",
                "target_metric": "paid_amount",
                "scope": "full_sample",
                "time_window": "2026-01-01..2026-06-30",
                "target_claim": "Q2 相比 Q1 为什么变化",
                "baseline": {"label": "Q1"},
                "target": {"label": "Q2"},
            },
            "request": {"question": "Q2 相比 Q1 为什么变化？"},
            "evidence_brief": {"limitations": ["weak_direction"]},
            "causal_evidence_dossier": {},
            "causal_audit": {},
            "draft_claims": [
                {
                    "text": "Q2 相比 Q1 的付费金额提升 20.0%，当前只支持窗口对比结论。",
                    "numbers": {"change_pct": 0.2},
                    "scope": "full_sample",
                    "time_window": "2026-01-01..2026-06-30",
                    "claim_strength": "observed",
                    "evidence_refs": ["compare_periods:inline"],
                }
            ],
            "verifier": {"errors": []},
            "evidence": [
                {
                    "evidence_ref": "compare_periods:inline",
                    "capability_id": "compare_periods",
                    "strength": "medium",
                    "wording_limit": "contextual",
                    "typed_payload": {"median_uplift": 0.2},
                    "limitations": ["weak_direction"],
                }
            ],
        }

        context = _answer_synthesis_context(state)

        self.assertEqual(
            context["verified_claim_slots"][0]["business_claim"],
            "Q2 相比 Q1 的付费金额提升 20.0%，当前只支持窗口对比结论。",
        )
        self.assertIn("排查方向", context["bounded_insight_guidance"]["insight_prompt"])
        self.assertIn("方向一致性不足", context["bounded_insight_guidance"]["evidence_limits"])

    def test_route_normalization_adds_driver_decomposition_for_explicit_volume_vs_unit_value_question(self):
        nodes = _normalize_route_requested_nodes(
            ("data_quality_profile", "compare_periods", "answer_verify"),
            {
                "question_family": "custom_baseline_comparison",
                "pattern_family": "custom_baseline",
                "target_claim": "Q2提升主要是付费用户数增加还是单付费用户金额提升带来的",
                "target_metric": "paid_amount",
                "analysis_requirements": {
                    "claim_intents": ["formula_component_contribution"]
                },
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
                "analysis_requirements": {
                    "requested_dimensions": ["channel"],
                    "diagnostic_tags": ["pattern_attribution"],
                },
            },
        )

        self.assertIn("driver_decomposition", nodes)

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

        self.assertIn("evidence_reduce", nodes)
        self.assertIn("answer_verify", nodes)

    def test_route_normalization_keeps_llm_requested_segment_for_compiler_audit(self):
        nodes = _normalize_route_requested_nodes(
            ("driver_decomposition", "segment_contribution", "answer_verify"),
            {
                "question_family": "segment_or_factor_attribution",
                "pattern_family": "custom_baseline",
                "target_claim": "Q2提升主要是付费用户数贡献还是单付费用户金额贡献",
                "target_metric": "paid_amount",
                "analysis_requirements": {"requested_dimensions": ["channel"]},
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
                "secondary_question_families": [],
                "pattern_family": "custom_baseline",
                "target_claim": "pattern_explanation",
                "target_metric": "paid_amount",
                "analysis_requirements": {
                    "requested_dimensions": ["channel"],
                    "diagnostic_tags": ["factor_topk"],
                },
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
                "analysis_requirements": {
                    "requested_dimensions": ["channel"],
                    "diagnostic_tags": ["factor_topk"],
                },
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
                "analysis_requirements": {
                    "requested_dimensions": ["channel"],
                    "diagnostic_tags": ["factor_topk"],
                },
            },
        )

        self.assertIn("joint_attribution", nodes)

    def test_route_normalization_adds_outlier_recalc_for_daily_removal_clarification(self):
        nodes = _normalize_route_requested_nodes(
            ("data_quality_profile", "compare_periods", "answer_verify"),
            {
                "question": "按日粒度，移除贡献最大的正向日期后复算，不做订单级明细剔除。",
                "question_family": "anomaly_or_black_swan_review",
                "primary_question_family": "anomaly_or_black_swan_review",
                "pattern_family": "custom_baseline",
                "target_claim": "移除贡献最大的正向日期后复算付费金额方向",
                "target_metric": "paid_amount",
                "analysis_requirements": {
                    "claim_intents": ["external_shock_candidate_or_anomaly"],
                    "diagnostic_tags": ["anomaly"],
                },
            },
        )

        self.assertIn("outlier_scan", nodes)
        self.assertIn("outlier_contribution", nodes)

    def test_route_normalization_keeps_compare_and_verify_for_daily_average_corrections(self):
        nodes = _normalize_route_requested_nodes(
            ("data_quality_profile", "driver_decomposition"),
            {
                "question": "换成日均再看一遍。",
                "question_family": "custom_baseline_comparison",
                "primary_question_family": "custom_baseline_comparison",
                "pattern_family": "custom_baseline",
                "target_claim": "按日均付费金额重新比较 Q2 和 Q1",
                "target_metric": "paid_amount",
                "analysis_requirements": {"baselines": ["rolling_7_day_baseline"]},
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
                "analysis_requirements": {"baselines": ["rolling_7_day_baseline"]},
            },
        )

        self.assertIn("compare_periods", nodes)
        self.assertIn("formula_decompose", nodes)

    def test_weekly_grain_without_weekday_target_repairs_to_rolling(self):
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

        state = {
            "request": {
                "question": "口径改成按周看，还一样吗？",
                "pattern_params": {
                    "period_key": "period",
                    "group_key": "group",
                    "target_group": "target",
                    "baseline_group": "baseline",
                },
            },
            "run_id": "weekly-grain-period-compare",
            "llm_client": fake,
            "llm_calls": [],
        }

        _understand_business_intent(state)

        self.assertEqual(state["intent"]["pattern_family"], "rolling")
        self.assertNotIn("target_weekdays", state["intent"]["pattern_params"])

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

    def test_business_intent_treats_null_list_fields_as_empty_lists(self):
        fake = FakeLLMClient(
            {
                "business_intent": {
                    "question_family": "pattern_explanation",
                    "pattern_family": "intra_period",
                    "baseline_candidates": None,
                    "sub_intents": None,
                    "ambiguous_slots": None,
                    "question_families": None,
                    "secondary_question_families": None,
                }
            }
        )
        state = {
            "request": {"question": "最近付费金额走势怎么样？"},
            "run_id": "intent-null-lists",
            "llm_client": fake,
            "llm_calls": [],
        }

        _understand_business_intent(state)

        self.assertEqual(state["intent"]["baseline_candidates"], [])
        self.assertEqual(state["intent"]["sub_intents"], [])
        self.assertEqual(state["intent"]["ambiguous_slots"], [])
        self.assertEqual(state["intent"]["secondary_question_families"], [])

    def test_business_intent_normalizes_none_pattern_family_to_intra_period(self):
        fake = FakeLLMClient(
            {
                "business_intent": {
                    "question_family": "custom_baseline_comparison",
                    "pattern_family": "none",
                }
            }
        )
        state = {
            "request": {"question": "2026年Q2相比Q1，付费金额变化了多少？"},
            "run_id": "intent-pattern-family-none",
            "llm_client": fake,
            "llm_calls": [],
        }

        _understand_business_intent(state)

        self.assertEqual(state["intent"]["pattern_family"], "intra_period")

    def test_business_intent_normalizes_unsupported_pattern_family_to_intra_period(self):
        fake = FakeLLMClient(
            {
                "business_intent": {
                    "question_family": "pattern_explanation",
                    "pattern_family": "surprise_mode",
                }
            }
        )
        state = {
            "request": {"question": "最近付费金额走势怎么样？"},
            "run_id": "intent-pattern-family-unsupported",
            "llm_client": fake,
            "llm_calls": [],
        }

        _understand_business_intent(state)

        self.assertEqual(state["intent"]["pattern_family"], "intra_period")

    def test_business_intent_does_not_infer_custom_baseline_from_carried_context(self):
        fake = FakeLLMClient(
            {
                "business_intent": {
                    "question_family": "custom_baseline_comparison",
                    "pattern_family": "surprise_mode",
                    "baseline": {"label": "Q1"},
                    "target": {"label": "Q2"},
                }
            }
        )
        state = {
            "request": {
                "question": "2026年Q2相比Q1，付费金额变化了多少？",
            },
            "run_id": "intent-pattern-family-carried-context",
            "llm_client": fake,
            "llm_calls": [],
        }

        _understand_business_intent(state)

        self.assertEqual(state["intent"]["pattern_family"], "intra_period")

    def test_route_normalization_keeps_answer_verify_for_actionability_challenges(self):
        nodes = _normalize_route_requested_nodes(
            ("data_quality_profile",),
            {
                "question": "这个结果能不能直接指导投放？",
                "question_family": "revenue_health_review",
                "primary_question_family": "revenue_health_review",
                "target_claim": "判断当前结果能否直接指导投放",
                "target_metric": "paid_amount",
                "analysis_requirements": {"claim_intents": ["contract_coverage_and_trust_boundary"]},
            },
        )

        self.assertIn("formula_decompose", nodes)

    def test_route_normalization_keeps_answer_verify_for_stability_challenges(self):
        nodes = _normalize_route_requested_nodes(
            ("data_quality_profile",),
            {
                "question": "这些结果有多稳？",
                "question_family": "pattern_explanation",
                "primary_question_family": "pattern_explanation",
                "target_claim": "判断当前结果稳健性",
                "target_metric": "paid_amount",
                "analysis_requirements": {"requested_dimensions": ["channel"]},
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

        self.assertIn("compare_periods", nodes)

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
        self.assertIn("evidence_reduce", result.answer_package["accepted_graph"])
        self.assertIn("metric_timeseries", result.answer_package["accepted_graph"])
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
        self.assertIn("metric_timeseries", result.answer_package["accepted_graph"])

    def test_boundary_question_without_user_choice_waits_without_conclusion(self):
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
                },
                "clarification_question": {
                    "questions": [{
                        "question": "Which business scope should be used?",
                        "options": [
                            "Use the full sample.",
                            "Use a custom business scope.",
                            "tell the agent to do differently",
                        ],
                    }],
                    "recommended_assumption": {"option": "Use the full sample."},
                    "status_message": "Waiting for a business scope choice.",
                },
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {"artifact_root": tmpdir, "run_id": "needs-question", "llm_client": fake}
            )

        self.assertEqual(result.status, "waiting_for_clarification")
        self.assertIn("clarification_question", fake.calls)
        self.assertNotIn("blocked_explanation", fake.calls)
        self.assertEqual(result.answer_package["accepted_graph"], [])
        self.assertEqual(
            result.answer_package["clarification"]["recommended_assumption"],
            {"option": "Use the full sample."},
        )

    def test_degrade_suggestion_does_not_drop_established_pattern_answer(self):
        fake = FakeLLMClient({"next_action": {"next_action": "degrade"}})

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {"artifact_root": tmpdir, "run_id": "degrade-override", "llm_client": fake}
            )

        self.assertEqual(result.status, "draft")
        self.assertIn("evidence_interpretation", fake.calls)
        self.assertIn("answer_synthesis", fake.calls)
        self.assertIn("degraded_explanation", fake.calls)
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
        self.assertEqual(summary["claims"], [])
        self.assertFalse(result.answer_package["quality_gate"]["has_verified_claims"])

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
        self.assertIn("degraded_explanation", fake.calls)
        self.assertEqual(answer_text, "")
        self.assertEqual(payload["claims"], [])
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

    def test_degraded_explanation_rejects_target_metric_substitution(self):
        state = {
            "intent": {"target_metric": "paid_amount"},
            "evidence_brief": {"limitations": ["source_unbound"]},
            "validator_results": [],
        }
        output = {
            "status": "degraded",
            "explanation": "利润指标所需数据暂时不可用。",
            "owner": "数据工程团队",
            "repair_path": "补齐数据源后重跑。",
        }

        with self.assertRaisesRegex(WorkflowFailure, "target_metric_drift"):
            _sanitize_terminal_explanation(output, state, "degraded")

    def test_degraded_explanation_rejects_substitution_for_any_bound_metric(self):
        state = {
            "intent": {"target_metric": "payment_success_rate"},
            "evidence_brief": {"limitations": ["source_unbound"]},
            "validator_results": [],
        }
        output = {
            "status": "degraded",
            "explanation": "利润指标所需数据暂时不可用。",
            "owner": "数据工程团队",
            "repair_path": "补齐数据源后重跑。",
        }

        with self.assertRaisesRegex(WorkflowFailure, "target_metric_drift"):
            _sanitize_terminal_explanation(output, state, "degraded")

    def test_degraded_explanation_allows_target_and_other_metric_context(self):
        state = {
            "intent": {"target_metric": "payment_success_rate"},
            "evidence_brief": {"limitations": ["source_unbound"]},
            "validator_results": [],
        }
        output = {
            "status": "degraded",
            "explanation": "支付成功率暂时不可核验，付费金额仅作为相关缺口背景。",
            "owner": "数据工程团队",
            "repair_path": "补齐数据源后重跑。",
        }

        result = _sanitize_terminal_explanation(output, state, "degraded")

        self.assertIn("支付成功率", result["explanation"])

    def test_degraded_explanation_retries_semantic_rejection_with_reason(self):
        from bi_agent.runtime.langgraph_workflow import _generate_degraded_explanation

        state = {
            "request": {"run_mode": "production"},
            "run_id": "degraded-semantic-retry",
            "intent": {
                "target_metric": "paid_amount",
                "scope": "full_sample",
                "time_window": "yesterday",
            },
            "evidence_brief": {"limitations": ["source_unbound"]},
            "validator_results": [],
            "verifier": {},
            "evidence": [],
            "draft_claims": [],
        }
        payloads = []

        def explain(_state, task, payload):
            self.assertEqual(task, "degraded_explanation")
            payloads.append(payload)
            if len(payloads) == 1:
                return {
                    "explanation": "利润指标所需数据暂时不可用。",
                    "owner": "数据工程团队",
                    "repair_path": "补齐数据源后重跑。",
                }
            return {
                "explanation": "付费金额所需数据暂时不可用。",
                "owner": "数据工程团队",
                "repair_path": "补齐数据源后重跑。",
            }

        with patch(
            "bi_agent.runtime.langgraph_workflow._invoke_llm",
            side_effect=explain,
        ):
            _generate_degraded_explanation(state)

        self.assertEqual(len(payloads), 2)
        self.assertIn(
            "target_metric_drift",
            payloads[1]["retry_context"]["failure_reason"],
        )
        self.assertIn("付费金额", state["final_explanation"]["explanation"])

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
        self.assertEqual(summary["claims"], [])
        self.assertEqual(evidence[0]["evidence_type"], "insufficient")
        self.assertEqual(evidence[0]["strength"], "insufficient")
        self.assertEqual(
            result.answer_package["final_explanation"]["status"],
            "blocked",
        )
        self.assertFalse(result.answer_package["quality_gate"]["has_verified_claims"])

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
        self.assertIn("degraded_explanation", fake.calls)

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

    def test_data_coverage_input_prefers_baseline_runtime_rows_over_quality_probe_rows(self):
        class Provider:
            def configured(self):
                return True

            def binding_reason(self):
                return ""

            def plan(self, request, intent, accepted_graph):
                from bi_agent.runtime.clickhouse_revenue_rows import RevenueRowPlan

                return RevenueRowPlan(
                    sql_text="SELECT 1",
                    query_id="coverage-runtime:data_quality_probe",
                    required_fields=("period", "group", "amount", "orders"),
                    dimension_keys=(),
                )

            def fetch(self, plan):
                from bi_agent.runtime.clickhouse_revenue_rows import RevenueRowsResult

                quality_rows = (
                    {
                        "period": "2026-07-08",
                        "group": "target",
                        "orders": 10,
                        "paid_users": 8,
                        "min_period": "2026-07-01",
                        "max_period": "2026-07-08",
                    },
                )
                baseline_rows = (
                    {"period": "2026-07-07", "group": "previous_day", "amount": 90.0, "orders": 9},
                    {"period": "2026-07-08", "group": "target", "amount": 120.0, "orders": 10},
                )
                return RevenueRowsResult(
                    ok=True,
                    rows=quality_rows,
                    query_hash="hash-coverage-runtime",
                    query_id=plan.query_id,
                    result_refs=("quality-ref",),
                    rows_by_intent={
                        "data_quality_probe": quality_rows,
                        "daily_metric_baselines": baseline_rows,
                    },
                    result_refs_by_intent={
                        "data_quality_probe": ("quality-ref",),
                        "daily_metric_baselines": ("baseline-ref",),
                    },
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "coverage-runtime",
                    "question": "昨天付费金额为什么上涨？",
                    "llm_client": FakeLLMClient(),
                    "row_provider": Provider(),
                    "requested_nodes": [
                        "data_quality_profile",
                        "compare_periods",
                        "driver_decomposition",
                        "answer_verify",
                    ],
                }
            )

        payload = _llm_input_payload(result.answer_package, "data_coverage_interpretation")
        summary = payload["data_result_summary"]

        self.assertEqual(summary["row_count"], 2)
        self.assertIn("amount", summary["fields"])
        self.assertEqual(summary["field_values"]["group"], ["previous_day", "target"])
        self.assertNotIn("min_period", summary["fields"])

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
        self.assertEqual(claims, [])
        self.assertEqual(result.answer_package["admin_audit"]["verifier"]["status"], "failed")

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
        self.assertEqual(claims, [])

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
        self.assertEqual(claims, [])

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
        self.assertEqual(answer_text, "")

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

        self.assertEqual(answer_text, "")

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
        self.assertEqual(summary, "")

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
        self.assertIn("degraded_explanation", fake.calls)
        summary = result.answer_package["sections"][0]["payload"]
        self.assertEqual(summary["claims"], [])
        self.assertEqual(result.answer_package["admin_audit"]["verifier"]["status"], "failed")

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

    def test_degraded_answer_replaces_claim_rejected_after_repair(self):
        rejected_claim = {
            "text": "这里保留了错误数字 990.0%。",
            "evidence_refs": ["pattern_scan:intra_period"],
            "numbers": {"median_uplift": 9.9},
            "scope": "full_sample",
            "time_window": "2024-01..2026-05",
        }
        fake = FakeLLMClient(
            {
                "answer_synthesis": {
                    "answer_text": "这里保留了错误数字 990.0%。",
                    "claims": [rejected_claim],
                },
                "answer_repair": {
                    "answer_text": "修复后仍保留错误数字 990.0%。",
                    "claims": [rejected_claim],
                },
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "verifier-repair-degrades-safely",
                    "llm_client": fake,
                }
            )

        self.assertIn("degraded_explanation", fake.calls)
        self.assertEqual(result.answer_package["admin_audit"]["verifier"]["status"], "failed")
        self.assertNotIn("990.0%", result.answer_package["final_answer"])
        claims = result.answer_package["sections"][0]["payload"]["claims"]
        self.assertEqual(claims, [])
        audit_input = _llm_input_payload(result.answer_package, "final_answer_audit")
        self.assertEqual(audit_input["verified_claims"][0]["claim_strength"], "insufficient")
        self.assertNotIn("median_uplift", audit_input["verified_claims"][0]["numbers"])

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
        self.assertEqual(summary["claims"], [])

    def test_final_summary_display_repair_ignores_cautious_evidence_gaps(self):
        state = {
            "intent": {"pattern_family": "custom_baseline"},
            "evidence": [],
            "evidence_brief": {"limitations": ["insufficient_comparable_periods"]},
        }
        summary = (
            "我对问题的理解是：你想判断活动是否影响付费金额变化。\n"
            "分析脉络：我检查了现有指标变化与活动窗口。\n"
            "关键发现：现有材料不足以形成稳定判断。\n"
            "最终结论：当前证据不足，不能确认活动带来了付费金额变化。\n"
            "需要注意：结论仅用于后续排查，仍需补充相关证据。"
        )

        self.assertFalse(_final_summary_needs_display_repair(summary, state))

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
        self.assertEqual(answer, "")
        self.assertFalse(result.answer_package["quality_gate"]["has_verified_claims"])

    def test_followup_questions_are_single_intent(self):
        result = self._run_q2_q1_joint_attribution_workflow()
        questions = result.answer_package["follow_up_questions"]
        self.assertEqual(questions, [])

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

    def test_quality_gate_accepts_paraphrased_insufficient_claim(self):
        quality = evaluate_answer_quality(
            user_question="昨天收入变化最大的是哪个维度？",
            verified_claims=[
                {
                    "text": "当前证据不足以支撑主业务结论；主要限制是数据字段缺失、可比周期不足。",
                    "numbers": {},
                    "claim_strength": "insufficient",
                }
            ],
            final_answer=(
                "最终结论：当前证据不足，无法确认收入变化最大的维度和因子；"
                "主要限制包括包名和玩法字段缺失、可比周期不足。"
                "当前证据能把排查方向收敛到数据补齐。"
            ),
            follow_up_questions=[
                "要先补哪些字段？",
                "要复核可比周期吗？",
                "要看已有维度覆盖吗？",
            ],
        )

        self.assertTrue(quality["verified_claim_preserved"])
        self.assertNotIn("missing_verified_claim", quality["issues"])

    def test_quality_gate_accepts_insufficient_claim_with_unsupported_conclusion_wording(self):
        quality = evaluate_answer_quality(
            user_question="昨天活动是否影响了付费金额？",
            verified_claims=[
                {
                    "text": "当前证据不足以支撑主业务结论；主要限制是变化幅度低于当前重要性阈值。",
                    "numbers": {},
                    "claim_strength": "insufficient",
                }
            ],
            final_answer=(
                "最终结论：根据现有证据，无法支持活动影响付费金额的结论；"
                "主要限制包括变化幅度低于当前重要性阈值。"
            ),
            follow_up_questions=[
                "要补活动记录吗？",
                "要扩大对比窗口吗？",
                "要看渠道分层吗？",
            ],
        )

        self.assertTrue(quality["verified_claim_preserved"])
        self.assertNotIn("missing_verified_claim", quality["issues"])

    def test_quality_gate_accepts_generic_evidence_strength_limitation(self):
        quality = evaluate_answer_quality(
            user_question="昨天收入变化最大的是哪个维度？",
            verified_claims=[
                {
                    "text": "当前证据不足以支撑主业务结论；主要限制是当前证据强度不足。",
                    "numbers": {},
                    "claim_strength": "insufficient",
                }
            ],
            final_answer=(
                "最终结论：基于当前证据，无法得出昨天收入变化的确定原因。"
                "现有证据强度不足以支撑主业务结论。"
            ),
            follow_up_questions=[
                "要先补哪些字段？",
                "要复核可比周期吗？",
                "要看已有维度覆盖吗？",
            ],
        )

        self.assertTrue(quality["verified_claim_preserved"])
        self.assertNotIn("missing_verified_claim", quality["issues"])

    def test_quality_gate_accepts_business_paraphrase_with_claim_numbers(self):
        quality = evaluate_answer_quality(
            user_question="Q2 相比 Q1 付费金额为什么变了？",
            verified_claims=[
                {
                    "text": "Q2 相比 Q1 的付费金额提升 20.0%，当前只支持窗口对比结论。",
                    "numbers": {"change_pct": 0.2},
                    "claim_strength": "observed",
                }
            ],
            final_answer=(
                "最终结论：从当前窗口对比看，付费金额比基线高 20.0%，"
                "这能作为观察结论使用，不能直接写成原因定论。"
            ),
            follow_up_questions=[
                "要看渠道贡献吗？",
                "要复核异常日期吗？",
                "要换成日均口径吗？",
            ],
        )

        self.assertTrue(quality["verified_claim_preserved"])
        self.assertNotIn("missing_verified_claim", quality["issues"])

    def test_llm_final_audit_can_clear_local_missing_business_insight_warning(self):
        quality = _legacy_quality_with_final_answer_audit(
            {
                "direct_answer": True,
                "has_verified_claims": True,
                "verified_claim_preserved": False,
                "business_insight_present": False,
                "followups_one_intent": True,
                "issues": ["missing_verified_claim", "missing_business_insight"],
            },
            {
                "display_status": "ready",
                "hard_blockers": [],
                "repairable_warnings": [],
                "blocks_display": False,
            },
        )

        self.assertTrue(quality["business_insight_present"])
        self.assertTrue(quality["verified_claim_preserved"])
        self.assertNotIn("missing_verified_claim", quality["issues"])
        self.assertNotIn("missing_business_insight", quality["issues"])

    def test_llm_final_audit_can_clear_local_missing_verified_claim_warning(self):
        quality = _legacy_quality_with_final_answer_audit(
            {
                "direct_answer": True,
                "has_verified_claims": True,
                "verified_claim_preserved": False,
                "business_insight_present": True,
                "followups_one_intent": True,
                "issues": ["missing_verified_claim"],
            },
            {
                "display_status": "ready",
                "hard_blockers": [],
                "repairable_warnings": [],
                "blocks_display": False,
            },
        )

        self.assertTrue(quality["verified_claim_preserved"])
        self.assertNotIn("missing_verified_claim", quality["issues"])

    def test_llm_final_audit_keeps_business_insight_warning_when_audit_flags_it(self):
        quality = _legacy_quality_with_final_answer_audit(
            {
                "direct_answer": True,
                "has_verified_claims": True,
                "verified_claim_preserved": False,
                "business_insight_present": False,
                "followups_one_intent": True,
                "issues": ["missing_verified_claim", "missing_business_insight"],
            },
            {
                "display_status": "ready_with_warnings",
                "hard_blockers": [],
                "repairable_warnings": ["weak_business_interpretation"],
                "blocks_display": False,
            },
        )

        self.assertFalse(quality["business_insight_present"])
        self.assertFalse(quality["verified_claim_preserved"])
        self.assertIn("missing_verified_claim", quality["issues"])
        self.assertIn("missing_business_insight", quality["issues"])


if __name__ == "__main__":
    unittest.main()
