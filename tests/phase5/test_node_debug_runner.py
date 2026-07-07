import tempfile
import unittest

import yaml

from tests.phase4.fake_llm import FakeLLMClient
from tools.phase5 import debug_node_runner
from tools.phase5.debug_node_runner import build_initial_state, run_one_node


class NodeDebugRunnerTest(unittest.TestCase):
    def test_runner_knows_causal_audit_node(self):
        self.assertIn("audit_causal_implications", debug_node_runner.NODE_FUNCS)

    def test_node_debug_case_suite_starts_with_low_risk_then_stuck_case(self):
        with open("evals/phase5/node_debug_cases.yaml", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)

        ordered = sorted(data["cases"], key=lambda item: item["review_order"])

        self.assertEqual(
            [case["case_id"] for case in ordered],
            [
                "full_2026_q2_vs_q1",
                "full_month_start_vs_mid_end",
                "full_wajespecial_vs_other_by_month",
            ],
        )
        self.assertIn("卡点", ordered[-1]["review_focus"])

    def test_run_one_node_adds_only_that_node_and_its_llm_call(self):
        state = build_initial_state(
            {
                "case_id": "full_2026_q2_vs_q1",
                "pattern_family": "custom_baseline",
                "time_window": "2026-01-01..2026-06-30",
                "pattern_params": {
                    "period_key": "period",
                    "group_key": "group",
                    "target_group": "target",
                    "baseline_group": "baseline",
                    "min_periods": 1,
                },
                "question": "2026年Q2相比Q1，日均付费金额有没有明显抬升？",
                "required_capabilities": ["data_quality_profile", "compare_periods"],
                "baseline": {"label": "2026年Q1"},
                "target": {"label": "2026年Q2"},
            },
            rows=[
                {"period": "2026_h1", "group": "baseline", "amount": 100},
                {"period": "2026_h1", "group": "target", "amount": 115},
            ],
            artifact_root=tempfile.mkdtemp(),
        )
        fake = FakeLLMClient(
            {
                "business_intent": {
                    "question_family": "custom_baseline_comparison",
                    "pattern_family": "custom_baseline",
                    "target_metric": "paid_amount",
                    "scope": "全量样本",
                    "time_window": "2026-01-01..2026-06-30",
                    "target_claim": "判断2026年Q2日均付费金额是否显著抬升",
                },
            }
        )

        state = run_one_node(
            state,
            "understand_business_intent",
            llm_client=fake,
        )

        self.assertEqual(fake.calls, ["business_intent"])
        self.assertEqual(
            [event["node"] for event in state["checkpoint_events"]],
            ["understand_business_intent"],
        )
        self.assertEqual(state["node_debug_reviews"][-1]["llm_tasks_added"], ["business_intent"])
        self.assertIn("intent", state["node_debug_reviews"][-1]["changed_keys"])
        self.assertEqual(state["intent"]["scope"], "full_sample")
        self.assertNotIn("显著", state["intent"]["target_claim"])

        state = run_one_node(
            state,
            "decide_question_boundary",
            llm_client=fake,
        )

        self.assertEqual(fake.calls, ["business_intent", "boundary_decision"])
        self.assertEqual(
            [event["node"] for event in state["checkpoint_events"]],
            ["understand_business_intent", "decide_question_boundary"],
        )
        self.assertEqual(state["node_debug_reviews"][-1]["llm_tasks_added"], ["boundary_decision"])
        self.assertIn("boundary_decision", state["node_debug_reviews"][-1]["changed_keys"])

    def test_business_intent_preserves_composite_question_families(self):
        state = build_initial_state(
            {
                "case_id": "phase6-composite",
                "pattern_family": "custom_baseline",
                "time_window": "2026-01-01..2026-06-30",
                "pattern_params": {
                    "period_key": "channel",
                    "group_key": "group",
                    "target_group": "target",
                    "baseline_group": "baseline",
                },
                "question": "Q2为什么增长，主要渠道和用户数客单价分别怎么贡献？",
                "required_capabilities": ["driver_decomposition", "segment_contribution"],
            },
            rows=[
                {"channel": "WajeSpecial", "group": "baseline", "amount": 100},
                {"channel": "WajeSpecial", "group": "target", "amount": 160},
            ],
            artifact_root=tempfile.mkdtemp(),
        )
        fake = FakeLLMClient(
            {
                "business_intent": {
                    "question_family": "paid_amount_change_explanation",
                    "question_families": [
                        "paid_amount_change_explanation",
                        "segment_or_factor_attribution",
                    ],
                    "target_metric": "paid_amount",
                    "scope": "all_users",
                    "time_window": "2026-01-01..2026-06-30",
                    "pattern_family": "custom_baseline",
                    "target_claim": "formula_component_contribution",
                },
            }
        )

        state = run_one_node(state, "understand_business_intent", llm_client=fake)

        self.assertEqual(state["intent"]["question_family"], "paid_amount_change_explanation")
        self.assertEqual(
            state["intent"]["primary_question_family"],
            "paid_amount_change_explanation",
        )
        self.assertEqual(
            state["intent"]["secondary_question_families"],
            ["segment_or_factor_attribution"],
        )

    def test_boundary_decision_can_keep_needs_question_for_composite_ambiguity(self):
        state = build_initial_state(
            {
                "case_id": "phase6-ambiguous-composite",
                "pattern_family": "custom_baseline",
                "time_window": "2026-01-01..2026-06-30",
                "pattern_params": {
                    "period_key": "channel",
                    "group_key": "group",
                    "target_group": "target",
                    "baseline_group": "baseline",
                },
                "question": "WajeSpecial最近是不是比其他渠道好，也帮我看主要原因？",
                "required_capabilities": ["compare_periods", "segment_contribution"],
            },
            rows=[
                {"channel": "WajeSpecial", "group": "baseline", "amount": 100},
                {"channel": "WajeSpecial", "group": "target", "amount": 160},
            ],
            artifact_root=tempfile.mkdtemp(),
        )
        fake = FakeLLMClient(
            {
                "business_intent": {
                    "question_family": "paid_amount_change_explanation",
                    "question_families": [
                        "paid_amount_change_explanation",
                        "segment_or_factor_attribution",
                    ],
                    "target_metric": "paid_amount",
                    "scope": "WajeSpecial_vs_other_channels",
                    "time_window": "2026-01-01..2026-06-30",
                    "pattern_family": "custom_baseline",
                    "target_claim": "comparative_change",
                },
                "boundary_decision": {
                    "boundary_status": "needs_question",
                    "recommended_assumption": "先按日均付费金额比较，再拆主要贡献项。",
                    "clarification_questions": [
                        {
                            "question": "先看日均可比表现还是总额贡献？",
                            "options": ["按推荐继续", "只看总额"],
                        }
                    ],
                    "decision_summary": "指标口径会影响最终回答质量。",
                },
            }
        )

        state = run_one_node(state, "understand_business_intent", llm_client=fake)
        state = run_one_node(state, "decide_question_boundary", llm_client=fake)

        self.assertEqual(state["boundary_decision"]["boundary_status"], "needs_question")


if __name__ == "__main__":
    unittest.main()
