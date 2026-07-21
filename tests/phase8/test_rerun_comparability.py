from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class RerunComparabilityTest(unittest.TestCase):
    def test_rerun_comparability_route_compares_current_authority_refs(self):
        store = (ROOT / "app" / "api" / "_conversationStore.ts").read_text(
            encoding="utf-8"
        )
        route = (
            ROOT
            / "app"
            / "api"
            / "runs"
            / "[runId]"
            / "rerun-comparability"
            / "route.ts"
        ).read_text(encoding="utf-8")

        self.assertIn("runRerunComparability", store)
        self.assertIn("runAuditTrace", store)
        self.assertIn("snapshotRefs", store)
        self.assertIn("contractRefs", store)
        self.assertIn("queryRefs", store)
        self.assertIn("resultRefs", store)
        self.assertIn("waje_runtime.capability_evidence_ledger_entries", store)
        self.assertIn("waje_runtime.query_execution_authority", store)
        self.assertIn("waje_runtime.capability_execution_snapshots", store)
        self.assertNotIn("waje_runtime.evidence_refs", store)
        self.assertNotIn("waje_runtime.result_refs", store)
        for reason in (
            "snapshot_mismatch",
            "contract_ref_mismatch",
            "query_ref_mismatch",
            "result_ref_mismatch",
        ):
            with self.subTest(reason=reason):
                self.assertIn(reason, store)

        self.assertIn("candidateRunId", route)
        self.assertIn("runRerunComparability", route)
        self.assertNotIn("waje_runtime.", route)


if __name__ == "__main__":
    unittest.main()
