from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class GatewayClarificationsTest(unittest.TestCase):
    def test_clarification_route_records_answer_and_resumes_same_run(self):
        route = (
            ROOT / "app" / "api" / "runs" / "[runId]" / "clarifications" / "route.ts"
        ).read_text(encoding="utf-8")
        store = (ROOT / "app" / "api" / "_conversationStore.ts").read_text(encoding="utf-8")

        self.assertIn("runAgentCore", route)
        self.assertIn("recordClarificationOutcome", route)
        self.assertIn("addUserMessage", route)
        self.assertIn("agentCore", route)
        self.assertNotIn("createRun", route)
        self.assertIn("clarification_answer_recorded", store)


if __name__ == "__main__":
    unittest.main()
