import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bi_agent.conversation.agent_core import (
    ConversationAgentCore,
    _parse_external_topic_choice_answer,
    _parse_external_topic_selection,
)
from bi_agent.conversation.store import InMemoryConversationStore
from bi_agent.runtime.context_manifest import (
    build_context_manifest_record,
    validated_context_manifest_record,
)
from bi_agent.runtime.evidence_authority import EvidenceIntegrityError
from bi_agent.runtime.langgraph_workflow import WorkflowRunResult


class _TypedConversationClient:
    def __init__(
        self,
        *,
        intent="new_topic",
        topic_relation="new_topic",
        selected_topic_id=None,
        topic_options=None,
        recommended_topic_id=None,
    ):
        self.intent = intent
        self.topic_relation = topic_relation
        self.selected_topic_id = selected_topic_id
        self.topic_options = list(topic_options or [])
        self.recommended_topic_id = recommended_topic_id

    def invoke_json(self, *, task, prompt_version, messages, required_keys):
        return SimpleNamespace(
            output={
                "intent": self.intent,
                "topic_relation": self.topic_relation,
                "business_summary": "测试请求已绑定到当前业务意图。",
                "confidence": 0.99,
                "display_summary": "测试请求已绑定。",
                "selected_topic_id": self.selected_topic_id,
                "topic_options": self.topic_options,
                "recommended_topic_id": self.recommended_topic_id,
            },
            audit={
                "task": task,
                "prompt_version": prompt_version,
                "provider": "typed-test-client",
                "model": "typed-test-client",
            },
        )


class AgentCoreBridgeTest(unittest.TestCase):
    def test_clarification_claims_exact_dispatch_before_startup_ack_and_decision(self):
        events: list[object] = []

        class Store:
            def get_thread(self, thread_id):
                self.thread_id = thread_id
                return SimpleNamespace(owner_id="owner-clarification")

            def set_actor_id(self, actor_id):
                self.actor_id = actor_id

            def claim_run_dispatch(self, **kwargs):
                events.append(("claim", kwargs))

        clarification = {
            "sourceRunId": "run-clarification-dispatch",
            "resolutionId": "single-authority:request-clarification-dispatch",
            "attemptRunId": "run-clarification-dispatch",
            "answer": "采用上一日作为比较基线",
            "selectedOptionId": "comparison_baseline.previous_day",
            "source": "user",
            "retryAttempt": False,
        }
        decision_recorded = {
            "status": "decision_recorded",
            "run_id": "run-clarification-dispatch",
        }
        terminal = {"status": "planned", "run_id": "run-clarification-dispatch"}

        def record_decision(**_kwargs):
            events.append("decision")
            return decision_recorded

        def resume_after_decision(_core, **_kwargs):
            events.append("resume")
            return terminal

        with (
            patch(
                "bi_agent.conversation.agent_core._emit_agent_core_startup_ack",
                side_effect=lambda: events.append("ack"),
            ),
            patch(
                "bi_agent.conversation.agent_core."
                "_record_single_authority_clarification_submission",
                side_effect=record_decision,
            ),
            patch.object(
                ConversationAgentCore,
                "_resume_authoritative_plan_after_decision",
                new=resume_after_decision,
            ),
        ):
            result = ConversationAgentCore(Store()).run_message(
                thread_id="thread-clarification-dispatch",
                run_id="run-clarification-dispatch",
                user_id="owner-clarification",
                user_message=clarification["answer"],
                clarification=clarification,
                run_dispatch={
                    "dispatch_id": "dispatch-clarification-dispatch",
                    "dispatch_owner_id": "owner-dispatch",
                    "lease_epoch": 4,
                },
            )

        self.assertEqual(result, terminal)
        self.assertEqual(
            events,
            [
                (
                    "claim",
                    {
                        "dispatch_id": "dispatch-clarification-dispatch",
                        "run_id": "run-clarification-dispatch",
                        "thread_id": "thread-clarification-dispatch",
                        "dispatch_owner_id": "owner-dispatch",
                        "lease_epoch": 4,
                    },
                ),
                "ack",
                "decision",
                "resume",
            ],
        )

    def test_agent_core_enforces_personal_thread_ownership_before_workflow(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-owner-boundary", owner_id="user-owner")
        workflow_calls = []

        def workflow(request):
            workflow_calls.append(request)
            return WorkflowRunResult(
                status="failed",
                run_id=request["run_id"],
                failure_reason="synthetic_failure",
            )

        core = ConversationAgentCore(
            store,
            workflow_runner=workflow,
            conversation_llm_client=_TypedConversationClient(),
        )
        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "^thread_owner_mismatch$",
        ):
            core.run_message(
                thread_id="thread-owner-boundary",
                run_id="run-wrong-owner",
                user_id="user-other",
                user_message="Q2 比 Q1 付费金额为什么变了？",
                stop_after_phase="phase03",
            )

        self.assertEqual(workflow_calls, [])
        self.assertNotIn("run-wrong-owner", store.runs)

        result = core.run_message(
            thread_id="thread-owner-boundary",
            run_id="run-correct-owner",
            user_id="user-owner",
            user_message="Q2 比 Q1 付费金额为什么变了？",
            stop_after_phase="phase03",
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(workflow_calls), 1)
        self.assertNotIn("user_id", workflow_calls[0])
        self.assertNotIn("owner_ref", workflow_calls[0])
        self.assertTrue(
            any(event.get("actor_id") == "user-owner" for event in store.audit_events)
        )

    def test_context_manifest_current_schema_and_integrity_validation(self):
        current = build_context_manifest_record(
            run_id="run-manifest-version",
            thread_id="thread-manifest-version",
            topic_id="topic-manifest-version",
            sources=(
                {
                    "type": "evidence",
                    "ref": "evidence:1",
                    "can_support_claim": True,
                },
            ),
            accepted_assumptions=({"action_kind": "omit_unavailable_context"},),
        )
        self.assertEqual(current["manifest_schema_version"], "3")
        self.assertEqual(validated_context_manifest_record(current), current)

        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "integrity_invalid",
        ):
            validated_context_manifest_record({**current, "thread_id": "tampered"})
        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "payload_keys_invalid",
        ):
            validated_context_manifest_record({**current, "unknown": True})

    def test_agent_core_does_not_run_workflow_for_capability_question(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-capabilities", owner_id="analyst-1")
        calls = []

        def runner(request):
            calls.append(request)
            raise AssertionError("capability_question_ran_workflow")

        result = ConversationAgentCore(
            store,
            workflow_runner=runner,
            conversation_llm_client=_TypedConversationClient(
                intent="capability_question",
                topic_relation="rejected",
            ),
        ).run_message(
            thread_id="thread-capabilities",
            run_id="run-capability-question",
            user_message="你现在能看哪些数据？",
        )

        self.assertEqual(result["status"], "interaction_completed")
        self.assertEqual(
            result["interaction_result"],
            {
                "schema_version": "typed-interaction.v1",
                "intent": "capability_question",
                "response_text": "测试请求已绑定。",
            },
        )
        self.assertEqual(calls, [])

    def test_topic_choice_resumes_original_message_on_exact_persisted_topic(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-topic-choice", owner_id="analyst-1")
        current = store.create_topic(
            "thread-topic-choice",
            title="当前问题",
            summary="当前业务问题",
        )
        selected = store.create_topic(
            "thread-topic-choice",
            title="历史问题",
            summary="历史业务问题",
        )
        store.set_current_topic("thread-topic-choice", current.topic_id)
        original_message = "继续刚才提到的那个问题。"
        choice_client = _TypedConversationClient(
            intent="follow_up",
            topic_relation="ask_topic_choice",
            topic_options=[
                {
                    "topic_id": current.topic_id,
                    "label": "当前问题",
                    "description": "继续当前业务问题。",
                },
                {
                    "topic_id": selected.topic_id,
                    "label": "历史问题",
                    "description": "切换到历史业务问题。",
                },
            ],
            recommended_topic_id=current.topic_id,
        )
        first = ConversationAgentCore(
            store,
            workflow_runner=lambda _: self.fail("topic_choice_started_workflow"),
            conversation_llm_client=choice_client,
        ).run_message(
            thread_id="thread-topic-choice",
            run_id="run-topic-choice-source",
            user_message=original_message,
        )

        self.assertEqual(first["status"], "interaction_completed")
        self.assertEqual(
            first["interaction_result"]["schema_version"],
            "typed-topic-choice.v1",
        )
        workflow_calls = []

        class UnexpectedConversationClient:
            def invoke_json(self, **_):
                raise AssertionError("persisted_topic_choice_reclassified")

        def runner(request):
            workflow_calls.append(request)
            return WorkflowRunResult(
                status="failed",
                run_id=request["run_id"],
                failure_reason="synthetic_failure",
            )

        resumed = ConversationAgentCore(
            store,
            workflow_runner=runner,
            conversation_llm_client=UnexpectedConversationClient(),
        ).run_message(
            thread_id="thread-topic-choice",
            run_id="run-topic-choice-resumed",
            user_message="选择历史问题",
            topic_selection={
                "source_run_id": "run-topic-choice-source",
                "topic_id": selected.topic_id,
            },
            stop_after_phase="phase03",
        )

        self.assertEqual(resumed["status"], "failed")
        self.assertEqual(len(workflow_calls), 1)
        self.assertEqual(workflow_calls[0]["question"], original_message)
        self.assertNotIn("topic_id", workflow_calls[0])
        self.assertNotIn("topic_relation", workflow_calls[0])
        self.assertNotIn("topic_selection", workflow_calls[0])
        persisted_request = store.get_run_state("run-topic-choice-resumed")["request"]
        self.assertEqual(persisted_request["topic_id"], selected.topic_id)
        self.assertEqual(persisted_request["topic_relation"], "select_referenced_topic")
        self.assertEqual(
            persisted_request["topic_selection"]["source_run_id"],
            "run-topic-choice-source",
        )
        self.assertEqual(store.current_topic("thread-topic-choice"), selected)

    def test_topic_choice_rejects_topic_outside_persisted_options(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-topic-choice-invalid", owner_id="analyst-1")
        first = store.create_topic(
            "thread-topic-choice-invalid", title="问题一", summary="问题一"
        )
        second = store.create_topic(
            "thread-topic-choice-invalid", title="问题二", summary="问题二"
        )
        outside = store.create_topic(
            "thread-topic-choice-invalid", title="问题三", summary="问题三"
        )
        client = _TypedConversationClient(
            intent="follow_up",
            topic_relation="ask_topic_choice",
            topic_options=[
                {
                    "topic_id": first.topic_id,
                    "label": "问题一",
                    "description": "继续问题一。",
                },
                {
                    "topic_id": second.topic_id,
                    "label": "问题二",
                    "description": "继续问题二。",
                },
            ],
            recommended_topic_id=first.topic_id,
        )
        core = ConversationAgentCore(store, conversation_llm_client=client)
        core.run_message(
            thread_id="thread-topic-choice-invalid",
            run_id="run-topic-choice-invalid-source",
            user_message="继续那个问题。",
        )

        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "^topic_selection_option_invalid$",
        ):
            core.run_message(
                thread_id="thread-topic-choice-invalid",
                run_id="run-topic-choice-invalid-resume",
                user_message="选择问题三",
                topic_selection={
                    "source_run_id": "run-topic-choice-invalid-source",
                    "topic_id": outside.topic_id,
                },
            )

    def test_topic_choice_free_text_restores_source_context_for_typed_binding(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-topic-free-text", owner_id="analyst-1")
        first = store.create_topic(
            "thread-topic-free-text", title="当前问题", summary="当前问题"
        )
        second = store.create_topic(
            "thread-topic-free-text", title="历史问题", summary="历史问题"
        )
        original_message = "继续刚才提到的那个问题。"
        source_client = _TypedConversationClient(
            intent="follow_up",
            topic_relation="ask_topic_choice",
            topic_options=[
                {
                    "topic_id": first.topic_id,
                    "label": "当前问题",
                    "description": "继续当前问题。",
                },
                {
                    "topic_id": second.topic_id,
                    "label": "历史问题",
                    "description": "继续历史问题。",
                },
            ],
            recommended_topic_id=first.topic_id,
        )
        ConversationAgentCore(
            store,
            workflow_runner=lambda _: self.fail("topic_choice_started_workflow"),
            conversation_llm_client=source_client,
        ).run_message(
            thread_id="thread-topic-free-text",
            run_id="run-topic-free-text-source",
            user_message=original_message,
        )

        class FreeTextClient:
            def __init__(self):
                self.calls = []

            def invoke_json(self, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(
                    output={
                        "intent": "follow_up",
                        "topic_relation": "select_referenced_topic",
                        "business_summary": "用户补充后明确选择历史问题。",
                        "confidence": 0.97,
                        "display_summary": "已绑定历史问题。",
                        "selected_topic_id": second.topic_id,
                        "topic_options": [],
                        "recommended_topic_id": None,
                    },
                    audit={"provider": "typed-test-client"},
                )

        client = FreeTextClient()
        workflow_calls = []

        def runner(request):
            workflow_calls.append(request)
            return WorkflowRunResult(
                status="failed",
                run_id=request["run_id"],
                failure_reason="synthetic_failure",
            )

        answer = "两个选项都不准确，请按历史收入问题继续。"
        result = ConversationAgentCore(
            store,
            workflow_runner=runner,
            conversation_llm_client=client,
        ).run_message(
            thread_id="thread-topic-free-text",
            run_id="run-topic-free-text-resume",
            user_message=answer,
            topic_choice_answer={
                "source_run_id": "run-topic-free-text-source",
                "answer": answer,
            },
            stop_after_phase="phase03",
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(client.calls), 1)
        self.assertIn(original_message, str(client.calls[0]["messages"]))
        self.assertNotIn("topic_id", workflow_calls[0])
        self.assertEqual(
            store.get_run_state("run-topic-free-text-resume")["request"]["topic_id"],
            second.topic_id,
        )
        self.assertEqual(
            workflow_calls[0]["question"],
            f"原始业务问题：{original_message}\n用户对话题归属的补充：{answer}",
        )

    def test_external_topic_choice_envelopes_are_exact_and_normalized(self):
        self.assertEqual(
            _parse_external_topic_selection(
                '{"sourceRunId":"run-source","topicId":"topic-1"}'
            ),
            {"source_run_id": "run-source", "topic_id": "topic-1"},
        )
        self.assertEqual(
            _parse_external_topic_choice_answer(
                '{"sourceRunId":"run-source","answer":"采用另一种方式"}'
            ),
            {"source_run_id": "run-source", "answer": "采用另一种方式"},
        )
        with self.assertRaisesRegex(ValueError, "topic_selection_envelope_invalid"):
            _parse_external_topic_selection(
                '{"source_run_id":"run-source","topic_id":"topic-1"}'
            )

    def test_agent_core_failed_workflow_keeps_context_and_audits(self):
        audits = (
            {
                "task": "single_authority_intent",
                "provider": "contract-test-provider",
                "model": "contract-test-model",
                "prompt_version": "contract-test-v1",
                "response_id": "response-failed-workflow",
                "structured_output": {"status": "failed"},
                "raw_response_content": '{"status":"failed"}',
            },
        )
        checkpoints = (
            {
                "node": "understand_business_intent",
                "attempt": 1,
                "status": "failed",
            },
        )

        def workflow(request):
            return WorkflowRunResult(
                status="failed",
                run_id=request["run_id"],
                failure_reason="synthetic_failure",
                checkpoint_events=checkpoints,
                llm_calls=audits,
            )

        store = InMemoryConversationStore()
        result = ConversationAgentCore(
            store,
            workflow_runner=workflow,
            conversation_llm_client=_TypedConversationClient(),
        ).run_message(
            thread_id="thread-failed-workflow",
            run_id="run-failed-workflow",
            user_message="Q2 比 Q1 付费金额为什么变了？",
            stop_after_phase="phase03",
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_owner"], "workflow_runtime_owner")
        self.assertTrue(result["context_manifest"])
        self.assertEqual(
            tuple(store.runs["run-failed-workflow"]["checkpoint_events"]),
            checkpoints,
        )
        recorded = tuple(
            event
            for event in store.audit_events
            if event["event_type"] == "workflow_failure_llm_call_recorded"
        )
        self.assertEqual(tuple(event["payload"] for event in recorded), audits)


if __name__ == "__main__":
    unittest.main()
