import json
import tempfile
import unittest

from bi_agent.runtime.answer_package import build_answer_package, verify_answer_package
from bi_agent.runtime.langgraph_workflow import run_pattern_workflow
from bi_agent.runtime.artifacts import filter_artifact_for_role
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


class WorkflowArtifactsAnswerTest(unittest.TestCase):
    def test_answer_package_keeps_causal_audit_in_admin_audit_only(self):
        package = build_answer_package(
            run_id="causal-audit-package",
            draft_claims=[],
            evidence=[],
            checkpoint_events=[],
            proposed_graph=[],
            accepted_graph=[],
            rejected_or_degraded_mutations=[],
            validator_results=[],
            sql_text="SELECT 1",
            sql_hash="hash",
            artifact_audit={},
            causal_audit={"causal_assessment": "candidate_hypothesis"},
            causal_evidence_dossier={"target_claim": "候选机制"},
        )

        summary_payload = package["sections"][0]["payload"]
        admin_payload = package["admin_audit"]

        self.assertNotIn("causal_evidence_dossier", summary_payload)
        self.assertEqual(
            admin_payload["causal_audit"]["causal_assessment"],
            "candidate_hypothesis",
        )
        self.assertEqual(
            admin_payload["causal_evidence_dossier"]["target_claim"],
            "候选机制",
        )

    def test_langgraph_failure_does_not_publish_business_conclusion(self):
        result = run_pattern_workflow(
            {"force_langgraph_failure": True, "llm_client": FakeLLMClient()}
        )
        self.assertEqual(result.status, "failed")
        self.assertIsNone(result.answer_package)
        self.assertTrue(result.failure_reason)

    def test_role_visibility_hides_admin_sql_from_business_reader(self):
        artifact = {
            "sections": [
                {
                    "section_id": "summary",
                    "visibility": "business_summary",
                    "payload": {"text": "draft"},
                },
                {
                    "section_id": "sql",
                    "visibility": "admin_audit",
                    "payload": {"sql": "SELECT 1"},
                },
            ]
        }
        filtered = filter_artifact_for_role(artifact, "business_reader")
        self.assertEqual(
            [section["section_id"] for section in filtered["sections"]],
            ["summary"],
        )

    def test_successful_workflow_persists_answer_package_and_checkpoints(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "test-run",
                    "llm_client": FakeLLMClient(),
                }
            )

            self.assertEqual(result.status, "draft")
            self.assertTrue(result.answer_package)
            self.assertTrue(result.artifact_path.endswith("answer_package.json"))
            with open(result.artifact_path, encoding="utf-8") as handle:
                artifact = json.load(handle)

        self.assertEqual(artifact["run_id"], "test-run")
        self.assertEqual(
            [event["node"] for event in artifact["checkpoint_events"]],
            [
                "understand_business_intent",
                "decide_question_boundary",
                "clarification_policy_gate",
                "confirm_business_understanding",
                "design_analysis_route",
                "accept_analysis_route",
                "inspect_schema",
                "validate_runtime_binding",
                "interpret_data_coverage",
                "execute_capabilities",
                "reduce_evidence",
                "decide_next_action",
                "interpret_evidence",
                "audit_causal_implications",
                "synthesize_answer",
                "semantic_audit",
                "hard_verify_answer",
                "final_business_summary",
                "persist_artifact",
            ],
        )
        self.assertIn("accepted_graph", artifact)
        self.assertIn("proposed_graph", artifact)
        self.assertIn("validator_results", artifact)
        self.assertIn("llm_calls", artifact["admin_audit"])
        summary = artifact["sections"][0]["payload"]
        self.assertIn("final_business_summary", summary)
        self.assertIn("我对问题的理解", summary["final_business_summary"])
        self.assertIn("分析脉络", summary["final_business_summary"])
        self.assertIn("最终结论", summary["final_business_summary"])

    def test_business_artifact_sections_expose_sql_hash_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "visibility",
                    "llm_client": FakeLLMClient(),
                }
            )

            business = filter_artifact_for_role(result.answer_package, "business_reader")
            admin = filter_artifact_for_role(result.answer_package, "data_owner_admin")

        self.assertIn("sql_hash", json.dumps(business))
        self.assertNotIn("SELECT", json.dumps(business))
        self.assertNotIn("validator_results", business)
        self.assertNotIn("checkpoint_events", business)
        self.assertNotIn("proposed_graph", business)
        self.assertNotIn("accepted_graph", business)
        self.assertNotIn("rejected_or_degraded_mutations", business)
        self.assertIn("SELECT", json.dumps(admin))

    def test_analyst_diagnostics_do_not_expose_admin_validator_results(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "analyst",
                    "llm_client": FakeLLMClient(),
                }
            )

            analyst = filter_artifact_for_role(result.answer_package, "analyst")

        diagnostics = [
            section for section in analyst["sections"] if section["section_id"] == "diagnostics"
        ][0]
        self.assertIn("sql_hash", diagnostics["payload"])
        self.assertNotIn("validator_results", diagnostics["payload"])
        self.assertNotIn("artifact_audit", diagnostics["payload"])
        self.assertNotIn("sql_text", diagnostics["payload"])
        self.assertNotIn("proposed_graph", diagnostics["payload"])
        self.assertNotIn("accepted_graph", diagnostics["payload"])
        self.assertNotIn("rejected_or_degraded_mutations", diagnostics["payload"])
        self.assertNotIn("checkpoint_events", analyst)

    def test_wording_warnings_do_not_block_phase4_draft(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "wording",
                    "llm_client": FakeLLMClient(),
                    "draft_claims": [
                        {
                            "text": "Month-start timing caused paid amount uplift.",
                            "evidence_refs": ["pattern_scan:intra_period"],
                            "numbers": {"median_uplift": 0.2},
                            "scope": "full_sample",
                            "time_window": "2024-01..2026-05",
                        }
                    ],
                }
            )

            admin = filter_artifact_for_role(result.answer_package, "data_owner_admin")

        self.assertEqual(result.status, "draft")
        self.assertTrue(
            any(
                warning["code"] == "causal_wording_without_causal_evidence"
                for warning in admin["admin_audit"]["verifier"]["warnings"]
            )
        )

    def test_final_business_summary_enforces_user_facing_shape(self):
        fake = FakeLLMClient(
            {
                "final_business_summary": {
                    "summary_text": "paid_amount rose. pattern_scan says ok.",
                }
            }
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "final-summary-shape",
                    "llm_client": fake,
                }
            )

        summary = result.answer_package["sections"][0]["payload"]["final_business_summary"]
        self.assertIn("我对问题的理解", summary)
        self.assertIn("分析脉络", summary)
        self.assertIn("关键发现", summary)
        self.assertIn("最终结论", summary)
        self.assertIn("需要注意", summary)
        self.assertNotIn("paid_amount", summary)
        self.assertNotIn("pattern_scan", summary)

    def test_final_business_summary_allows_bounded_insight_without_exact_claim_copy(self):
        fake = FakeLLMClient(
            {
                "final_business_summary": {
                    "summary_text": (
                        "我对问题的理解是：你想判断全样本付费金额是否存在周期内模式。\n"
                        "分析脉络：系统先确认数据口径，再比较目标阶段和基线阶段。\n"
                        "关键发现：中位提升 20.0%，方向一致比例 100.0%，共有 29 个可比周期。\n"
                        "最终结论：这个现象支持一个有边界的周期内观察，但仍然停留在统计相关层面。\n"
                        "需要注意：洞察上可以继续观察支付节奏和用户结构，但不能归因。"
                    ),
                }
            }
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "final-summary-insight",
                    "llm_client": fake,
                }
            )

        summary = result.answer_package["sections"][0]["payload"]["final_business_summary"]
        self.assertIn("洞察上可以继续观察支付节奏和用户结构", summary)
        self.assertIn("方向一致比例", summary)
        self.assertNotIn("方向命中率", summary)
        self.assertNotIn("周期内付费金额模式在 2024-01..2026-05 观察到", summary)

    def test_final_business_summary_repairs_custom_baseline_limit_reason(self):
        fake = FakeLLMClient(
            {
                "next_action": {
                    "next_action": "degrade",
                    "decision_summary": "证据不足。",
                },
                "final_business_summary": {
                    "summary_text": (
                        "我对问题的理解是：你想看目标相比基线是否提升。\n"
                        "分析脉络：我做了周期对比。\n"
                        "关键发现：目标相比基线提升 20.0%。\n"
                        "最终结论：当前证据不足以发布这个主结论。\n"
                        "需要注意：继续观察。"
                    ),
                },
            }
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "final-summary-custom-baseline-limit",
                    "llm_client": fake,
                    "question": "目标期相比基线期是否稳定提升？",
                    "pattern_family": "custom_baseline",
                    "pattern_params": {
                        "period_key": "period",
                        "group_key": "group",
                        "target_group": "target",
                        "baseline_group": "baseline",
                        "min_periods": 2,
                    },
                    "baseline": {"label": "基线期"},
                    "target": {"label": "目标期"},
                    "rows": [
                        {"period": "p1", "group": "baseline", "amount": 100},
                        {"period": "p1", "group": "target", "amount": 120},
                    ],
                }
            )

        summary = result.answer_package["sections"][0]["payload"]["final_business_summary"]
        self.assertIn("可比周期", summary)
        self.assertIn("低于本轮要求", summary)

    def test_final_business_summary_businessizes_driver_scope_and_labels(self):
        fake = FakeLLMClient(
            {
                "business_intent": {
                    "question_family": "segment_or_factor_attribution",
                    "pattern_family": "custom_baseline",
                    "scope": "all_users",
                    "target_claim": "Q2提升来自付费用户数还是单付费用户金额",
                    "baseline_candidates": [],
                },
                "analysis_route": {
                    "requested_nodes": ["driver_decomposition", "answer_verify"],
                },
                "final_business_summary": {
                    "summary_text": (
                        "我对问题的理解是：口径是all_users。\n"
                        "分析脉络：我做了驱动拆解。\n"
                        "关键发现：主要驱动因素为单用户付费金额。\n"
                        "最终结论：单用户/单订单价值贡献 62.5%，用户数/订单量贡献 37.5%。\n"
                        "需要注意：只适用于当前窗口。"
                    ),
                },
            }
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "final-summary-driver-labels",
                    "llm_client": fake,
                    "question": "Q2提升主要来自付费用户数还是单付费用户金额？",
                    "pattern_family": "custom_baseline",
                    "pattern_params": {
                        "period_key": "period",
                        "group_key": "group",
                        "target_group": "target",
                        "baseline_group": "baseline",
                        "min_periods": 1,
                    },
                    "scope": "all_users",
                    "baseline": {"label": "Q1"},
                    "target": {"label": "Q2"},
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
                            "amount": 160,
                            "paid_users": 12,
                        },
                    ],
                }
            )

        summary = result.answer_package["sections"][0]["payload"]["final_business_summary"]
        self.assertIn("全体用户", summary)
        self.assertIn("单付费用户金额", summary)
        self.assertIn("付费用户数", summary)
        self.assertNotIn("all_users", summary)
        self.assertNotIn("单用户/单订单", summary)
        self.assertNotIn("驱动因素", summary)
        self.assertNotIn("单用户付费金额", summary)

    def test_final_business_summary_receives_composite_business_threads(self):
        fake = FakeLLMClient(
            {
                "business_intent": {
                    "question_family": "paid_amount_change_explanation",
                    "question_families": [
                        "paid_amount_change_explanation",
                        "segment_or_factor_attribution",
                    ],
                    "pattern_family": "custom_baseline",
                    "scope": "all_users",
                    "target_claim": "Q2增长的渠道和驱动贡献",
                    "baseline_candidates": [],
                },
                "analysis_route": {
                    "requested_nodes": [
                        "driver_decomposition",
                        "segment_contribution",
                        "answer_verify",
                    ],
                },
                "final_business_summary": {
                    "summary_text": (
                        "我对问题的理解是：你想看Q2增长如何被拆解。\n"
                        "分析脉络：我先判断整体变化，再拆渠道贡献和付费用户数/单付费用户金额贡献。\n"
                        "关键发现：增长主要由单付费用户金额和部分渠道共同解释。\n"
                        "最终结论：这是贡献拆解结论，不代表因果定论。\n"
                        "需要注意：继续观察渠道结构和用户价值变化。"
                    ),
                },
            }
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "final-summary-composite-context",
                    "llm_client": fake,
                    "question": "2026年Q2为什么增长，主要渠道和付费用户数/单付费用户金额分别怎么贡献？",
                    "pattern_family": "custom_baseline",
                    "pattern_params": {
                        "period_key": "channel",
                        "group_key": "group",
                        "target_group": "target",
                        "baseline_group": "baseline",
                    },
                    "scope": "all_users",
                    "baseline": {"label": "Q1"},
                    "target": {"label": "Q2"},
                    "rows": [
                        {
                            "channel": "WajeSpecial",
                            "group": "baseline",
                            "amount": 100,
                            "paid_users": 10,
                            "orders": 20,
                        },
                        {
                            "channel": "WajeSpecial",
                            "group": "target",
                            "amount": 160,
                            "paid_users": 12,
                            "orders": 24,
                        },
                        {
                            "channel": "Organic",
                            "group": "baseline",
                            "amount": 100,
                            "paid_users": 10,
                            "orders": 20,
                        },
                        {
                            "channel": "Organic",
                            "group": "target",
                            "amount": 90,
                            "paid_users": 9,
                            "orders": 18,
                        },
                    ],
                }
            )

        payload = _llm_input_payload(result.answer_package, "final_business_summary")

        self.assertEqual(
            payload["intent"]["question_families"],
            ["paid_amount_change_explanation", "segment_or_factor_attribution"],
        )
        self.assertEqual(
            [item["label"] for item in payload["business_threads"]],
            ["付费金额变化解释", "分群或因素归因"],
        )

    def test_final_business_summary_allows_negated_attribution_boundary(self):
        fake = FakeLLMClient(
            {
                "final_business_summary": {
                    "summary_text": (
                        "我对问题的理解是：你想判断全样本付费金额是否存在周期内模式。\n"
                        "分析脉络：系统先确认数据口径，再比较目标阶段和基线阶段。\n"
                        "关键发现：中位提升 20.0%，方向命中率 100.0%，共有 29 个可比周期。\n"
                        "最终结论：当前支持一个有边界的周期内观察，但不能归因于特定原因。\n"
                        "需要注意：洞察上可以继续观察支付节奏和用户结构。"
                    ),
                }
            }
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_pattern_workflow(
                {
                    "artifact_root": tmpdir,
                    "run_id": "final-summary-negated-attribution",
                    "llm_client": fake,
                }
            )

        summary = result.answer_package["sections"][0]["payload"]["final_business_summary"]
        self.assertIn("不能归因于特定原因", summary)
        self.assertIn("洞察上可以继续观察支付节奏和用户结构", summary)

    def test_medium_pattern_blocks_reliable_wording(self):
        verifier = verify_answer_package(
            draft_claims=[
                {
                    "text": "The paid amount shows a reliable rolling pattern.",
                    "evidence_refs": ["pattern_scan:rolling"],
                    "numbers": {"median_uplift": 0.04},
                    "scope": "full_sample",
                    "time_window": "2026-01..2026-06",
                }
            ],
            evidence=[
                {
                    "evidence_ref": "pattern_scan:rolling",
                    "capability": "pattern_scan",
                    "evidence_type": "statistical_association",
                    "strength": "medium",
                    "wording_limit": "supported",
                    "typed_payload": {
                        "median_uplift": 0.04,
                        "scope": "full_sample",
                        "time_window": "2026-01..2026-06",
                        "comparable_periods": 5,
                    },
                    "limitations": [],
                }
            ],
            visible_limitations=[],
        )

        self.assertTrue(
            any(warning["code"] == "over_strong_evidence_wording" for warning in verifier["warnings"])
        )

    def test_single_period_blocks_statistical_confidence_wording(self):
        verifier = verify_answer_package(
            draft_claims=[
                {
                    "text": "The uplift has high statistical confidence and appears non-random.",
                    "evidence_refs": ["pattern_scan:custom_baseline"],
                    "numbers": {"median_uplift": 0.15},
                    "scope": "full_sample",
                    "time_window": "2026-01..2026-06",
                }
            ],
            evidence=[
                {
                    "evidence_ref": "pattern_scan:custom_baseline",
                    "capability": "pattern_scan",
                    "evidence_type": "statistical_association",
                    "strength": "high",
                    "wording_limit": "supported",
                    "typed_payload": {
                        "median_uplift": 0.15,
                        "scope": "full_sample",
                        "time_window": "2026-01..2026-06",
                        "comparable_periods": 1,
                    },
                    "limitations": [],
                }
            ],
            visible_limitations=[],
        )

        self.assertTrue(
            any(warning["code"] == "single_period_confidence_wording" for warning in verifier["warnings"])
        )

    def test_retry_policy_retries_technical_failure_once_only(self):
        technical = run_pattern_workflow(
            {
                "force_failure": {
                    "node": "execute_capabilities",
                    "failure_type": "technical",
                },
                "llm_client": FakeLLMClient(),
            }
        )
        permission = run_pattern_workflow(
            {
                "force_failure": {
                    "node": "execute_capabilities",
                    "failure_type": "permission",
                },
                "llm_client": FakeLLMClient(),
            }
        )

        self.assertEqual(technical.status, "failed")
        self.assertEqual(
            [
                event["attempt"]
                for event in technical.checkpoint_events
                if event["node"] == "execute_capabilities"
            ],
            [1, 2],
        )
        self.assertEqual(
            [
                event["attempt"]
                for event in permission.checkpoint_events
                if event["node"] == "execute_capabilities"
            ],
            [1],
        )


if __name__ == "__main__":
    unittest.main()
