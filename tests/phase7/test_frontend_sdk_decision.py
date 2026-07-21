from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class FrontendSdkDecisionTest(unittest.TestCase):
    def test_phase7_records_single_authority_sdk_boundary(self):
        decision = (ROOT / "docs" / "phase-7-frontend-sdk-decision.md").read_text(
            encoding="utf-8"
        )
        roadmap = (ROOT / "docs" / "implementation-roadmap.md").read_text(
            encoding="utf-8"
        )
        package_json = (ROOT / "package.json").read_text(encoding="utf-8")
        package_lock = (ROOT / "package-lock.json").read_text(encoding="utf-8")

        self.assertIn("Decision", decision)
        self.assertIn("Gateway APIs", decision)
        self.assertIn("PostgreSQL runtime store", decision)
        self.assertIn("does not own Phase 7 runtime state", decision)
        self.assertIn("fixed customer publication projection", decision)
        self.assertIn("## Replay read-model contract", decision)
        self.assertIn("complete, monotonic persisted chronology", decision)
        self.assertIn("durable call journal", decision)
        self.assertIn("keeps execution, verifier", decision)
        self.assertIn("does not coerce missing values to zero", decision)
        self.assertIn("validated customer publication exists", decision)
        self.assertIn("accepted graph is task-granular", decision)
        self.assertIn("discriminated execution state", decision)
        self.assertIn("`not_started` means the plan accepted the task", decision)
        self.assertIn("`unsettled` means execution activity exists", decision)
        self.assertIn("neither state is reported as an unknown outcome", decision)
        self.assertIn("authoritative outcome, retryability", decision)
        self.assertIn("`succeeded`, `unavailable`, `integrity_failed`", decision)
        self.assertIn("exact `(planRevisionId, taskId)` identity", decision)
        self.assertIn("never displays internal technical detail", decision)
        self.assertIn("durable execution transition", decision)
        self.assertIn("internal audit payloads remain server-side", decision)
        self.assertIn(
            "## Phase 7: Delete old authority and finish product acceptance", roadmap
        )
        self.assertNotIn("@21st-sdk/react", package_json)
        self.assertNotIn("@21st-sdk/react", package_lock)


if __name__ == "__main__":
    unittest.main()
