from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class CustomerRunStateStreamTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.route = (
            ROOT / "app" / "api" / "runs" / "[runId]" / "events" / "route.ts"
        ).read_text(encoding="utf-8")
        cls.store = (ROOT / "app" / "api" / "_conversationStore.ts").read_text(
            encoding="utf-8"
        )

    def test_public_stream_emits_only_versioned_customer_snapshots(self):
        self.assertIn("loadCustomerAnalysisSnapshot", self.route)
        self.assertIn('"event: customer_state_changed"', self.route)
        self.assertIn("last-event-id", self.route)
        self.assertIn("current.stateVersion", self.route)
        self.assertIn("ReadableStream", self.route)
        self.assertIn("while (!closed)", self.route)
        self.assertIn(": heartbeat", self.route)
        self.assertIn('"X-Accel-Buffering": "no"', self.route)
        self.assertNotIn("runEvents", self.route)
        self.assertNotIn("audit_events", self.route)
        self.assertNotIn("node_process", self.route)

    def test_snapshot_gates_current_clarification_by_authoritative_run_status(self):
        contract = (
            ROOT / "app" / "api" / "_customerAnalysisContract.ts"
        ).read_text(encoding="utf-8")
        self.assertIn('run.status === "waiting_for_clarification"', contract)
        self.assertIn("currentClarification", contract)
        self.assertNotIn('event === "clarification_state_saved"', contract)

    def test_complete_technical_chronology_remains_in_audit_layer(self):
        self.assertIn("export async function runEvents", self.store)
        self.assertIn("waje_runtime.audit_events", self.store)
        self.assertIn("waje_runtime.run_nodes", self.store)
        self.assertIn("loadPersistedPublication", self.store)
        self.assertIn("customer_request_failed", self.store)
        self.assertIn("technicalDetailRef", self.store)

    def test_customer_errors_do_not_return_internal_snake_case_codes(self):
        self.assertIn("customerErrorProjection", self.store)
        self.assertIn('code: "action_no_longer_available"', self.store)
        self.assertIn('code: "analysis_unavailable"', self.store)
        self.assertIn("payload: { internalCode }", self.store)
        self.assertNotIn("error: internalCode", self.store)


if __name__ == "__main__":
    unittest.main()
