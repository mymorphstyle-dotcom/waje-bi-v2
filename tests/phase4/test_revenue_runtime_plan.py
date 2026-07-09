import unittest

from bi_agent.runtime.revenue_runtime_plan import build_revenue_runtime_plan


class RevenueRuntimePlanTest(unittest.TestCase):
    def test_multi_baseline_question_compiles_windows_and_params(self):
        plan = build_revenue_runtime_plan(
            target_metric="paid_amount",
            accepted_graph=(
                "data_quality_profile",
                "compare_periods",
                "rolling_window_compare",
                "driver_decomposition",
                "answer_verify",
            ),
            diagnostic_axes=("multi_baseline",),
            question_text="相比前一天、近 7 日均值、上周同日，昨天付费金额为什么变化？",
            prior_assets=(),
        )

        self.assertEqual(plan["windows"]["target"], "yesterday")
        self.assertEqual(
            plan["baselines"],
            ("previous_day", "rolling_7_day_baseline", "same_weekday_last_week"),
        )
        self.assertEqual(
            plan["capability_params"]["rolling_window_compare"]["window_days"],
            7,
        )
        self.assertIn("daily_metric_baselines", plan["query_intents"])

    def test_factor_topk_compiles_dimension_candidates(self):
        plan = build_revenue_runtime_plan(
            target_metric="paid_amount",
            accepted_graph=(
                "segment_contribution",
                "joint_attribution",
                "driver_decomposition",
                "answer_verify",
            ),
            diagnostic_axes=("factor_topk",),
            question_text="昨天收入变化最大的是哪个一级渠道、地区、设备、包、支付方式或玩法？",
            prior_assets=(),
        )

        dimensions = plan["dimension_candidates"]
        self.assertIn(
            {"field": "channel", "business_name": "一级渠道", "required": True},
            dimensions,
        )
        self.assertIn(
            {"field": "payment_method", "business_name": "支付方式", "required": False},
            dimensions,
        )
        self.assertEqual(
            plan["capability_params"]["joint_attribution"]["max_dimension_count"],
            2,
        )
        self.assertEqual(plan["capability_params"]["segment_contribution"]["top_k"], 5)

    def test_prior_assets_reduce_repeated_scans(self):
        plan = build_revenue_runtime_plan(
            target_metric="paid_amount",
            accepted_graph=("segment_contribution", "answer_verify"),
            diagnostic_axes=("factor_topk",),
            question_text="继续看哪个渠道影响最大",
            prior_assets=(
                {
                    "asset_type": "dimension_scan",
                    "dimension": "channel",
                    "status": "usable",
                    "query_ref": "query:channel-scan",
                },
            ),
        )

        self.assertIn("query:channel-scan", plan["asset_inputs_used"])
        self.assertIn("dimension_scan_reuse", plan["query_intents"])


if __name__ == "__main__":
    unittest.main()
