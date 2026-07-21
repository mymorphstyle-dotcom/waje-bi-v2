from __future__ import annotations

from types import SimpleNamespace
import unittest

from bi_agent.conversation.models import ClarificationOption
from bi_agent.conversation.runtime import (
    ConversationOrchestrationError,
    ConversationRuntime,
)
from bi_agent.conversation.store import InMemoryConversationStore


class TypedConversationClient:
    def __init__(self, output: dict[str, object]) -> None:
        self.output = output
        self.calls: list[dict[str, object]] = []

    def invoke_json(self, *, task, prompt_version, messages, required_keys):
        self.calls.append(
            {
                "task": task,
                "prompt_version": prompt_version,
                "messages": list(messages),
                "required_keys": list(required_keys),
            }
        )
        return SimpleNamespace(
            output=dict(self.output),
            audit={
                "task": task,
                "prompt_version": prompt_version,
                "provider": "typed-test-client",
                "model": "typed-test-client",
            },
        )


class ConversationRuntimeTest(unittest.TestCase):
    def test_clarification_option_has_one_canonical_schema(self):
        option = ClarificationOption(
            option_id="comparison_baseline.previous_day",
            label="跟前一天比较",
            description="用于日变化解释。",
            recommended=True,
        )

        self.assertEqual(
            option.to_dict(),
            {
                "option_id": "comparison_baseline.previous_day",
                "label": "跟前一天比较",
                "description": "用于日变化解释。",
                "recommended": True,
            },
        )
        with self.assertRaises(TypeError):
            ClarificationOption(
                id="comparison_baseline.previous_day",
                label="跟前一天比较",
                business_meaning="用于日变化解释。",
                recommended=True,
            )

    def test_open_business_routing_uses_typed_llm_binding(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-open-semantics", owner_id="analyst-1")
        topic = store.create_topic(
            "thread-open-semantics",
            title="收入变化",
            summary="收入变化分析",
        )
        store.set_current_topic("thread-open-semantics", topic.topic_id)
        client = TypedConversationClient(
            {
                "intent": "challenge",
                "topic_relation": "inherit_current",
                "business_summary": "用户要求检查当前判断的证据边界。",
                "confidence": 0.93,
                "display_summary": "已绑定为当前问题的挑战。",
                "selected_topic_id": None,
                "topic_options": [],
                "recommended_topic_id": None,
            }
        )

        result = ConversationRuntime(store, llm_client=client).handle_message(
            "thread-open-semantics",
            "换一种完全不同的说法检查这个判断。",
        )

        self.assertEqual(result.turn_intent.intent, "challenge")
        self.assertEqual(
            result.turn_intent.decision_source, "llm_conversation_orchestrator"
        )
        self.assertEqual(result.topic_relation, "inherit_current")
        self.assertIsNotNone(result.run_request)
        self.assertNotIn("runtime_budget", result.run_request.to_dict())
        self.assertIsNone(result.context_manifest.snapshot_version)
        self.assertEqual(result.context_manifest.contract_versions, {})
        self.assertEqual(result.context_manifest.schema_fingerprint, "")
        self.assertEqual(
            [call["task"] for call in client.calls], ["conversation_orchestrator"]
        )

    def test_topic_ambiguity_returns_typed_choice_without_starting_analysis(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-topic-ambiguity", owner_id="analyst-1")
        current = store.create_topic(
            "thread-topic-ambiguity",
            title="当前问题",
            summary="当前业务问题",
        )
        alternative = store.create_topic(
            "thread-topic-ambiguity",
            title="另一个问题",
            summary="另一个业务问题",
        )
        store.set_current_topic("thread-topic-ambiguity", current.topic_id)
        client = TypedConversationClient(
            {
                "intent": "follow_up",
                "topic_relation": "ask_topic_choice",
                "business_summary": "用户引用的历史问题存在歧义。",
                "confidence": 0.78,
                "display_summary": "请确认你想继续哪一个问题。",
                "selected_topic_id": None,
                "topic_options": [
                    {
                        "topic_id": current.topic_id,
                        "label": "当前问题",
                        "description": "继续当前业务问题。",
                    },
                    {
                        "topic_id": alternative.topic_id,
                        "label": "另一个问题",
                        "description": "切换到另一个业务问题。",
                    },
                ],
                "recommended_topic_id": current.topic_id,
            }
        )

        result = ConversationRuntime(store, llm_client=client).handle_message(
            "thread-topic-ambiguity",
            "继续刚才提到的那个问题。",
        )

        self.assertNotIn("needs_clarification", result.to_dict())
        self.assertNotIn("clarification", result.to_dict())
        self.assertIsNone(result.run_request)
        self.assertEqual(
            result.interaction_response.to_dict(),
            {
                "schema_version": "typed-topic-choice.v1",
                "intent": "follow_up",
                "response_text": "请确认你想继续哪一个问题。",
                "options": [
                    {
                        "topic_id": current.topic_id,
                        "label": "当前问题",
                        "description": "继续当前业务问题。",
                    },
                    {
                        "topic_id": alternative.topic_id,
                        "label": "另一个问题",
                        "description": "切换到另一个业务问题。",
                    },
                ],
                "recommended_topic_id": current.topic_id,
                "allow_free_text": True,
            },
        )
        self.assertEqual(store.current_topic("thread-topic-ambiguity"), current)

    def test_referenced_topic_binds_exact_llm_selected_candidate(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-topic-selection", owner_id="analyst-1")
        current = store.create_topic(
            "thread-topic-selection",
            title="当前问题",
            summary="当前业务问题",
        )
        selected = store.create_topic(
            "thread-topic-selection",
            title="被引用的问题",
            summary="被引用的业务问题",
        )
        third = store.create_topic(
            "thread-topic-selection",
            title="无关问题",
            summary="无关业务问题",
        )
        store.set_current_topic("thread-topic-selection", current.topic_id)
        client = TypedConversationClient(
            {
                "intent": "follow_up",
                "topic_relation": "select_referenced_topic",
                "business_summary": "用户明确引用被引用的问题。",
                "confidence": 0.96,
                "display_summary": "已绑定到被引用的问题。",
                "selected_topic_id": selected.topic_id,
                "topic_options": [],
                "recommended_topic_id": None,
            }
        )

        result = ConversationRuntime(store, llm_client=client).handle_message(
            "thread-topic-selection",
            "继续被引用的问题。",
        )

        self.assertEqual(result.topic_id, selected.topic_id)
        self.assertNotEqual(result.topic_id, third.topic_id)
        self.assertEqual(store.current_topic("thread-topic-selection"), selected)
        self.assertIsNotNone(result.run_request)

    def test_topic_choice_rejects_option_outside_candidate_topics(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-topic-choice-invalid", owner_id="analyst-1")
        first = store.create_topic(
            "thread-topic-choice-invalid", title="问题一", summary="问题一"
        )
        second = store.create_topic(
            "thread-topic-choice-invalid", title="问题二", summary="问题二"
        )
        store.set_current_topic("thread-topic-choice-invalid", first.topic_id)
        client = TypedConversationClient(
            {
                "intent": "follow_up",
                "topic_relation": "ask_topic_choice",
                "business_summary": "历史引用存在歧义。",
                "confidence": 0.8,
                "display_summary": "请选择问题。",
                "selected_topic_id": None,
                "topic_options": [
                    {
                        "topic_id": first.topic_id,
                        "label": "问题一",
                        "description": "继续问题一。",
                    },
                    {
                        "topic_id": "topic-not-in-thread",
                        "label": "伪造问题",
                        "description": "不属于当前线程。",
                    },
                ],
                "recommended_topic_id": second.topic_id,
            }
        )

        with self.assertRaisesRegex(
            ConversationOrchestrationError,
            "^conversation_orchestrator_topic_choice_invalid$",
        ):
            ConversationRuntime(store, llm_client=client).handle_message(
                "thread-topic-choice-invalid",
                "继续那个问题。",
            )

    def test_open_text_is_not_reclassified_by_local_keyword_rules(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-safety", owner_id="analyst-1")
        client = TypedConversationClient(
            {
                "intent": "new_topic",
                "topic_relation": "new_topic",
                "business_summary": "用户请求执行分析。",
                "confidence": 0.99,
                "display_summary": "准备执行。",
                "selected_topic_id": None,
                "topic_options": [],
                "recommended_topic_id": None,
            }
        )

        runtime = ConversationRuntime(store, llm_client=client)
        for message in (
            "直接执行 SQL 并列出原始订单 ID。",
            "预测下个月付费金额。",
        ):
            result = runtime.handle_message("thread-safety", message)
            self.assertEqual(result.turn_intent.intent, "new_topic")
            self.assertIsNotNone(result.run_request)
            self.assertIsNone(result.interaction_response)

    def test_non_analysis_intent_returns_typed_interaction_response(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-interaction", owner_id="analyst-1")
        client = TypedConversationClient(
            {
                "intent": "capability_question",
                "topic_relation": "rejected",
                "business_summary": "用户询问当前可用分析能力。",
                "confidence": 0.99,
                "display_summary": "当前可基于已发布数据合同完成聚合分析。",
                "selected_topic_id": None,
                "topic_options": [],
                "recommended_topic_id": None,
            }
        )

        result = ConversationRuntime(store, llm_client=client).handle_message(
            "thread-interaction",
            "你现在能分析什么？",
        )

        self.assertIsNone(result.run_request)
        self.assertEqual(
            result.interaction_response.to_dict(),
            {
                "schema_version": "typed-interaction.v1",
                "intent": "capability_question",
                "response_text": "当前可基于已发布数据合同完成聚合分析。",
            },
        )

    def test_memory_proposal_preserves_typed_intent_user_text(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-memory", owner_id="analyst-1")
        client = TypedConversationClient(
            {
                "intent": "memory_update",
                "topic_relation": "rejected",
                "business_summary": "用户提出记忆调整。",
                "confidence": 0.98,
                "display_summary": "已生成待确认的记忆提案。",
                "selected_topic_id": None,
                "topic_options": [],
                "recommended_topic_id": None,
            }
        )
        message = "删掉之前关于渠道偏好的记忆"

        result = ConversationRuntime(store, llm_client=client).handle_message(
            "thread-memory",
            message,
        )

        self.assertEqual(result.memory_proposals[0].text, message)
        self.assertEqual(result.interaction_response.intent, "memory_update")

    def test_interaction_response_text_is_required_from_typed_binding(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-interaction-invalid", owner_id="analyst-1")
        client = TypedConversationClient(
            {
                "intent": "off_topic",
                "topic_relation": "rejected",
                "business_summary": "用户请求不属于 BI 分析。",
                "confidence": 0.99,
                "selected_topic_id": None,
                "topic_options": [],
                "recommended_topic_id": None,
            }
        )

        with self.assertRaisesRegex(
            ConversationOrchestrationError,
            "^conversation_orchestrator_interaction_response_invalid$",
        ):
            ConversationRuntime(store, llm_client=client).handle_message(
                "thread-interaction-invalid",
                "写一首诗",
            )


if __name__ == "__main__":
    unittest.main()
