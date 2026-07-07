from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class PermissionFilteredArtifactsTest(unittest.TestCase):
    def test_artifact_filter_prunes_claim_payloads_with_same_visibility_rules(self):
        store = (ROOT / "app" / "api" / "_conversationStore.ts").read_text(encoding="utf-8")

        self.assertIn("filterAnswerPackageForRole", store)
        self.assertIn("filterSummaryPayloadForRole", store)
        self.assertIn("filterVisibleItems", store)
        self.assertIn("filterVisibleVisualizationPlan", store)
        for key in ("claims", "claim_groups", "visualization_plan"):
            with self.subTest(key=key):
                self.assertIn(key, store)

        returned_shape = store.split("return {", 1)[1]
        self.assertNotIn("admin_audit: answerPackage.admin_audit", returned_shape)


if __name__ == "__main__":
    unittest.main()
