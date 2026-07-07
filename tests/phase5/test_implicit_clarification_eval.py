from pathlib import Path
import unittest

import yaml


class ImplicitClarificationEvalTest(unittest.TestCase):
    def test_implicit_clarification_cases_are_not_obvious_missing_fields(self):
        data = yaml.safe_load(
            Path("evals/phase5/implicit_clarification_cases.yaml").read_text(
                encoding="utf-8"
            )
        )
        case_ids = {case["case_id"] for case in data["cases"]}

        self.assertEqual(
            case_ids,
            {
                "channel_total_vs_daily_average",
                "combined_vs_per_segment_baseline",
                "strict_every_period_vs_majority_tendency",
                "calendar_vs_business_event_window",
            },
        )
        self.assertTrue(
            all(case["expected_boundary_status"] == "needs_question" for case in data["cases"])
        )
        self.assertTrue(all("latent_choice" in case for case in data["cases"]))


if __name__ == "__main__":
    unittest.main()
