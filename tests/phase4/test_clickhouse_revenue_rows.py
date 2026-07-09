import unittest

from bi_agent.runtime.clickhouse_revenue_rows import ClickHouseRevenueRows
from bi_agent.runtime.clickhouse_runtime import ClickHouseQueryResult


class FakeRuntime:
    def __init__(self, rows=(), ok=True, reason="", rows_by_query_id=None):
        self.rows = tuple(rows)
        self.rows_by_query_id = {
            str(query_id): tuple(query_rows)
            for query_id, query_rows in (rows_by_query_id or {}).items()
        }
        self.ok = ok
        self.reason = reason
        self.calls = []
        self.binding = type("Binding", (), {"ok": True, "reason": ""})()

    def configured(self):
        return self.binding.ok

    def aggregate(self, sql, query_id):
        self.calls.append((sql, query_id))
        rows = self.rows_by_query_id.get(query_id, self.rows)
        return ClickHouseQueryResult(
            ok=self.ok,
            reason=self.reason,
            rows=rows,
            query_hash=f"hash-{query_id}",
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

    def test_plan_uses_compiler_query_specs_before_graph_fallback(self):
        provider = ClickHouseRevenueRows(
            runtime=FakeRuntime(),
            table="paid_order_success_clean_20240101_20260704",
        )
        plan = provider.plan(
            {
                "run_id": "run-compiler-plan",
                "compiler_runtime_plan": {
                    "windows": {"target": "yesterday", "history_days": 12},
                    "baselines": ("previous_day",),
                    "query_intents": ("dimension_scan",),
                    "row_shapes": [
                        {
                            "dimension_keys": ("channel",),
                            "required_fields": (
                                "period",
                                "group",
                                "amount",
                                "paid_users",
                                "orders",
                            ),
                        }
                    ],
                },
            },
            {"time_window": "yesterday"},
            ("compare_periods",),
        )

        self.assertEqual(plan.query_id, "run-compiler-plan:dimension_scan")
        self.assertEqual(plan.dimension_keys, ("channel",))
        self.assertIn("GROUP BY period, group, channel", plan.sql_text)
        self.assertIn("- 12", plan.sql_text)

    def test_plan_prefers_joint_scan_when_multi_intent_graph_needs_dimensions(self):
        provider = ClickHouseRevenueRows(
            runtime=FakeRuntime(),
            table="paid_order_success_clean_20240101_20260704",
        )
        plan = provider.plan(
            {
                "run_id": "run-multi-intent",
                "compiler_runtime_plan": {
                    "windows": {"target": "yesterday", "history_days": 12},
                    "baselines": ("previous_day",),
                    "query_intents": ("daily_metric_baselines", "joint_candidate_scan"),
                    "capability_params": {"joint_attribution": {"max_dimension_count": 2}},
                    "row_shapes": [
                        {
                            "dimension_keys": ("channel", "payment_method", "region"),
                            "required_fields": (
                                "period",
                                "group",
                                "amount",
                                "orders",
                            ),
                        }
                    ],
                },
            },
            {"time_window": "yesterday"},
            ("compare_periods", "joint_attribution"),
        )

        self.assertEqual(plan.query_id, "run-multi-intent:joint_candidate_scan")
        self.assertEqual(plan.dimension_keys, ("channel", "payment_method"))
        self.assertIn("GROUP BY period, group, channel, payment_method", plan.sql_text)

    def test_plan_blocks_unbound_custom_baseline_windows(self):
        provider = ClickHouseRevenueRows(
            runtime=FakeRuntime(),
            table="paid_order_success_clean_20240101_20260704",
        )
        plan = provider.plan(
            {
                "run_id": "run-custom-baseline",
                "compiler_runtime_plan": {
                    "windows": {"target": "Q2", "baseline": "Q1"},
                    "baselines": ("custom_baseline",),
                    "query_intents": ("daily_metric_baselines",),
                },
            },
            {"time_window": "2026-01-01..2026-06-30"},
            ("compare_periods",),
        )

        self.assertEqual(plan.sql_text, "")
        self.assertEqual(plan.reason, "custom_baseline_window_unbound")
        self.assertEqual(plan.query_id, "run-custom-baseline:daily_metric_baselines")

    def test_plan_prefers_executable_dimension_scan_when_reuse_is_blocked(self):
        provider = ClickHouseRevenueRows(
            runtime=FakeRuntime(),
            table="paid_order_success_clean_20240101_20260704",
        )
        plan = provider.plan(
            {
                "run_id": "run-reuse",
                "compiler_runtime_plan": {
                    "windows": {"target": "yesterday", "history_days": 12},
                    "baselines": ("previous_day",),
                    "query_intents": ("daily_metric_baselines", "dimension_scan_reuse"),
                    "row_shapes": [
                        {
                            "dimension_keys": ("channel",),
                            "required_fields": (
                                "period",
                                "group",
                                "amount",
                                "orders",
                            ),
                        }
                    ],
                },
            },
            {"time_window": "yesterday"},
            ("segment_contribution",),
        )

        self.assertIn("SELECT", plan.sql_text)
        self.assertEqual(plan.reason, "")
        self.assertEqual(plan.query_id, "run-reuse:daily_metric_baselines")

    def test_plan_prefers_executable_baseline_when_event_probe_is_unbound(self):
        provider = ClickHouseRevenueRows(
            runtime=FakeRuntime(),
            table="paid_order_success_clean_20240101_20260704",
        )
        plan = provider.plan(
            {
                "run_id": "run-event",
                "compiler_runtime_plan": {
                    "windows": {"target": "yesterday", "history_days": 12},
                    "baselines": ("previous_day",),
                    "query_intents": ("daily_metric_baselines", "event_context_probe"),
                },
            },
            {"time_window": "yesterday"},
            ("event_evidence",),
        )

        self.assertIn("SELECT", plan.sql_text)
        self.assertEqual(plan.reason, "")
        self.assertEqual(plan.query_id, "run-event:daily_metric_baselines")

    def test_plan_prefers_executable_baseline_query_when_event_probe_is_blocked(self):
        provider = ClickHouseRevenueRows(
            runtime=FakeRuntime(),
            table="paid_order_success_clean_20240101_20260704",
        )
        plan = provider.plan(
            {
                "run_id": "run-event-fallback",
                "compiler_runtime_plan": {
                    "windows": {"target": "yesterday", "history_days": 12},
                    "baselines": ("previous_day",),
                    "query_intents": ("daily_metric_baselines", "event_context_probe"),
                },
            },
            {"time_window": "yesterday"},
            ("compare_periods", "driver_decomposition", "event_evidence"),
        )

        self.assertIn("SELECT", plan.sql_text)
        self.assertEqual(plan.reason, "")
        self.assertEqual(plan.query_id, "run-event-fallback:daily_metric_baselines")

    def test_plan_with_explicit_dimension_scan_and_empty_dimensions_stays_blocked(self):
        provider = ClickHouseRevenueRows(
            runtime=FakeRuntime(),
            table="paid_order_success_clean_20240101_20260704",
        )
        plan = provider.plan(
            {
                "run_id": "run-empty-dimension-scan",
                "compiler_runtime_plan": {
                    "windows": {"target": "yesterday", "history_days": 12},
                    "query_intents": ("dimension_scan",),
                    "row_shapes": [
                        {
                            "required_fields": ("period", "group", "amount"),
                            "dimension_keys": (),
                        }
                    ],
                },
            },
            {"time_window": "yesterday"},
            ("segment_contribution",),
        )

        self.assertEqual(plan.sql_text, "")
        self.assertEqual(plan.reason, "missing_dimension_keys")
        self.assertEqual(plan.query_id, "run-empty-dimension-scan:dimension_scan")

    def test_plan_with_unsafe_compiler_dimension_does_not_emit_dimension_sql(self):
        provider = ClickHouseRevenueRows(
            runtime=FakeRuntime(),
            table="paid_order_success_clean_20240101_20260704",
        )
        plan = provider.plan(
            {
                "run_id": "run-unsafe-dimension-scan",
                "compiler_runtime_plan": {
                    "windows": {"target": "yesterday", "history_days": 12},
                    "query_intents": ("dimension_scan",),
                    "row_shapes": [
                        {
                            "required_fields": ("period", "group", "amount"),
                            "dimension_keys": ("channel;DROP",),
                        }
                    ],
                },
            },
            {"time_window": "yesterday"},
            ("segment_contribution",),
        )

        self.assertEqual(plan.sql_text, "")
        self.assertEqual(plan.reason, "unsafe_dimension_keys")
        self.assertNotIn("channel;DROP", plan.sql_text)

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
        self.assertEqual(result.result_refs, ("hash-run-1:clickhouse_revenue_rows",))

    def test_fetch_executes_all_compiler_query_specs_and_groups_rows_by_intent(self):
        runtime = FakeRuntime(
            rows_by_query_id={
                "run-multi:daily_metric_baselines": (
                    {"period": "2026-07-07", "group": "baseline", "amount": 90.0, "orders": 9},
                    {"period": "2026-07-08", "group": "target", "amount": 120.0, "orders": 10},
                ),
                "run-multi:dimension_scan": (
                    {
                        "period": "2026-07-08",
                        "group": "target",
                        "channel": "ads",
                        "amount": 80.0,
                        "orders": 7,
                    },
                ),
                "run-multi:data_quality_probe": (
                    {
                        "period": "2026-07-08",
                        "group": "target",
                        "orders": 10,
                        "paid_users": 8,
                        "min_period": "2026-07-01",
                        "max_period": "2026-07-08",
                    },
                ),
            }
        )
        provider = ClickHouseRevenueRows(
            runtime=runtime,
            table="paid_order_success_clean_20240101_20260704",
        )
        plan = provider.plan(
            {
                "run_id": "run-multi",
                "compiler_runtime_plan": {
                    "windows": {"target": "yesterday", "history_days": 12},
                    "baselines": ("previous_day",),
                    "query_intents": (
                        "daily_metric_baselines",
                        "dimension_scan",
                        "data_quality_probe",
                    ),
                    "row_shapes": [
                        {
                            "dimension_keys": ("channel",),
                            "required_fields": (
                                "period",
                                "group",
                                "amount",
                                "orders",
                                "paid_users",
                            ),
                        }
                    ],
                },
            },
            {"time_window": "yesterday"},
            ("compare_periods", "segment_contribution", "data_quality_profile"),
        )

        result = provider.fetch(plan)

        self.assertTrue(result.ok)
        self.assertEqual(
            [query_id for _, query_id in runtime.calls],
            [
                "run-multi:daily_metric_baselines",
                "run-multi:dimension_scan",
                "run-multi:data_quality_probe",
            ],
        )
        self.assertEqual(result.rows_by_intent["daily_metric_baselines"][0]["amount"], 90.0)
        self.assertEqual(result.rows_by_intent["dimension_scan"][0]["channel"], "ads")
        self.assertEqual(result.rows_by_intent["data_quality_probe"][0]["orders"], 10)
        self.assertEqual(
            result.result_refs_by_intent["dimension_scan"],
            ("hash-run-multi:dimension_scan",),
        )

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
