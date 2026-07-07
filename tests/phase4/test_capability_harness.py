import unittest

from bi_agent.capabilities.driver_decomposition import driver_decomposition
from bi_agent.capabilities.outlier_contribution import outlier_contribution
from bi_agent.capabilities.segment_contribution import segment_contribution
from bi_agent.runtime.capability_harness import execute_capability
from bi_agent.runtime.capability_models import BudgetState, CapabilityRequest


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


if __name__ == "__main__":
    unittest.main()
