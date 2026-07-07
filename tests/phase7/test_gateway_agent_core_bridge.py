from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class GatewayAgentCoreBridgeTest(unittest.TestCase):
    def test_message_route_invokes_python_agent_core_command(self):
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
        self.assertIn("bi_agent.conversation.agent_core", source)
        self.assertIn("WAJE_AGENT_CORE_COMMAND", source)
        self.assertIn("agentCore", source)


if __name__ == "__main__":
    unittest.main()
