import unittest

from bi_agent.runtime.clickhouse_query_planner import build_clickhouse_query_specs


class ClickHouseQueryPlannerTest(unittest.TestCase):
    def test_builds_baseline_and_dimension_scan_queries(self):
        specs = build_clickhouse_query_specs(
            {
                "windows": {"target": "yesterday", "history_days": 36},
                "baselines": ("previous_day", "rolling_7_day_baseline"),
                "query_intents": ("daily_metric_baselines", "dimension_scan"),
                "dimension_candidates": (
                    {"field": "channel", "business_name": "一级渠道", "required": True},
                    {"field": "payment_method", "business_name": "支付方式", "required": False},
                ),
                "row_shapes": (
                    {
                        "required_fields": ("period", "group", "amount", "paid_users", "orders"),
                        "dimension_keys": ("channel", "payment_method"),
                    },
                ),
            },
            table="paid_order_success_clean_20240101_20260704",
            run_id="run-1",
        )

        intents = {spec["intent"] for spec in specs}
        self.assertIn("daily_metric_baselines", intents)
        self.assertIn("dimension_scan", intents)
        self.assertTrue(all("GROUP BY" in spec["sql_text"] for spec in specs))
        self.assertTrue(
            all("paid_order_success_clean_20240101_20260704" in spec["sql_text"] for spec in specs)
        )
        self.assertTrue(any("rolling_7_day_baseline" in spec["sql_text"] for spec in specs))

    def test_unsafe_table_returns_no_specs(self):
        specs = build_clickhouse_query_specs(
            {
                "query_intents": ("daily_metric_baselines",),
                "row_shapes": ({"required_fields": ("amount",)},),
            },
            table="paid_order; DROP TABLE x",
            run_id="run-1",
        )

        self.assertEqual(specs, ())


if __name__ == "__main__":
    unittest.main()
