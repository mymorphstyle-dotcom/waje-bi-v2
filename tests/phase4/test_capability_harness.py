import unittest

from bi_agent.capabilities.driver_decomposition import driver_decomposition
from bi_agent.capabilities.outlier_contribution import outlier_contribution
from bi_agent.capabilities.segment_contribution import segment_contribution
from bi_agent.runtime.capability_harness import execute_capability
from bi_agent.runtime.capability_models import BudgetState, CapabilityRequest


def run_capability(capability_id, params):
    value = dict(params)
    rows = value.pop("rows")
    if capability_id == "outlier_contribution":
        return outlier_contribution(rows, **value)
    if capability_id == "high_value_user_contribution":
        from bi_agent.capabilities.high_value_user_contribution import (
            high_value_user_contribution,
        )

        return high_value_user_contribution(rows, **value)
    if capability_id == "user_mix_contribution":
        from bi_agent.capabilities.user_mix_contribution import user_mix_contribution

        return user_mix_contribution(rows, **value)
    raise KeyError(capability_id)


class CapabilityHarnessTest(unittest.TestCase):
    def test_pattern_scan_returns_normalized_envelope(self):
        request = CapabilityRequest(
            run_id="run-1",
            accepted_graph_id="graph-1",
            graph_version=1,
            capability_id="compare_period_phases",
            question_family="pattern_explanation",
            target_claim="recurring_pattern_existence",
            claim_type="recurring_pattern_existence",
            metric="paid_amount_ngn",
            scope="all_successful_paid_orders",
            time_window="2026-01..2026-03",
            baseline={"label": "middle_or_end"},
            target={"label": "start"},
            grain="month",
            filters={},
            dimensions=(),
            contract_versions={"metric": "paid_amount.v1"},
            role="analyst",
            budget_state=BudgetState(
                mode="research",
                used_capability_calls=0,
                soft_limit=50,
                hard_limit=100,
            ),
            llm_business_reason="Check whether month start is higher than sibling phases.",
            params={
                "rows": [
                    {"month": "2026-01", "phase": "start", "amount": 130},
                    {"month": "2026-01", "phase": "middle", "amount": 100},
                    {"month": "2026-01", "phase": "end", "amount": 90},
                    {"month": "2026-02", "phase": "start", "amount": 140},
                    {"month": "2026-02", "phase": "middle", "amount": 100},
                    {"month": "2026-02", "phase": "end", "amount": 90},
                ],
                "result_refs": ("sqlhash-1",),
                "pattern_family": "intra_period",
                "target_phase": "start",
                "min_periods": 2,
            },
        )

        envelope = execute_capability(request)

        self.assertEqual(envelope.capability_id, "compare_period_phases")
        self.assertEqual(envelope.target_label, "start")
        self.assertEqual(envelope.baseline_label, "middle_or_end")
        self.assertEqual(envelope.result_refs, ("sqlhash-1",))
        self.assertIn("median_uplift", envelope.numeric_facts)

    def test_compare_periods_public_capability_uses_custom_baseline_scan(self):
        request = CapabilityRequest(
            run_id="run-custom-baseline",
            accepted_graph_id="graph-1",
            graph_version=1,
            capability_id="compare_periods",
            question_family="custom_baseline_comparison",
            target_claim="comparative_change",
            claim_type="comparative_change",
            metric="paid_amount_ngn",
            scope="all_successful_paid_orders",
            time_window="2026-01-01..2026-06-30",
            baseline={"label": "Q1"},
            target={"label": "Q2"},
            grain="custom_baseline",
            filters={},
            dimensions=(),
            contract_versions={"metric": "paid_amount.v1"},
            role="analyst",
            budget_state=BudgetState(
                mode="research",
                used_capability_calls=0,
                soft_limit=50,
                hard_limit=100,
            ),
            llm_business_reason="Compare Q2 daily paid amount against Q1.",
            params={
                "rows": [
                    {"period": "h1_2026", "group": "baseline", "amount": 100},
                    {"period": "h1_2026", "group": "target", "amount": 120},
                ],
                "result_refs": ("sqlhash-2",),
                "pattern_family": "custom_baseline",
                "period_key": "period",
                "group_key": "group",
                "target_group": "target",
                "baseline_group": "baseline",
                "min_periods": 1,
            },
        )

        envelope = execute_capability(request)

        self.assertEqual(envelope.capability_id, "compare_periods")
        self.assertEqual(envelope.target_label, "Q2")
        self.assertEqual(envelope.baseline_label, "Q1")
        self.assertEqual(envelope.numeric_facts["comparable_periods"], 1)
        self.assertAlmostEqual(envelope.numeric_facts["median_uplift"], 0.2)

    def test_driver_decomposition_identifies_volume_or_unit_driver(self):
        result = driver_decomposition(
            [
                {"period": "h1", "group": "baseline", "amount": 100, "paid_users": 10},
                {"period": "h1", "group": "target", "amount": 150, "paid_users": 12},
            ]
        )

        self.assertEqual(result.wording_limit, "quantified")
        self.assertEqual(result.typed_payload["primary_driver"], "unit_value")
        self.assertGreater(
            result.typed_payload["decompositions"][0]["unit_value_share"],
            result.typed_payload["decompositions"][0]["volume_share"],
        )

    def test_driver_decomposition_skips_null_period_without_crashing(self):
        result = driver_decomposition(
            [
                {"period": None, "group": "baseline", "amount": 100, "paid_users": 10},
                {"period": "h1", "group": "baseline", "amount": 100, "paid_users": 10},
                {"period": "h1", "group": "target", "amount": 150, "paid_users": 12},
            ]
        )

        self.assertEqual(result.wording_limit, "quantified")
        self.assertEqual(result.typed_payload["decompositions"][0]["period"], "h1")

    def test_segment_contribution_ranks_dragging_segments(self):
        result = segment_contribution(
            [
                {"period": "A", "group": "baseline", "amount": 100},
                {"period": "A", "group": "target", "amount": 80},
                {"period": "B", "group": "baseline", "amount": 100},
                {"period": "B", "group": "target", "amount": 130},
            ]
        )

        self.assertEqual(result.wording_limit, "contextual")
        self.assertEqual(result.typed_payload["top_drags"][0]["segment"], "A")

    def test_outlier_contribution_reports_top_period_share(self):
        result = outlier_contribution(
            [
                {"period": "1", "group": "baseline", "amount": 100},
                {"period": "1", "group": "target", "amount": 200},
                {"period": "2", "group": "baseline", "amount": 100},
                {"period": "2", "group": "target", "amount": 105},
            ]
        )

        self.assertEqual(result.wording_limit, "contextual")
        self.assertGreater(result.typed_payload["top_positive_share"], 0.9)

    def test_outlier_contribution_reports_direction_after_removal(self):
        result = run_capability(
            "outlier_contribution",
            {
                "rows": [
                {"period": "1", "group": "baseline", "amount": 100},
                {"period": "1", "group": "target", "amount": 180},
                {"period": "2", "group": "baseline", "amount": 100},
                {"period": "2", "group": "target", "amount": 120},
                {"period": "3", "group": "baseline", "amount": 100},
                {"period": "3", "group": "target", "amount": 90},
                ],
                "period_grain": "day",
                "removal_policy": "top_positive_contribution_periods",
                "max_removed_periods": 1,
            },
        )

        self.assertIn("direction_preserved_after_top_positive", result.typed_payload)
        self.assertIn("remaining_delta_after_top_positive", result.typed_payload)

    def test_outlier_contribution_blocks_unsupported_removal_policy(self):
        result = run_capability(
            "outlier_contribution",
            {
                "rows": [
                    {"period": "1", "group": "baseline", "amount": 100},
                    {"period": "1", "group": "target", "amount": 180},
                ],
                "removal_policy": "custom_rule",
            },
        )

        self.assertEqual(result.wording_limit, "insufficient")
        self.assertIn("unsupported_removal_policy", result.limitations)
        self.assertEqual(result.typed_payload["removal_policy"], "custom_rule")

    def test_outlier_contribution_can_skip_direction_after_removal_claims(self):
        result = run_capability(
            "outlier_contribution",
            {
                "rows": [
                    {"period": "1", "group": "baseline", "amount": 100},
                    {"period": "1", "group": "target", "amount": 180},
                    {"period": "2", "group": "baseline", "amount": 100},
                    {"period": "2", "group": "target", "amount": 120},
                ],
                "removal_policy": "top_positive_contribution_periods",
                "direction_after_removal": False,
            },
        )

        self.assertNotIn(
            "direction_preserved_after_top_positive",
            result.typed_payload,
        )
        self.assertNotIn("direction_after_removal", result.typed_payload)
        self.assertEqual(
            result.typed_payload["direction_after_removal_evaluated"],
            False,
        )

    def test_user_mix_contribution_is_aggregate_only(self):
        result = run_capability(
            "user_mix_contribution",
            {
                "rows": [
                {
                    "period": "Q1",
                    "group": "baseline",
                    "channel": "Organic",
                    "user_mix_bucket": "new",
                    "amount": 100,
                    "paid_users": 10,
                },
                {
                    "period": "Q1",
                    "group": "target",
                    "channel": "Organic",
                    "user_mix_bucket": "new",
                    "amount": 120,
                    "paid_users": 12,
                },
                ],
                "segment_key": "channel",
                "user_grain_policy": "new_vs_returning",
            },
        )

        self.assertEqual(result.typed_payload["privacy_policy"], "aggregate_only")
        self.assertNotIn("raw_user_ids", result.typed_payload)

    def test_high_value_user_contribution_applies_threshold_indicators(self):
        result = run_capability(
            "high_value_user_contribution",
            {
                "rows": [
                    {
                        "period": "Q1",
                        "group": "baseline",
                        "bucket": "p95_plus",
                        "amount": 100,
                        "paid_users": 10,
                        "value_percentile": 0.97,
                    },
                    {
                        "period": "Q1",
                        "group": "baseline",
                        "bucket": "regular",
                        "amount": 60,
                        "paid_users": 30,
                        "value_percentile": 0.40,
                    },
                    {
                        "period": "Q1",
                        "group": "target",
                        "bucket": "p95_plus",
                        "amount": 180,
                        "paid_users": 12,
                        "value_percentile": 0.98,
                    },
                ],
                "threshold_policy": {"type": "top_percentile", "value": 0.95},
            },
        )

        self.assertEqual(result.typed_payload["privacy_policy"], "aggregate_only")
        self.assertNotIn("raw_user_ids", result.typed_payload)
        self.assertEqual(result.wording_limit, "contextual")
        self.assertEqual(result.typed_payload["high_value_amount"], 280.0)
        self.assertEqual(result.typed_payload["high_value_paid_users"], 22.0)
        self.assertGreater(result.typed_payload["high_value_amount_share"], 0.8)

    def test_high_value_user_contribution_degrades_without_indicator_fields(self):
        result = run_capability(
            "high_value_user_contribution",
            {
                "rows": [
                    {"period": "Q1", "group": "baseline", "amount": 100, "paid_users": 10},
                    {"period": "Q1", "group": "target", "amount": 180, "paid_users": 12},
                ],
                "threshold_policy": {"type": "top_percentile", "value": 0.95},
            },
        )

        self.assertEqual(result.wording_limit, "insufficient")
        self.assertIn("missing_high_value_indicator", result.limitations)
        self.assertIn("不能验证", result.typed_payload["business_readout"])

    def test_high_value_user_contribution_rejects_threshold_mismatch_bucket(self):
        result = run_capability(
            "high_value_user_contribution",
            {
                "rows": [
                    {
                        "period": "Q1",
                        "group": "baseline",
                        "bucket": "top_10_percent",
                        "amount": 100,
                        "paid_users": 10,
                    },
                    {
                        "period": "Q1",
                        "group": "target",
                        "bucket": "top_20",
                        "amount": 180,
                        "paid_users": 12,
                    },
                ],
                "threshold_policy": {"type": "top_percentile", "value": 0.95},
            },
        )

        self.assertEqual(result.wording_limit, "insufficient")
        self.assertIn("missing_high_value_indicator", result.limitations)
        self.assertEqual(result.typed_payload["high_value_amount"], 0.0)

    def test_high_value_user_contribution_degrades_partial_explicit_aggregate_fields(self):
        result = run_capability(
            "high_value_user_contribution",
            {
                "rows": [
                    {
                        "period": "Q1",
                        "group": "baseline",
                        "amount": 100,
                        "paid_users": 10,
                        "high_value_amount": 80,
                    },
                    {
                        "period": "Q1",
                        "group": "target",
                        "amount": 180,
                        "paid_users": 12,
                        "high_value_paid_users": 9,
                    },
                ],
                "threshold_policy": {"type": "top_percentile", "value": 0.95},
            },
        )

        self.assertEqual(result.wording_limit, "insufficient")
        self.assertIn("partial_high_value_aggregate_fields", result.limitations)
        self.assertNotIn("high_value_amount_share", result.typed_payload)
        self.assertIn("字段不完整", result.typed_payload["business_readout"])


if __name__ == "__main__":
    unittest.main()
