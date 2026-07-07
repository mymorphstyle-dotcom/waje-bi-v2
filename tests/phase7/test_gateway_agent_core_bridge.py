from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class GatewayAgentCoreBridgeTest(unittest.TestCase):
    def test_message_route_invokes_python_agent_core_command(self):
        helper = (ROOT / "app" / "api" / "_agentCore.ts").read_text(encoding="utf-8")
        source = (
            ROOT
            / "app"
            / "api"
            / "threads"
            / "[threadId]"
            / "messages"
            / "route.ts"
        ).read_text(encoding="utf-8")

        self.assertIn("runAgentCore", source)
        self.assertIn("_agentCore", source)
        self.assertIn("bi_agent.conversation.agent_core", helper)
        self.assertIn("WAJE_AGENT_CORE_COMMAND", helper)
        self.assertIn("agentCore", source)

    def test_artifact_continue_route_invokes_shared_agent_core_command(self):
        helper = ROOT / "app" / "api" / "_agentCore.ts"
        continue_route = (
            ROOT
            / "app"
            / "api"
            / "artifacts"
            / "[artifactId]"
            / "continue"
            / "route.ts"
        ).read_text(encoding="utf-8")
        message_route = (
            ROOT
            / "app"
            / "api"
            / "threads"
            / "[threadId]"
            / "messages"
            / "route.ts"
        ).read_text(encoding="utf-8")

        self.assertTrue(helper.exists())
        helper_source = helper.read_text(encoding="utf-8")
        self.assertIn("bi_agent.conversation.agent_core", helper_source)
        self.assertIn("WAJE_AGENT_CORE_INLINE", helper_source)
        self.assertIn("runAgentCore", continue_route)
        self.assertIn("_agentCore", continue_route)
        self.assertIn("_agentCore", message_route)

    def test_inline_agent_core_preserves_waiting_for_clarification_status(self):
        helper = (ROOT / "app" / "api" / "_agentCore.ts").read_text(encoding="utf-8")
        store = (ROOT / "app" / "api" / "_conversationStore.ts").read_text(encoding="utf-8")

        self.assertIn("waiting_for_clarification", helper)
        self.assertIn("JSON.parse", helper)
        self.assertIn("parsed.status", helper)
        self.assertIn("waiting_for_clarification", store)

    def test_message_route_surfaces_inline_waiting_for_clarification_status(self):
        source = (
            ROOT
            / "app"
            / "api"
            / "threads"
            / "[threadId]"
            / "messages"
            / "route.ts"
        ).read_text(encoding="utf-8")

        self.assertIn("effectiveRun", source)
        self.assertIn("agentCore?.status === \"waiting_for_clarification\"", source)

    def test_artifact_continue_route_surfaces_inline_waiting_for_clarification_status(self):
        source = (
            ROOT
            / "app"
            / "api"
            / "artifacts"
            / "[artifactId]"
            / "continue"
            / "route.ts"
        ).read_text(encoding="utf-8")

        self.assertIn("effectiveRun", source)
        self.assertIn("agentCore?.status === \"waiting_for_clarification\"", source)


if __name__ == "__main__":
    unittest.main()
