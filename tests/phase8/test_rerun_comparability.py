from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class RerunComparabilityTest(unittest.TestCase):
    def test_rerun_comparability_route_compares_snapshot_contract_and_query_refs(self):
        store = (ROOT / "app" / "api" / "_conversationStore.ts").read_text(encoding="utf-8")
        route = (
            ROOT / "app" / "api" / "runs" / "[runId]" / "rerun-comparability" / "route.ts"
        ).read_text(encoding="utf-8")

        self.assertIn("runRerunComparability", store)
        self.assertIn("runAuditTrace", store)
        self.assertIn("snapshotIds", store)
        self.assertIn("contractVersions", store)
        self.assertIn("queryRefs", store)
        for reason in ("snapshot_mismatch", "contract_version_mismatch", "query_ref_mismatch"):
            with self.subTest(reason=reason):
                self.assertIn(reason, store)

        self.assertIn("candidateRunId", route)
        self.assertIn("runRerunComparability", route)
        self.assertNotIn("waje_runtime.", route)


if __name__ == "__main__":
    unittest.main()
