import unittest
from pathlib import Path

from bi_agent.conversation.agent_core import ConversationAgentCore
from bi_agent.conversation.store import InMemoryConversationStore
from bi_agent.runtime.langgraph_workflow import WorkflowRunResult


class AgentCoreBridgeTest(unittest.TestCase):
    def test_agent_core_runs_workflow_and_persists_answer_package(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-agent-core", owner_id="analyst-1")
        core = ConversationAgentCore(store, workflow_runner=fake_workflow)

        result = core.run_message(
            thread_id="thread-agent-core",
            run_id="run-agent-core",
            user_message="Q2 比 Q1 付费金额为什么变了？",
            role="analyst",
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(store.runs["run-agent-core"]["status"], "completed")
        self.assertTrue(store.context_manifests)
        self.assertTrue(store.answer_packages)
        package = store.answer_packages["run-agent-core"]
        self.assertEqual(package["run_id"], "run-agent-core")
        self.assertEqual(package["sections"][0]["payload"]["answer_text"], "这是持久化的业务回答。")
        topic = store.current_topic("thread-agent-core")
        self.assertIsNotNone(store.latest_artifact_for_topic(topic.topic_id))
        self.assertTrue(
            any(event["event_type"] == "answer_package_recorded" for event in store.audit_events)
        )

    def test_agent_core_does_not_run_langgraph_for_capability_question(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-agent-core", owner_id="analyst-1")
        calls = []

        def runner(_request):
            calls.append(_request)
            return fake_workflow(_request)

        core = ConversationAgentCore(store, workflow_runner=runner)
        result = core.run_message(
            thread_id="thread-agent-core",
            run_id="run-capability-question",
            user_message="你现在能看哪些数据？",
            role="analyst",
        )

        self.assertEqual(result["status"], "completed_without_workflow")
        self.assertEqual(calls, [])
        self.assertIsNone(store.runs["run-capability-question"]["answer_package"])

    def test_agent_core_waits_for_structured_clarification_without_running_workflow(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-agent-core", owner_id="analyst-1")
        calls = []

        def runner(_request):
            calls.append(_request)
            return fake_workflow(_request)

        core = ConversationAgentCore(store, workflow_runner=runner)
        result = core.run_message(
            thread_id="thread-agent-core",
            run_id="run-needs-clarification",
            user_message="这个月是不是变好了？",
            role="analyst",
        )

        self.assertEqual(result["status"], "waiting_for_clarification")
        self.assertEqual(calls, [])
        self.assertIn("clarification", result)
        self.assertEqual(store.runs["run-needs-clarification"]["status"], "waiting_for_clarification")
        self.assertEqual(
            store.runs["run-needs-clarification"]["request"]["clarification"]["clarification_id"],
            result["clarification"]["clarification_id"],
        )
        self.assertTrue(
            any(
                event["event_type"] == "clarification_requested"
                and event["run_id"] == "run-needs-clarification"
                for event in store.audit_events
            )
        )

    def test_live_conversation_case_schema_supports_clarification_resume(self):
        from tools.phase7.run_live_conversation_system_test import load_cases

        cases = load_cases("evals/phase7/conversation_scenarios.yaml")
        case = next(item for item in cases if item["id"] == "q2_q1_wajespecial_long_followup")
        self.assertGreaterEqual(len(case["turns"]), 4)
        self.assertIs(case["turns"][3]["expect"]["allow_clarification"], True)
        self.assertIn("clarification_response", case["turns"][3])
        self.assertFalse(any("case_id" in item for item in cases))

    def test_live_conversation_harness_runs_clarification_and_resumes_same_topic(self):
        from tempfile import TemporaryDirectory

        from tools.phase7.run_live_conversation_system_test import load_cases, run_case

        case = next(
            item
            for item in load_cases("evals/phase7/conversation_scenarios.yaml")
            if item["id"] == "q2_q1_wajespecial_long_followup"
        )

        with TemporaryDirectory() as tmpdir:
            result = run_case(
                ConversationAgentCore.from_environment(),
                case,
                Path(tmpdir),
            )

        clarification_turn = result["turns"][3]
        self.assertEqual(clarification_turn["status"], "waiting_for_clarification")
        self.assertEqual(clarification_turn["resumed_status"], "completed")
        self.assertEqual(clarification_turn["topic_id"], clarification_turn["resumed_topic_id"])
        self.assertEqual(result["turns"][0]["topic_id"], result["turns"][1]["topic_id"])
        self.assertEqual(result["turns"][1]["topic_id"], result["turns"][2]["topic_id"])


def fake_workflow(request):
    return WorkflowRunResult(
        status="draft",
        run_id=request["run_id"],
        answer_package={
            "run_id": request["run_id"],
            "status": "draft",
            "snapshot_id": "2026H1",
            "permission_scope": "analyst",
            "follow_up_context": "这轮回答后可以继续追问渠道、异常和口径变化。",
            "sections": [
                {
                    "id": "summary",
                    "visibility": "business_summary",
                    "payload": {"answer_text": "这是持久化的业务回答。"},
                }
            ],
            "admin_audit": {"verifier": {"status": "passed"}},
        },
        artifact_path="artifacts/phase-7/run-agent-core/answer_package.json",
        checkpoint_events=({"node": "persist_artifact", "status": "completed"},),
    )


if __name__ == "__main__":
    unittest.main()
