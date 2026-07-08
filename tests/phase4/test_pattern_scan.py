import unittest

from bi_agent.capabilities.data_quality_check import data_quality_check
from bi_agent.capabilities.formula_decompose import formula_decompose
from bi_agent.capabilities.joint_attribution import joint_attribution
from bi_agent.capabilities.pattern_scan import scan_pattern
from bi_agent.capabilities.segment_bridge import segment_bridge


def month_rows(start_amount, mid_amount, end_amount):
    rows = []
    year = 2024
    month = 1
    for _ in range(24):
        month_key = f"{year}-{month:02d}"
        rows.extend(
            [
                {"month": month_key, "phase": "start", "amount": start_amount, "days": 10},
                {"month": month_key, "phase": "mid", "amount": mid_amount, "days": 10},
                {"month": month_key, "phase": "end", "amount": end_amount, "days": 11},
            ]
        )
        month += 1
        if month == 13:
            year += 1
            month = 1
    return rows


class PatternScanTest(unittest.TestCase):
    def test_month_start_pattern_requires_direction_and_uplift(self):
        result = scan_pattern(
            month_rows(110, 90, 95),
            pattern_family="intra_period",
            target_phase="start",
            materiality_floor=0.03,
        )

        self.assertIs(result.established, True)
        self.assertGreaterEqual(result.direction_ratio, 0.70)
        self.assertGreaterEqual(result.median_uplift, 0.03)
        self.assertEqual(result.evidence_type, "statistical_association")
        self.assertIn(result.strength, {"medium", "high"})

    def test_weak_direction_degrades_pattern_claim(self):
        result = scan_pattern(
            month_rows(100, 120, 95),
            pattern_family="intra_period",
            target_phase="start",
            materiality_floor=0.03,
        )

        self.assertIs(result.established, False)
        self.assertIn(result.wording_limit, {"tendency", "insufficient"})

    def test_custom_baseline_compares_named_groups(self):
        rows = [
            {"period": "p1", "group": "target", "amount": 120},
            {"period": "p1", "group": "baseline", "amount": 100},
            {"period": "p2", "group": "target", "amount": 122},
            {"period": "p2", "group": "baseline", "amount": 100},
        ]

        result = scan_pattern(
            rows,
            pattern_family="custom_baseline",
            target_group="target",
            baseline_group="baseline",
            materiality_floor=0.03,
            min_periods=2,
        )

        self.assertIs(result.established, True)
        self.assertEqual(result.comparable_periods, 2)
        self.assertEqual(result.evidence_type, "statistical_association")

    def test_weekly_pattern_can_use_selected_baseline_weekdays(self):
        rows = []
        for week in ("w1", "w2", "w3"):
            rows.extend(
                [
                    {"week": week, "weekday": 1, "amount": 100},
                    {"week": week, "weekday": 5, "amount": 110},
                    {"week": week, "weekday": 7, "amount": 100},
                ]
            )

        result = scan_pattern(
            rows,
            pattern_family="weekly",
            week_key="week",
            weekday_key="weekday",
            target_weekdays=(5,),
            baseline_weekdays=(1, 7),
            materiality_floor=0.03,
            min_periods=3,
        )

        self.assertIs(result.established, True)
        self.assertEqual(result.comparable_periods, 3)

    def test_empty_pattern_scan_returns_insufficient_evidence(self):
        result = scan_pattern(
            [],
            pattern_family="intra_period",
            target_phase="start",
            materiality_floor=0.03,
        )

        self.assertIs(result.established, False)
        self.assertEqual(result.evidence_type, "insufficient")
        self.assertEqual(result.wording_limit, "insufficient")

    def test_formula_partial_gaps_degrade_quantified_wording(self):
        result = formula_decompose(
            [
                {"formula_id": "covered", "components": ("paid_orders",)},
                {"formula_id": "gap", "components": ("paid_orders", "missing_component")},
            ],
            available_components=("paid_orders",),
        )

        self.assertEqual(result.evidence_type, "accounting_contribution")
        self.assertEqual(result.strength, "low")
        self.assertEqual(result.wording_limit, "degraded")
        self.assertIn("missing_formula_component:gap", result.limitations)

    def test_data_quality_empty_rows_are_not_supported(self):
        result = data_quality_check([], required_fields=("amount",))

        self.assertEqual(result.evidence_type, "insufficient")
        self.assertEqual(result.strength, "insufficient")
        self.assertEqual(result.wording_limit, "blocked")

    def test_segment_bridge_blocks_sparse_or_sensitive_rows(self):
        result = segment_bridge(
            [
                {"segment": "A", "amount": 100, "n": 3},
                {"segment": "B", "amount": 200, "raw_user_id": "u1", "n": 20},
                {"segment": "C", "amount": 300, "email": "a@example.test", "n": 20},
                {"segment": "D", "amount": 400, "account_id": "acct", "n": 20},
                {"segment": "E", "amount": 500, "phone": "123", "n": 20},
            ],
        )

        self.assertEqual(result.evidence_type, "permission_limited")
        self.assertEqual(result.wording_limit, "blocked")
        self.assertIn("sparse_cell", result.limitations)
        self.assertIn("raw_identifier_present", result.limitations)

    def test_segment_bridge_requires_valid_sample_size(self):
        for segments in (
            [{"segment": "A", "amount": 100}],
            [{"segment": "A", "amount": 100, "n": "unknown"}],
        ):
            with self.subTest(segments=segments):
                result = segment_bridge(segments)

                self.assertEqual(result.evidence_type, "permission_limited")
                self.assertEqual(result.wording_limit, "blocked")
                self.assertIn("sample_size_unverified", result.limitations)

    def test_segment_bridge_outputs_only_safe_aggregate_payload(self):
        result = segment_bridge(
            [{"segment": "A", "amount": 100, "n": 20, "payment_method": "OPAY"}]
        )

        self.assertEqual(result.wording_limit, "contextual")
        self.assertEqual(
            result.typed_payload["segments"],
            ({"segment": "A", "amount": 100, "n": 20},),
        )

    def test_joint_attribution_computes_combinations_when_one_dimension_is_insufficient(self):
        segment = segment_bridge(
            [{"segment": "channel", "amount": 100, "n": 100}],
            residual=0.25,
            fit=0.60,
        )
        result = joint_attribution(
            [
                {"channel": "WajeSpecial", "phase": "start", "group": "baseline", "amount": 100, "n": 40},
                {"channel": "WajeSpecial", "phase": "start", "group": "target", "amount": 180, "n": 45},
                {"channel": "WajeSpecial", "phase": "mid", "group": "baseline", "amount": 100, "n": 40},
                {"channel": "WajeSpecial", "phase": "mid", "group": "target", "amount": 30, "n": 45},
                {"channel": "Other", "phase": "start", "group": "baseline", "amount": 100, "n": 80},
                {"channel": "Other", "phase": "start", "group": "target", "amount": 30, "n": 85},
                {"channel": "Other", "phase": "mid", "group": "baseline", "amount": 100, "n": 80},
                {"channel": "Other", "phase": "mid", "group": "target", "amount": 180, "n": 85},
            ],
            segment_evidence=segment,
            dimension_keys=("channel", "phase"),
        )

        self.assertEqual(result.evidence_type, "statistical_association")
        self.assertEqual(result.wording_limit, "candidate")
        self.assertEqual(result.typed_payload["dimension_decision"]["action"], "keep_joint")
        self.assertEqual(result.typed_payload["dimension_keys"], ("channel", "phase"))
        self.assertEqual(result.typed_payload["top_combinations"][0]["dimension_values"], ("WajeSpecial", "start"))
        self.assertAlmostEqual(result.typed_payload["total_delta"], 20.0)

    def test_joint_attribution_blocks_sparse_or_sensitive_combination_cells(self):
        segment = segment_bridge(
            [{"segment": "channel", "amount": 100, "n": 100}],
            residual=0.25,
            fit=0.60,
        )
        result = joint_attribution(
            [
                {
                    "channel": "WajeSpecial",
                    "phase": "start",
                    "group": "baseline",
                    "amount": 100,
                    "raw_user_id": "u1",
                    "n": 3,
                }
            ],
            segment_evidence=segment,
            dimension_keys=("channel", "phase"),
        )

        self.assertEqual(result.evidence_type, "permission_limited")
        self.assertEqual(result.wording_limit, "blocked")
        self.assertIn("sparse_cell", result.limitations)
        self.assertIn("raw_identifier_present", result.limitations)

    def test_joint_attribution_recommends_downgrade_when_single_dimension_explains_change(self):
        segment = segment_bridge(
            [{"segment": "channel", "amount": 100, "n": 100}],
            residual=0.25,
            fit=0.60,
        )
        result = joint_attribution(
            [
                {"channel": "WajeSpecial", "phase": "start", "group": "baseline", "amount": 100, "n": 40},
                {"channel": "WajeSpecial", "phase": "start", "group": "target", "amount": 180, "n": 45},
                {"channel": "WajeSpecial", "phase": "mid", "group": "baseline", "amount": 100, "n": 40},
                {"channel": "WajeSpecial", "phase": "mid", "group": "target", "amount": 120, "n": 45},
                {"channel": "Other", "phase": "start", "group": "baseline", "amount": 100, "n": 80},
                {"channel": "Other", "phase": "start", "group": "target", "amount": 105, "n": 85},
                {"channel": "Other", "phase": "mid", "group": "baseline", "amount": 100, "n": 80},
                {"channel": "Other", "phase": "mid", "group": "target", "amount": 105, "n": 85},
            ],
            segment_evidence=segment,
            dimension_keys=("channel", "phase"),
        )

        self.assertEqual(
            result.typed_payload["dimension_decision"]["action"],
            "downgrade_to_single_dimension",
        )
        self.assertEqual(result.typed_payload["dimension_decision"]["dimension"], "channel")


if __name__ == "__main__":
    unittest.main()
