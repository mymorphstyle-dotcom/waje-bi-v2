from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class FrontendSdkDecisionTest(unittest.TestCase):
    def test_phase7_records_sdk_boundary_and_removes_unadopted_dependency(self):
        decision = (ROOT / "docs" / "phase-7-frontend-sdk-decision.md").read_text(encoding="utf-8")
        roadmap = (ROOT / "docs" / "implementation-roadmap.md").read_text(encoding="utf-8")
        package_json = (ROOT / "package.json").read_text(encoding="utf-8")
        package_lock = (ROOT / "package-lock.json").read_text(encoding="utf-8")

        self.assertIn("Decision", decision)
        self.assertIn("WAJE TraceRun", decision)
        self.assertIn("Postgres Runtime Store", decision)
        self.assertIn("not adopted for Phase 7 runtime", decision)
        self.assertIn("- [x] SDK decision for 21st Agent Elements or a better-fitting alternative.", roadmap)
        self.assertNotIn("@21st-sdk/react", package_json)
        self.assertNotIn("@21st-sdk/react", package_lock)


if __name__ == "__main__":
    unittest.main()
