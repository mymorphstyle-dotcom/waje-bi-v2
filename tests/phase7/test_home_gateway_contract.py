from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class HomeGatewayContractTest(unittest.TestCase):
    def test_home_uses_gateway_thread_message_and_clarification_routes(self):
        page = (ROOT / "app" / "page.tsx").read_text(encoding="utf-8")

        self.assertIn('fetch("/api/threads"', page)
        self.assertIn('method: "POST"', page)
        self.assertIn("/api/threads/${encodeURIComponent(activeThreadId)}/messages", page)
        self.assertIn("/api/runs/${encodeURIComponent(sourceRunId)}/clarifications", page)
        self.assertIn("new EventSource(eventsUrl)", page)
        self.assertIn('event.event === "answer_package_ready"', page)
        self.assertIn("final_business_summary", page)

    def test_home_contains_no_demo_result_or_fake_workflow_fallback(self):
        page = (ROOT / "app" / "page.tsx").read_text(encoding="utf-8")

        for forbidden in (
            "/api/langgraph",
            "LangGraph mock",
            "monthEvents",
            "monthAnswer",
            "toolGroups",
            "patternData",
            "contributionData",
            "serverWait",
            "playFlow",
            "+18.9%",
            "25/29",
            "54%",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, page)

        self.assertFalse((ROOT / "app" / "api" / "langgraph" / "route.ts").exists())

    def test_home_preserves_backend_recommended_clarification(self):
        page = (ROOT / "app" / "page.tsx").read_text(encoding="utf-8")

        self.assertIn("raw.recommended_assumption", page)
        self.assertIn("label === recommendedAssumption", page)


if __name__ == "__main__":
    unittest.main()
