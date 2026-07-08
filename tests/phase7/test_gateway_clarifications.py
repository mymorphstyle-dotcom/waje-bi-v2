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

    def test_clarification_route_forwards_full_payload_and_waits_for_resumed_result(self):
        route = (
            ROOT / "app" / "api" / "runs" / "[runId]" / "clarifications" / "route.ts"
        ).read_text(encoding="utf-8")
        helper = (ROOT / "app" / "api" / "_agentCore.ts").read_text(encoding="utf-8")

        self.assertRegex(
            route,
            r"const clarificationPayload = \{\s*runId,\s*answer,\s*selectedOptionId: body\.selectedOptionId \?\? null,\s*source: \"user\"(?: as const)?,\s*\}",
        )
        self.assertRegex(
            route,
            r"runAgentCore\([^;]*clarification:\s*clarificationPayload[^;]*forceInline:\s*true[^;]*\)",
        )
        self.assertIn("resumedRunId: resumed.run_id ?? runId", route)
        self.assertIn("topicId: resumed.topic_id ?? null", route)
        self.assertIn("status: resumed.status ?? agentCore.status", route)
        self.assertIn("answerPackagePreview: resumed.answer_package ?? null", route)

        self.assertRegex(helper, r"clarification\?:\s*\{")
        self.assertIn("selectedOptionId?: string | null", helper)
        self.assertIn('source?: "user"', helper)
        self.assertIn('"--clarification"', helper)
        self.assertIn("JSON.stringify(options.clarification)", helper)
        self.assertRegex(helper, r"options\.forceInline\s*\|\|\s*process\.env\.WAJE_AGENT_CORE_INLINE === \"1\"")


if __name__ == "__main__":
    unittest.main()
