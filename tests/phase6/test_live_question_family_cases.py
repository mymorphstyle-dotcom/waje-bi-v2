from pathlib import Path
import unittest

import yaml

from tools.phase5.run_live_node_system_test import _load_live_cases


ROOT = Path(__file__).resolve().parents[2]
CASE_FILE = ROOT / "evals" / "phase6" / "live_question_family_cases.yaml"


class Phase6LiveQuestionFamilyCasesTest(unittest.TestCase):
    def test_manifest_covers_launch_families_and_composite_intent(self):
        data = yaml.safe_load(CASE_FILE.read_text(encoding="utf-8"))
        cases = data["cases"]
        families = {case["primary_question_family"] for case in cases}

        self.assertGreaterEqual(len(cases), 12)
        self.assertTrue(
            {
                "pattern_explanation",
                "paid_amount_change_explanation",
                "business_object_impact_review",
                "segment_or_factor_attribution",
                "revenue_health_review",
                "anomaly_or_black_swan_review",
                "custom_baseline_comparison",
                "data_quality_or_evidence_review",
            }.issubset(families)
        )
        self.assertTrue(
            any(len(case.get("secondary_question_families", ())) > 0 for case in cases)
        )

    def test_cases_are_live_only_and_reviewable(self):
        data = yaml.safe_load(CASE_FILE.read_text(encoding="utf-8"))

        self.assertEqual(data["artifact_policy"], "real_clickhouse_real_llm_node_debug_only")
        self.assertTrue(data.get("source_case_files"))
        for case in data["cases"]:
            with self.subTest(case=case["case_id"]):
                self.assertNotIn("fixture_rows", case)
                self.assertTrue(case.get("real_sql") or case.get("source_case_id"))
                self.assertTrue(case["question"])
                self.assertTrue(case["review_focus"])
                self.assertTrue(case["required_capabilities"])
                self.assertTrue(case["required_accepted_capabilities"])
                self.assertTrue(case["allowed_final_statuses"])

    def test_manifest_source_cases_resolve_to_runnable_cases(self):
        cases = _load_live_cases(CASE_FILE)

        self.assertGreaterEqual(len(cases), 12)
        for case in cases:
            with self.subTest(case=case["case_id"]):
                self.assertTrue(case.get("real_sql"))
                self.assertTrue(case.get("pattern_family"))
                self.assertTrue(case.get("pattern_params"))


if __name__ == "__main__":
    unittest.main()
