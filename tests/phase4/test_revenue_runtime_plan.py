import unittest

from bi_agent.runtime.analysis_assets import build_dimension_scan_reuse_contract
from bi_agent.runtime.revenue_runtime_plan import build_revenue_runtime_plan


class RevenueRuntimePlanTest(unittest.TestCase):
    def test_bound_context_takes_precedence_over_day_over_day_text(self):
        plan = build_revenue_runtime_plan(
            target_metric="paid_amount",
            accepted_graph=(
                "compare_periods",
                "driver_decomposition",
                "answer_verify",
            ),
            diagnostic_axes=(),
            question_text="昨天付费金额为什么变化？",
            bound_context={
                "pattern_family": "custom_baseline",
                "time_window": "2026-04-01..2026-06-30",
                "baseline": {"label": "2026Q1"},
                "target": {"label": "2026Q2"},
            },
            prior_assets=(),
        )

        self.assertEqual(
            plan["windows"],
            {
                "target": "2026Q2",
                "baseline": "2026Q1",
                "time_window": "2026-04-01..2026-06-30",
            },
        )
        self.assertEqual(plan["baselines"], ("custom_baseline",))
        self.assertEqual(
            plan["capability_params"]["compare_periods"]["baselines"],
            ("custom_baseline",),
        )

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

    def test_candidate_only_dimensions_require_contract_gap_descriptors(self):
        plan = build_revenue_runtime_plan(
            target_metric="paid_amount",
            accepted_graph=(
                "segment_contribution",
                "joint_attribution",
                "answer_verify",
            ),
            diagnostic_axes=("factor_topk",),
            question_text="昨天收入变化最大的是哪个一级渠道、地区、设备、包、支付方式或玩法？",
            prior_assets=(),
        )

        row_shape = plan["row_shapes"][0]
        gap_fields = {
            field
            for gap in row_shape["contract_gaps"]
            for field in (gap.get("fields") or ())
        }
        dimension_keys = set(row_shape["dimension_keys"])

        for dimension in plan["dimension_candidates"]:
            field = dimension["field"]
            if field in dimension_keys:
                continue
            self.assertIn(field, gap_fields)
        self.assertIn("package_name_contract_missing", {gap["gap_id"] for gap in row_shape["contract_gaps"]})
        self.assertIn("gameplay_contract_missing", {gap["gap_id"] for gap in row_shape["contract_gaps"]})

    def test_prior_assets_reduce_repeated_scans(self):
        plan = build_revenue_runtime_plan(
            target_metric="paid_amount",
            accepted_graph=("segment_contribution", "answer_verify"),
            diagnostic_axes=("factor_topk",),
            question_text="继续看哪个渠道影响最大",
            prior_assets=(
                {
                    "asset_type": "dimension_scan",
                    "dimensions": ("channel",),
                    "status": "usable",
                    "query_ref": "query:channel-scan",
                    "reuse_contract": build_dimension_scan_reuse_contract(
                        target_metric="paid_amount",
                        scope="full_sample",
                        time_window="2026-07-08",
                        windows={"target": "2026-07-08", "baseline": "2026-07-07"},
                        baselines=("previous_day",),
                        permission_scope="analyst",
                        snapshot_version="2026H1",
                        dimensions=("channel",),
                        required_fields=(
                            "period",
                            "group",
                            "amount",
                            "paid_users",
                            "orders",
                            "first_paid_users",
                        ),
                        contract_versions={"runtime": "contract-v1"},
                        schema_fingerprint="schema-v1",
                    ),
                    "created_at": "2026-07-08T08:00:00+00:00",
                    "expires_at": "2026-07-10T08:00:00+00:00",
                    "row_payload": {
                        "rows": (
                            {
                                "period": "2026-07-07",
                                "group": "baseline",
                                "amount": 100,
                                "paid_users": 10,
                                "orders": 12,
                                "first_paid_users": 3,
                                "channel": "A",
                            },
                            {
                                "period": "2026-07-08",
                                "group": "target",
                                "amount": 130,
                                "paid_users": 11,
                                "orders": 14,
                                "first_paid_users": 4,
                                "channel": "A",
                            },
                        ),
                        "row_count": 2,
                        "truncated": False,
                    },
                },
            ),
            bound_context={
                "scope": "full_sample",
                "time_window": "2026-07-08",
                "windows": {"target": "2026-07-08", "baseline": "2026-07-07"},
                "baselines": ("previous_day",),
                "permission_scope": "analyst",
                "snapshot_version": "2026H1",
                "contract_versions": {"runtime": "contract-v1"},
                "schema_fingerprint": "schema-v1",
            },
        )

        self.assertIn("query:channel-scan", plan["asset_inputs_used"])
        self.assertIn("dimension_scan_reuse", plan["query_intents"])
        self.assertNotIn("dimension_scan", plan["query_intents"])
        self.assertEqual(plan["asset_row_inputs"][0]["query_ref"], "query:channel-scan")
        self.assertEqual(len(plan["asset_row_inputs"][0]["rows"]), 2)
        self.assertEqual(plan["asset_row_inputs"][0]["rows"][0]["channel"], "A")

    def test_non_matching_prior_assets_do_not_suppress_needed_scan(self):
        plan = build_revenue_runtime_plan(
            target_metric="paid_amount",
            accepted_graph=("segment_contribution", "answer_verify"),
            diagnostic_axes=("factor_topk",),
            question_text="继续看哪个渠道影响最大",
            prior_assets=(
                {
                    "asset_type": "dimension_scan",
                    "dimensions": ("region",),
                    "status": "usable",
                    "query_ref": "query:region-scan",
                },
                {
                    "asset_type": "segment_contribution",
                    "dimension": "channel",
                    "status": "usable",
                    "query_ref": "query:channel-contribution",
                },
            ),
        )

        self.assertEqual(plan["asset_inputs_used"], ())
        self.assertNotIn("dimension_scan_reuse", plan["query_intents"])
        self.assertIn("dimension_scan", plan["query_intents"])

    def test_stale_prior_dimension_scan_does_not_suppress_fresh_scan(self):
        plan = build_revenue_runtime_plan(
            target_metric="paid_amount",
            accepted_graph=("segment_contribution", "answer_verify"),
            diagnostic_axes=("factor_topk",),
            question_text="继续看哪个渠道影响最大",
            bound_context={
                "scope": "full_sample",
                "time_window": "2026-07-09",
                "windows": {"target": "2026-07-09", "baseline": "2026-07-08"},
                "baselines": ("previous_day",),
                "permission_scope": "analyst",
                "snapshot_version": "2026H1",
            },
            prior_assets=(
                {
                    "asset_type": "dimension_scan",
                    "dimensions": ("channel",),
                    "status": "usable",
                    "query_ref": "query:stale-channel-scan",
                    "reuse_contract": {
                        "target_metric": "paid_amount",
                        "scope": "full_sample",
                        "time_window": "2026-07-09",
                        "windows": {"target": "2026-07-09", "baseline": "2026-07-08"},
                        "baselines": ("previous_day",),
                        "permission_scope": "analyst",
                        "snapshot_version": "2026H1",
                        "contract_signature": "scan:channel:paid_amount:full_sample",
                    },
                    "created_at": "2026-07-07T08:00:00+00:00",
                    "expires_at": "2026-07-08T08:00:00+00:00",
                    "row_payload": {"rows": (), "row_count": 0, "truncated": False},
                },
            ),
        )

        self.assertEqual(plan["asset_inputs_used"], ())
        self.assertEqual(plan["asset_row_inputs"], ())
        self.assertIn("dimension_scan", plan["query_intents"])

    def test_permission_mismatch_does_not_reuse_prior_dimension_scan(self):
        plan = build_revenue_runtime_plan(
            target_metric="paid_amount",
            accepted_graph=("segment_contribution", "answer_verify"),
            diagnostic_axes=("factor_topk",),
            question_text="继续看哪个渠道影响最大",
            bound_context={
                "scope": "full_sample",
                "time_window": "2026-07-09",
                "windows": {"target": "2026-07-09", "baseline": "2026-07-08"},
                "baselines": ("previous_day",),
                "permission_scope": "business_reader",
                "snapshot_version": "2026H1",
            },
            prior_assets=(
                {
                    "asset_type": "dimension_scan",
                    "dimensions": ("channel",),
                    "status": "usable",
                    "query_ref": "query:analyst-channel-scan",
                    "reuse_contract": {
                        "target_metric": "paid_amount",
                        "scope": "full_sample",
                        "time_window": "2026-07-09",
                        "windows": {"target": "2026-07-09", "baseline": "2026-07-08"},
                        "baselines": ("previous_day",),
                        "permission_scope": "analyst",
                        "snapshot_version": "2026H1",
                        "contract_signature": "scan:channel:paid_amount:full_sample",
                    },
                    "created_at": "2026-07-09T08:00:00+00:00",
                    "expires_at": "2026-07-10T08:00:00+00:00",
                    "row_payload": {
                        "rows": (
                            {
                                "period": "2026-07-09",
                                "group": "target",
                                "amount": 130,
                                "paid_users": 11,
                                "orders": 14,
                                "first_paid_users": 4,
                                "channel": "A",
                            },
                        ),
                        "row_count": 1,
                        "truncated": False,
                    },
                },
            ),
        )

        self.assertEqual(plan["asset_inputs_used"], ())
        self.assertEqual(plan["asset_row_inputs"], ())
        self.assertIn("dimension_scan", plan["query_intents"])

    def test_scope_mismatch_does_not_reuse_prior_dimension_scan(self):
        plan = build_revenue_runtime_plan(
            target_metric="paid_amount",
            accepted_graph=("segment_contribution", "answer_verify"),
            diagnostic_axes=("factor_topk",),
            question_text="继续看哪个渠道影响最大",
            bound_context={
                "scope": "new_users",
                "time_window": "2026-07-09",
                "windows": {"target": "2026-07-09", "baseline": "2026-07-08"},
                "baselines": ("previous_day",),
                "permission_scope": "analyst",
                "snapshot_version": "2026H1",
            },
            prior_assets=(
                {
                    "asset_type": "dimension_scan",
                    "dimensions": ("channel",),
                    "status": "usable",
                    "query_ref": "query:full-sample-channel-scan",
                    "reuse_contract": {
                        "target_metric": "paid_amount",
                        "scope": "full_sample",
                        "time_window": "2026-07-09",
                        "windows": {"target": "2026-07-09", "baseline": "2026-07-08"},
                        "baselines": ("previous_day",),
                        "permission_scope": "analyst",
                        "snapshot_version": "2026H1",
                        "contract_signature": "scan:channel:paid_amount:full_sample",
                    },
                    "created_at": "2026-07-09T08:00:00+00:00",
                    "expires_at": "2026-07-10T08:00:00+00:00",
                    "row_payload": {
                        "rows": (
                            {
                                "period": "2026-07-09",
                                "group": "target",
                                "amount": 130,
                                "paid_users": 11,
                                "orders": 14,
                                "first_paid_users": 4,
                                "channel": "A",
                            },
                        ),
                        "row_count": 1,
                        "truncated": False,
                    },
                },
            ),
        )

        self.assertEqual(plan["asset_inputs_used"], ())
        self.assertEqual(plan["asset_row_inputs"], ())
        self.assertIn("dimension_scan", plan["query_intents"])

    def test_missing_row_payload_does_not_reuse_prior_dimension_scan(self):
        plan = build_revenue_runtime_plan(
            target_metric="paid_amount",
            accepted_graph=("segment_contribution", "answer_verify"),
            diagnostic_axes=("factor_topk",),
            question_text="继续看哪个渠道影响最大",
            bound_context={
                "scope": "full_sample",
                "time_window": "2026-07-09",
                "windows": {"target": "2026-07-09", "baseline": "2026-07-08"},
                "baselines": ("previous_day",),
                "permission_scope": "analyst",
                "snapshot_version": "2026H1",
            },
            prior_assets=(
                {
                    "asset_type": "dimension_scan",
                    "dimensions": ("channel",),
                    "status": "usable",
                    "query_ref": "query:missing-rows",
                    "reuse_contract": {
                        "target_metric": "paid_amount",
                        "scope": "full_sample",
                        "time_window": "2026-07-09",
                        "windows": {"target": "2026-07-09", "baseline": "2026-07-08"},
                        "baselines": ("previous_day",),
                        "permission_scope": "analyst",
                        "snapshot_version": "2026H1",
                        "contract_signature": "scan:channel:paid_amount:full_sample",
                    },
                    "created_at": "2026-07-09T08:00:00+00:00",
                    "expires_at": "2026-07-10T08:00:00+00:00",
                    "row_payload": {"rows": (), "row_count": 2, "truncated": False},
                },
            ),
        )

        self.assertEqual(plan["asset_inputs_used"], ())
        self.assertEqual(plan["asset_row_inputs"], ())
        self.assertIn("dimension_scan", plan["query_intents"])


if __name__ == "__main__":
    unittest.main()
