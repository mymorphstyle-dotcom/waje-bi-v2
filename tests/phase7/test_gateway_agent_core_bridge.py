from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class GatewayAgentCoreBridgeTest(unittest.TestCase):
    def test_message_route_invokes_python_agent_core_command(self):
        helper = (ROOT / "app" / "api" / "_agentCore.ts").read_text(encoding="utf-8")
        store = (ROOT / "app" / "api" / "_conversationStore.ts").read_text(
            encoding="utf-8"
        )
        source = (
            ROOT / "app" / "api" / "threads" / "[threadId]" / "messages" / "route.ts"
        ).read_text(encoding="utf-8")
        runtime = (ROOT / "app" / "api" / "_pythonRuntime.ts").read_text(
            encoding="utf-8"
        )

        self.assertIn("runAgentCore", source)
        self.assertIn("_agentCore", source)
        self.assertIn("bi_agent.conversation.agent_core", helper)
        self.assertIn("wajePythonInvocation", helper)
        self.assertIn('"uv"', runtime)
        self.assertIn('"3.12"', runtime)
        self.assertIn('"requirements.txt"', runtime)
        self.assertNotIn('spawn("python3"', helper)
        self.assertIn('"--topic-selection"', helper)
        self.assertIn('"--topic-choice-answer"', helper)
        self.assertIn("agentCore", source)
        self.assertIn("loadCustomerAnalysisSnapshot", source)
        self.assertIn("recordCustomerRunStateFromAgentResult", source)
        self.assertIn("topicSelectionFrom", source)
        self.assertIn("topicChoiceAnswerFrom", source)
        self.assertNotIn("agentCore:", source)
        self.assertIn("export async function loadCustomerAnalysisSnapshot", store)

    def test_inline_agent_core_preserves_waiting_for_clarification_status(self):
        helper = (ROOT / "app" / "api" / "_agentCore.ts").read_text(encoding="utf-8")
        store = (ROOT / "app" / "api" / "_conversationStore.ts").read_text(
            encoding="utf-8"
        )

        self.assertIn("waiting_for_clarification", helper)
        self.assertIn("JSON.parse", helper)
        self.assertIn("parsed.status", helper)
        self.assertIn("waiting_for_clarification", store)

    def test_message_route_surfaces_inline_waiting_for_clarification_status(self):
        source = (
            ROOT / "app" / "api" / "threads" / "[threadId]" / "messages" / "route.ts"
        ).read_text(encoding="utf-8")

        self.assertIn("recordCustomerRunStateFromAgentResult", source)
        self.assertIn("loadCustomerAnalysisSnapshot", source)
        self.assertIn('terminalStatus === "waiting_for_clarification"', source)

    def test_gateway_entry_routes_terminalize_spawn_failure_with_shared_cas(self):
        store = (ROOT / "app" / "api" / "_conversationStore.ts").read_text(
            encoding="utf-8"
        )
        routes = [
            (
                ROOT
                / "app"
                / "api"
                / "threads"
                / "[threadId]"
                / "messages"
                / "route.ts"
            ).read_text(encoding="utf-8")
        ]

        self.assertIn("export async function failOwnedRunDispatch", store)
        self.assertIn("status = 'failed'", store)
        self.assertIn(
            "status IN ('queued', 'running', 'running_workflow')",
            store,
        )
        for route in routes:
            self.assertIn("failOwnedRunDispatch", route)
            self.assertIn("agentCore?.error || runIdMismatch", route)
            self.assertIn("agent_core_run_id_mismatch", route)

    def test_bridge_accepts_typed_interaction_and_phase45_terminals(self):
        helper = (ROOT / "app/api/_agentCore.ts").read_text(encoding="utf-8")
        route = (ROOT / "app/api/threads/[threadId]/messages/route.ts").read_text(
            encoding="utf-8"
        )

        for status in (
            "interaction_completed",
            "authority_sealed",
            "narrative_ready",
        ):
            self.assertIn(status, helper)
            self.assertIn(status, route)
        self.assertNotIn("completed_without_workflow", helper)


if __name__ == "__main__":
    unittest.main()
