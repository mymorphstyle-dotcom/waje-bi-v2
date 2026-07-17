from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class CustomerSafeArtifactsTest(unittest.TestCase):
    def test_customer_projection_prunes_internal_claim_payloads_consistently(self):
        store = (ROOT / "app" / "api" / "_conversationStore.ts").read_text(encoding="utf-8")

        self.assertIn("projectAnswerPackageForCustomer", store)
        self.assertIn("projectSummaryPayloadForCustomer", store)
        self.assertIn("filterVisibleItems", store)
        self.assertIn("filterVisibleVisualizationPlan", store)
        for key in ("claims", "claim_groups", "visualization_plan"):
            with self.subTest(key=key):
                self.assertIn(key, store)

        returned_shape = store.split("return {", 1)[1]
        self.assertNotIn("admin_audit: answerPackage.admin_audit", returned_shape)

    def test_customer_projection_has_one_visibility_set_for_all_users(self):
        store = (ROOT / "app" / "api" / "_conversationStore.ts").read_text(encoding="utf-8")

        self.assertIn(
            'new Set(["business_summary", "aggregate_evidence", "diagnostic_detail"])',
            store,
        )
        self.assertIn(
            "projectAnswerPackageForCustomer(answerPackage: Record<string, unknown>)",
            store,
        )


if __name__ == "__main__":
    unittest.main()
