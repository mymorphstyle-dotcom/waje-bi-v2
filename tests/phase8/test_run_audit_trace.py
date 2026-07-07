from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class RunAuditTraceTest(unittest.TestCase):
    def test_run_audit_trace_route_links_answer_to_runtime_refs(self):
        store = (ROOT / "app" / "api" / "_conversationStore.ts").read_text(encoding="utf-8")
        route = (
            ROOT / "app" / "api" / "runs" / "[runId]" / "audit-trace" / "route.ts"
        ).read_text(encoding="utf-8")

        self.assertIn("runAuditTrace", store)
        for table in (
            "waje_runtime.analysis_runs",
            "waje_runtime.answer_packages",
            "waje_runtime.run_nodes",
            "waje_runtime.evidence_refs",
            "waje_runtime.result_refs",
            "waje_runtime.audit_events",
        ):
            with self.subTest(table=table):
                self.assertIn(table, store)

        self.assertIn("contract_version", store)
        self.assertIn("snapshot_id", store)
        self.assertIn("query_ref", store)
        self.assertIn("evidenceFromPackage", store)
        self.assertIn("verifier", store)
        self.assertIn("runAuditTrace", route)
        self.assertNotIn("waje_runtime.", route)


if __name__ == "__main__":
    unittest.main()
