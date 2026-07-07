import unittest

from tools.phase5.generate_phase5_report import build_report_model


class Phase5ReportGeneratorTest(unittest.TestCase):
    def test_report_model_summarizes_existing_phase5_inputs(self):
        model = build_report_model(
            [
                {
                    "case_id": "full_wajespecial_vs_other_by_month",
                    "eval_status": "degraded",
                    "business_conclusion_published": False,
                    "primary_evidence": {
                        "capability": "rolling_window_compare",
                        "strength": "low",
                        "wording_limit": "insufficient",
                        "limitations": ["insufficient_comparable_periods"],
                    },
                },
                {
                    "case_id": "full_2026_q2_vs_q1",
                    "eval_status": "passed",
                    "business_conclusion_published": True,
                    "primary_evidence": {
                        "capability": "compare_periods",
                        "strength": "high",
                        "wording_limit": "supported",
                        "limitations": [],
                    },
                },
            ],
            {
                "cases": [
                    {
                        "case_id": "channel_total_vs_daily_average",
                        "expected_boundary_status": "needs_question",
                        "latent_choice": "total_amount_vs_daily_average",
                    }
                ]
            },
        )

        self.assertEqual(model["status_counts"], {"degraded": 1, "passed": 1})
        self.assertEqual(model["published_counts"], {"blocked_or_degraded": 1, "published": 1})
        self.assertEqual(model["weak_evidence_case_ids"], ["full_wajespecial_vs_other_by_month"])
        self.assertEqual(model["route_drift_case_ids"], ["full_wajespecial_vs_other_by_month"])
        self.assertEqual(model["clarification_case_count"], 1)
        self.assertEqual(model["clarification_expected_counts"], {"needs_question": 1})


if __name__ == "__main__":
    unittest.main()
