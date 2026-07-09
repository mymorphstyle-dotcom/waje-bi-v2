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

    def test_data_quality_probe_includes_business_measure_fields(self):
        specs = build_clickhouse_query_specs(
            {
                "windows": {"target": "yesterday", "history_days": 36},
                "baselines": ("previous_day",),
                "query_intents": ("data_quality_probe",),
                "row_shapes": (
                    {
                        "required_fields": (
                            "period",
                            "group",
                            "amount",
                            "paid_users",
                            "orders",
                            "first_paid_users",
                        ),
                    },
                ),
            },
            table="paid_order_success_clean_20240101_20260704",
            run_id="run-quality",
        )

        self.assertEqual(len(specs), 1)
        self.assertIn("sum(paid_amount_ngn) AS amount", specs[0]["sql_text"])
        self.assertIn("countIf(is_first_payment = '1') AS first_paid_users", specs[0]["sql_text"])
        self.assertIn("amount", specs[0]["required_fields"])
        self.assertIn("first_paid_users", specs[0]["required_fields"])

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

    def test_custom_baseline_with_explicit_ranges_builds_deterministic_query(self):
        specs = build_clickhouse_query_specs(
            {
                "windows": {
                    "target": "2026-04-01..2026-06-30",
                    "baseline": "2026-01-01..2026-03-31",
                },
                "baselines": ("custom_baseline",),
                "query_intents": ("daily_metric_baselines",),
                "row_shapes": (
                    {
                        "required_fields": ("period", "group", "amount"),
                    },
                ),
            },
            table="paid_order_success_clean_20240101_20260704",
            run_id="run-custom",
        )

        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0]["reason"], "")
        self.assertIn("toDate('2026-04-01')", specs[0]["sql_text"])
        self.assertIn("toDate('2026-06-30')", specs[0]["sql_text"])
        self.assertIn("toDate('2026-01-01')", specs[0]["sql_text"])
        self.assertIn("toDate('2026-03-31')", specs[0]["sql_text"])
        self.assertNotIn("now('Africa/Lagos')", specs[0]["sql_text"])

    def test_custom_baseline_without_bound_dates_returns_blocked_reason(self):
        specs = build_clickhouse_query_specs(
            {
                "windows": {"target": "Q2", "baseline": "Q1"},
                "baselines": ("custom_baseline",),
                "query_intents": ("daily_metric_baselines",),
            },
            table="paid_order_success_clean_20240101_20260704",
            run_id="run-custom",
        )

        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0]["sql_text"], "")
        self.assertEqual(specs[0]["reason"], "custom_baseline_window_unbound")

    def test_dimension_scan_with_unsafe_row_shape_dimensions_returns_blocked_reason(self):
        specs = build_clickhouse_query_specs(
            {
                "query_intents": ("dimension_scan",),
                "row_shapes": (
                    {
                        "required_fields": ("period", "group", "amount"),
                        "dimension_keys": ("channel;DROP",),
                    },
                ),
            },
            table="paid_order_success_clean_20240101_20260704",
            run_id="run-unsafe-dimension",
        )

        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0]["sql_text"], "")
        self.assertEqual(specs[0]["reason"], "unsafe_dimension_keys")


if __name__ == "__main__":
    unittest.main()
