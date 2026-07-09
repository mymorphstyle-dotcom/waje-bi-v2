import unittest

from bi_agent.runtime.clickhouse_revenue_rows import ClickHouseRevenueRows
from bi_agent.runtime.clickhouse_runtime import ClickHouseQueryResult


class FakeRuntime:
    def __init__(self, rows=(), ok=True, reason=""):
        self.rows = tuple(rows)
        self.ok = ok
        self.reason = reason
        self.calls = []
        self.binding = type("Binding", (), {"ok": True, "reason": ""})()

    def configured(self):
        return self.binding.ok

    def aggregate(self, sql, query_id):
        self.calls.append((sql, query_id))
        return ClickHouseQueryResult(
            ok=self.ok,
            reason=self.reason,
            rows=self.rows,
            query_hash="hash-real",
            query_id=query_id,
        )


class ClickHouseRevenueRowsTest(unittest.TestCase):
    def test_plans_aggregate_only_rows_for_driver_and_joint_attribution(self):
        provider = ClickHouseRevenueRows(
            runtime=FakeRuntime(),
            table="paid_order_success_clean_20240101_20260704",
        )
        plan = provider.plan(
            {"run_id": "run-1"},
            {
                "question_family": "paid_amount_change_explanation",
                "target_metric": "paid_amount",
                "scope": "full_sample",
                "time_window": "yesterday",
            },
            ("compare_periods", "driver_decomposition", "joint_attribution"),
        )

        self.assertIn("SELECT", plan.sql_text)
        self.assertIn("sum(paid_amount_ngn) AS amount", plan.sql_text)
        self.assertIn("uniqExact(user_id) AS paid_users", plan.sql_text)
        self.assertIn("count() AS orders", plan.sql_text)
        self.assertIn("channel", plan.dimension_keys)
        self.assertIn("payment_method", plan.dimension_keys)
        self.assertIn("amount", plan.required_fields)

    def test_plan_uses_compiler_runtime_row_shape_dimensions(self):
        provider = ClickHouseRevenueRows(
            runtime=FakeRuntime(),
            table="paid_order_success_clean_20240101_20260704",
        )
        plan = provider.plan(
            {
                "run_id": "run-compiler-plan",
                "compiler_runtime_plan": {
                    "row_shapes": [
                        {
                            "dimension_keys": ("channel", "payment_method", "region"),
                            "required_fields": ("period", "group", "amount", "orders"),
                        }
                    ]
                },
            },
            {"time_window": "yesterday"},
            ("segment_contribution",),
        )

        self.assertEqual(plan.dimension_keys, ("channel", "payment_method", "region"))
        self.assertEqual(plan.required_fields, ("period", "group", "amount", "orders"))

    def test_fetch_returns_bounded_aggregate_rows_and_query_ref(self):
        runtime = FakeRuntime(
            rows=({"period": "2026-07-08", "group": "target", "amount": 120.0},)
        )
        provider = ClickHouseRevenueRows(
            runtime=runtime,
            table="paid_order_success_clean_20240101_20260704",
        )
        plan = provider.plan(
            {"run_id": "run-1"},
            {"time_window": "yesterday"},
            ("compare_periods",),
        )
        result = provider.fetch(plan)

        self.assertTrue(result.ok)
        self.assertEqual(result.rows[0]["amount"], 120.0)
        self.assertEqual(result.query_id, plan.query_id)
        self.assertEqual(result.result_refs, ("hash-real",))

    def test_fetch_blocks_when_runtime_query_fails(self):
        provider = ClickHouseRevenueRows(
            runtime=FakeRuntime(ok=False, reason="clickhouse_query_failed"),
            table="paid_order_success_clean_20240101_20260704",
        )
        plan = provider.plan(
            {"run_id": "run-1"},
            {"time_window": "yesterday"},
            ("compare_periods",),
        )
        result = provider.fetch(plan)

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "clickhouse_query_failed")

    def test_unsafe_table_identifier_is_blocked_before_runtime_call(self):
        runtime = FakeRuntime()
        provider = ClickHouseRevenueRows(
            runtime=runtime,
            table="paid_order_success_clean_20240101_20260704; DROP TABLE raw",
        )
        plan = provider.plan(
            {"run_id": "run-1"},
            {"time_window": "yesterday"},
            ("compare_periods",),
        )
        result = provider.fetch(plan)

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "invalid_identifier")
        self.assertEqual(runtime.calls, [])


if __name__ == "__main__":
    unittest.main()
