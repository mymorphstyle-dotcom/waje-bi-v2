from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class GatewayAgentCoreBridgeTest(unittest.TestCase):
    def test_message_route_invokes_general_agent_runtime(self):
        helper = (ROOT / "app/api/_generalAgent.ts").read_text(encoding="utf-8")
        source = (
            ROOT / "app/api/threads/[threadId]/messages/route.ts"
        ).read_text(encoding="utf-8")
        runtime = (ROOT / "app/api/_pythonRuntime.ts").read_text(encoding="utf-8")

        self.assertIn("runGeneralAgentTurn", source)
        self.assertIn('"bi_agent.runtime.general_agent_entry"', helper)
        self.assertIn("wajePythonInvocation", helper)
        self.assertIn('"uv"', runtime)
        self.assertIn('"3.12"', runtime)
        self.assertIn('"requirements.txt"', runtime)
        self.assertIn("loadCustomerAnalysisSnapshot", source)
        self.assertIn("pendingActionResolutionFrom", source)
        self.assertNotIn("runAgentCore", source)
        self.assertNotIn("claimRunDispatchRequest", source)
        self.assertNotIn("topicSelectionFrom", source)
        self.assertNotIn("topicChoiceAnswerFrom", source)

    def test_gateway_contract_does_not_import_agents_sdk_types(self):
        helper = (ROOT / "app/api/_generalAgent.ts").read_text(encoding="utf-8")
        route = (
            ROOT / "app/api/threads/[threadId]/messages/route.ts"
        ).read_text(encoding="utf-8")

        for source in (helper, route):
            self.assertNotIn('from "agents"', source)
            self.assertNotIn("RunConfig", source)
            self.assertNotIn("FunctionTool", source)
            self.assertNotIn("api.openai.com", source)

    def test_general_agent_startup_and_output_failures_are_typed(self):
        helper = (ROOT / "app/api/_generalAgent.ts").read_text(encoding="utf-8")
        store = (ROOT / "app/api/_conversationStore.ts").read_text(
            encoding="utf-8"
        )

        self.assertIn("WAJE_GENERAL_AGENT_RUNNING", helper)
        self.assertIn("general_agent_startup_failed", helper)
        self.assertIn("general_agent_output_malformed_json", helper)
        self.assertIn("general_agent_spawn_failed", store)
        self.assertIn("general_agent_process_failed", store)

    def test_existing_bi_agent_core_remains_inside_durable_worker(self):
        helper = (ROOT / "app/api/_agentCore.ts").read_text(encoding="utf-8")
        worker = (ROOT / "tools/runtime/recover_run_dispatches.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("bi_agent.conversation.agent_core", helper)
        self.assertIn("ConversationAgentCore", worker)
        self.assertIn("run_agent_core_dispatch", worker)
        self.assertIn("process_agent_task_resume_outbox", worker)


if __name__ == "__main__":
    unittest.main()
