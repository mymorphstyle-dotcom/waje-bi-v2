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
        self.assertEqual(result["accepted_graph"], [])
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

    def test_agent_core_failed_workflow_still_returns_context_manifest(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-agent-core", owner_id="analyst-1")
        core = ConversationAgentCore(store, workflow_runner=fake_failed_workflow)

        result = core.run_message(
            thread_id="thread-agent-core",
            run_id="run-failed-workflow",
            user_message="Q2 比 Q1 付费金额为什么变了？",
            role="analyst",
        )

        self.assertEqual(result["status"], "failed")
        self.assertIn("context_manifest", result)
        self.assertTrue(result["context_manifest"])

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

        self.assertEqual(result["status"], "passed")
        self.assertIn("run_id", result)
        self.assertIn("topic_id", result)
        self.assertIn("answer_package", result)
        self.assertIn("context_manifest", result)
        self.assertIn("accepted_graph", result)
        self.assertIn("llm_calls", result)
        self.assertIn("quality_review", result)
        clarification_turn = result["turns"][3]
        second_turn = result["turns"][1]
        third_turn = result["turns"][2]
        self.assertEqual(clarification_turn["status"], "waiting_for_clarification")
        self.assertEqual(clarification_turn["resumed_status"], "completed")
        self.assertEqual(clarification_turn["topic_id"], clarification_turn["resumed_topic_id"])
        self.assertEqual(result["turns"][0]["topic_id"], result["turns"][1]["topic_id"])
        self.assertEqual(result["turns"][1]["topic_id"], result["turns"][2]["topic_id"])
        self.assertEqual(second_turn["expectation_review"]["missing_required_capabilities"], [])
        self.assertEqual(third_turn["expectation_review"]["missing_required_capabilities"], [])
        for turn in result["turns"]:
            with self.subTest(turn=turn["index"]):
                self.assertTrue(turn["expectation_review"]["intent_passed"])
                self.assertTrue(turn["expectation_review"]["topic_relation_passed"])
                self.assertTrue(turn["expectation_review"]["context_manifest_present"])
                self.assertTrue(turn["expectation_review"]["context_manifest_can_support_claims"])
                self.assertTrue(turn["expectation_review"]["claim_support_policy_passed"])
                self.assertGreater(turn["expectation_review"]["claim_evidence_review"]["claim_count"], 0)
                self.assertEqual(
                    turn["expectation_review"]["claim_evidence_review"]["unsupported_evidence_refs"],
                    [],
                )
                self.assertEqual(turn["expectation_review"]["missing_final_answer_text"], [])
        self.assertIn("outlier_contribution", clarification_turn["resumed_accepted_graph"])

    def test_live_harness_rejects_claim_refs_without_traceable_source(self):
        from tools.phase7.run_live_conversation_system_test import _expectation_review

        review = _expectation_review(
            {"expect": {"final_answer_contains": ["结论"]}},
            {"intent": "follow_up", "topic_relation": "inherit_current"},
            {
                "intent": "follow_up",
                "topic_relation": "inherit_current",
                "answer_package": {
                    "sections": [
                        {
                            "payload": {
                                "answer_text": "结论",
                                "claims": [
                                    {
                                        "text": "结论",
                                        "evidence_refs": ["artifact:missing"],
                                    }
                                ],
                            }
                        }
                    ]
                },
                "context_manifest": {
                    "can_support_claims": True,
                    "items": [],
                },
            },
            [],
        )

        self.assertFalse(review["claim_support_policy_passed"])
        self.assertEqual(
            review["claim_evidence_review"]["unsupported_evidence_refs"],
            ["artifact:missing"],
        )

    def test_live_harness_loads_local_env_without_overriding_shell(self):
        import os
        from tempfile import TemporaryDirectory

        from tools.phase7.run_live_conversation_system_test import load_env_file

        with TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "WAJE_RUNTIME_DATABASE_URL=postgres://local\nWAJE_LLM_MODEL=deepseek-chat\n",
                encoding="utf-8",
            )
            old_database_url = os.environ.pop("WAJE_RUNTIME_DATABASE_URL", None)
            old_model = os.environ.get("WAJE_LLM_MODEL")
            os.environ["WAJE_LLM_MODEL"] = "shell-model"
            try:
                loaded = load_env_file(str(env_path))
                self.assertIn("WAJE_RUNTIME_DATABASE_URL", loaded)
                self.assertNotIn("WAJE_LLM_MODEL", loaded)
                self.assertEqual(os.environ["WAJE_RUNTIME_DATABASE_URL"], "postgres://local")
                self.assertEqual(os.environ["WAJE_LLM_MODEL"], "shell-model")
            finally:
                if old_database_url is None:
                    os.environ.pop("WAJE_RUNTIME_DATABASE_URL", None)
                else:
                    os.environ["WAJE_RUNTIME_DATABASE_URL"] = old_database_url
                if old_model is None:
                    os.environ.pop("WAJE_LLM_MODEL", None)
                else:
                    os.environ["WAJE_LLM_MODEL"] = old_model


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


def fake_failed_workflow(request):
    return WorkflowRunResult(
        status="failed",
        run_id=request["run_id"],
        failure_reason="synthetic_failure",
    )


if __name__ == "__main__":
    unittest.main()
