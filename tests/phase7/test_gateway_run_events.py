from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class GatewayRunEventsTest(unittest.TestCase):
    def test_run_events_stream_reads_persisted_runtime_state(self):
        store = (ROOT / "app" / "api" / "_conversationStore.ts").read_text(encoding="utf-8")
        route = (
            ROOT / "app" / "api" / "runs" / "[runId]" / "events" / "route.ts"
        ).read_text(encoding="utf-8")

        self.assertIn("runEvents", store)
        self.assertIn("waje_runtime.audit_events", store)
        self.assertIn("waje_runtime.answer_packages", store)
        self.assertIn("answer_package_ready", store)
        self.assertIn("runEvents", route)
        self.assertNotIn("context_manifest_pending", route)

    def test_run_events_include_business_process_summary(self):
        store = (ROOT / "app" / "api" / "_conversationStore.ts").read_text(encoding="utf-8")

        self.assertIn("processEvent", store)
        self.assertIn("process:", store)
        self.assertIn("需要用户确认", store)
        self.assertIn("payload", store)

    def test_run_events_include_run_node_process_events(self):
        store = (ROOT / "app" / "api" / "_conversationStore.ts").read_text(encoding="utf-8")

        self.assertIn("waje_runtime.run_nodes", store)
        self.assertIn("node_process", store)
        self.assertIn("processNodeEvent", store)
        self.assertIn("accepted_plan", store)
        self.assertIn("capability_progress", store)
        self.assertIn("verifier_result", store)
        self.assertIn("repair_or_degrade", store)


if __name__ == "__main__":
    unittest.main()
