import unittest

from bi_agent.capabilities.pattern_scan import scan_pattern


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


if __name__ == "__main__":
    unittest.main()
