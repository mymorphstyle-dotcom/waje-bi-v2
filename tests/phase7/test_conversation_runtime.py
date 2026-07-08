from collections import Counter
from pathlib import Path
from types import SimpleNamespace
import unittest

import yaml

from bi_agent.conversation.runtime import ConversationRuntime
from bi_agent.conversation.store import InMemoryConversationStore


ROOT = Path(__file__).resolve().parents[2]
CASE_FILE = ROOT / "evals" / "phase7" / "conversation_scenarios.yaml"


def _cases():
    return yaml.safe_load(CASE_FILE.read_text(encoding="utf-8"))["cases"]


class ConversationRuntimeTest(unittest.TestCase):
    def test_manifest_has_required_natural_language_coverage(self):
        data = yaml.safe_load(CASE_FILE.read_text(encoding="utf-8"))
        cases = data["cases"]
        counts = Counter(case["group"] for case in cases)

        self.assertGreaterEqual(len(cases), data["minimum_cases"])
        self.assertGreaterEqual(counts["continuous_follow_up"], 20)
        self.assertGreaterEqual(counts["mixed_question"], 10)
        self.assertGreaterEqual(counts["offtopic_capability_unsupported"], 10)
        self.assertGreaterEqual(counts["permission_snapshot_memory"], 10)
        self.assertGreaterEqual(counts["correction_challenge_clarification"], 10)
        for case in cases:
            with self.subTest(case=case["case_id"]):
                self.assertTrue(case["user_message"])
                self.assertIn("expected_intent", case)
                self.assertIn("expected_topic_relation", case)
                self.assertIn("expected_context_use", case)
                self.assertIn("expected_forbidden_context", case)
                self.assertIn("expected_reuse", case)
                self.assertIn("expected_langgraph", case)
                self.assertTrue(case["expected_answer_boundary"])

    def test_runtime_classifies_all_conversation_scenarios(self):
        for case in _cases():
            with self.subTest(case=case["case_id"]):
                runtime = _seed_runtime()
                result = runtime.handle_message(
                    "thread-phase7",
                    case["user_message"],
                    role="business_reader" if case["case_id"] == "psm_004" else "analyst",
                    active_run_status="running" if case["case_id"] == "ccc_009" else "idle",
                    current_snapshot="2026H2" if case["case_id"] in {"psm_005", "psm_010"} else "2026H1",
                )

                self.assertEqual(result.turn_intent.intent, case["expected_intent"])
                self.assertEqual(result.topic_relation, case["expected_topic_relation"])
                self.assertTrue(result.context_manifest.items)
                self.assertTrue(result.audit_events)
                self.assertIn(
                    case["expected_reuse"],
                    [decision.decision for decision in result.reuse_decisions],
                )
                if case["expected_intent"] in {
                    "off_topic",
                    "capability_question",
                    "unsupported_request",
                    "memory_update",
                }:
                    self.assertIsNone(result.run_request)
                elif case["expected_langgraph"].get("ask_question"):
                    self.assertTrue(result.needs_clarification)
                    self.assertIsNone(result.run_request)
                    self.assertIsNotNone(result.clarification)
                else:
                    self.assertIsNotNone(result.run_request)

    def test_context_manifest_and_reuse_are_claim_safe(self):
        runtime = _seed_runtime()

        reusable = runtime.handle_message(
            "thread-phase7",
            "那具体哪些渠道贡献最大？",
        )
        result_items = [
            item for item in reusable.context_manifest.items if item.source_type == "result_ref"
        ]
        self.assertEqual(len(result_items), 1)
        self.assertEqual(result_items[0].source_ref, "result:q2-q1:paid_amount")
        self.assertTrue(result_items[0].can_support_claims)
        self.assertEqual(result_items[0].claim_use, "reuse")
        self.assertEqual(result_items[0].source_version, "contracts-v1:2026H1")
        self.assertTrue(reusable.context_manifest.can_support_claims)

        blocked = runtime.handle_message(
            "thread-phase7",
            "我现在只有普通权限，继续看刚才的细分。",
            role="business_reader",
        )
        self.assertEqual(blocked.reuse_decisions[0].decision, "blocked")
        self.assertFalse(blocked.context_manifest.can_support_claims)
        self.assertIn("permission_scope_mismatch", blocked.reuse_decisions[0].reason)
        blocked_result_items = [
            item for item in blocked.context_manifest.items if item.source_type == "result_ref"
        ]
        self.assertTrue(blocked_result_items[0].expired)
        self.assertEqual(blocked_result_items[0].claim_use, "blocked")

        stale = runtime.handle_message(
            "thread-phase7",
            "数据更新以后，这个判断现在还成立吗？",
            current_snapshot="2026H2",
        )
        self.assertEqual(stale.reuse_decisions[0].decision, "context_only")
        self.assertFalse(stale.context_manifest.can_support_claims)
        self.assertIn("snapshot_mismatch", stale.reuse_decisions[0].reason)
        stale_result_items = [
            item for item in stale.context_manifest.items if item.source_type == "result_ref"
        ]
        self.assertTrue(stale_result_items[0].expired)
        self.assertEqual(stale_result_items[0].claim_use, "context_only")

    def test_memory_update_creates_audited_proposal_without_long_term_write(self):
        runtime = _seed_runtime()
        before = runtime.store.long_term_memory("org-default")

        result = runtime.handle_message(
            "thread-phase7",
            "以后默认把 WajeSpecial 单独看。",
        )

        self.assertEqual(result.turn_intent.intent, "memory_update")
        self.assertEqual(len(result.memory_proposals), 1)
        self.assertEqual(result.memory_proposals[0].status, "proposed")
        self.assertEqual(runtime.store.long_term_memory("org-default"), before)

    def test_memory_items_have_refresh_and_revocation_metadata(self):
        store = InMemoryConversationStore()
        item = store.add_memory_item(
            owner_scope="org-default",
            text="默认把 WajeSpecial 单独观察。",
            source_ref="memory:accepted:wajespecial",
            visibility="analyst",
            status="accepted",
        )

        self.assertEqual(item.ttl, "until_revoked")
        self.assertEqual(item.refresh_rule, "refresh_on_contract_or_scope_change")
        self.assertEqual(item.revocation_path, "memory_proposal_revoke_or_admin_action")

    def test_clarification_answer_resumes_pending_topic_even_when_current_topic_changed(self):
        store = InMemoryConversationStore()
        runtime = ConversationRuntime(store)
        store.create_thread("thread-clarify", owner_id="analyst-1")
        q2_topic = store.create_topic("thread-clarify", title="Q2 vs Q1", summary="Q2/Q1")
        month_topic = store.create_topic("thread-clarify", title="1 月月初", summary="1 月月初")
        store.set_current_topic("thread-clarify", q2_topic.topic_id)
        store.set_pending_clarification("thread-clarify", month_topic.topic_id, "metric_choice")

        result = runtime.handle_message("thread-clarify", "日均。")

        self.assertEqual(result.turn_intent.intent, "clarification_answer")
        self.assertEqual(result.topic_id, month_topic.topic_id)
        self.assertEqual(result.run_request.topic_id, month_topic.topic_id)
        self.assertEqual(store.get_thread("thread-clarify").pending_clarification_id, "")
        self.assertTrue(
            any(item.source_type == "clarification" for item in result.context_manifest.items)
        )

    def test_q_comparison_starts_new_topic_and_outlier_strategy_clarification_resumes_same_topic(self):
        store = InMemoryConversationStore()
        runtime = ConversationRuntime(store)
        store.create_thread("thread-live-case", owner_id="analyst-1")

        first = runtime.handle_message("thread-live-case", "Q2 相比 Q1 付费金额为什么变了？")
        self.assertEqual(first.turn_intent.intent, "new_topic")
        self.assertEqual(first.topic_relation, "new_topic")
        self.assertIsNotNone(first.topic_id)

        second = runtime.handle_message("thread-live-case", "那具体哪些渠道贡献最大？")
        self.assertIn("segment_contribution", second.run_request.requested_nodes)
        self.assertIn("joint_attribution", second.run_request.requested_nodes)

        third = runtime.handle_message("thread-live-case", "这些渠道里 WajeSpecial 是主要原因吗？")
        self.assertIn("joint_attribution", third.run_request.requested_nodes)

        follow_up = runtime.handle_message("thread-live-case", "如果去掉异常天还成立吗？")
        self.assertEqual(follow_up.turn_intent.intent, "challenge")
        self.assertTrue(follow_up.needs_clarification)
        self.assertEqual(follow_up.topic_id, first.topic_id)
        self.assertEqual(
            follow_up.clarification.reason,
            "outlier_removal_strategy_changes_business_answer",
        )

        resumed = runtime.handle_message(
            "thread-live-case",
            "按日粒度，移除贡献最大的正向日期后复算，不做订单级明细剔除。",
        )
        self.assertEqual(resumed.turn_intent.intent, "clarification_answer")
        self.assertEqual(resumed.topic_id, first.topic_id)
        self.assertIsNotNone(resumed.run_request)
        self.assertIn("outlier_contribution", resumed.run_request.requested_nodes)

    def test_outlier_variant_question_triggers_outlier_strategy_clarification(self):
        store = InMemoryConversationStore()
        runtime = ConversationRuntime(store)
        store.create_thread("thread-outlier-variant", owner_id="analyst-1")
        first = runtime.handle_message("thread-outlier-variant", "Q2 相比 Q1 付费金额为什么变了？")

        result = runtime.handle_message("thread-outlier-variant", "剔除异常日后还成立吗？")

        self.assertEqual(result.turn_intent.intent, "challenge")
        self.assertTrue(result.needs_clarification)
        self.assertEqual(result.topic_id, first.topic_id)
        self.assertEqual(
            result.clarification.reason,
            "outlier_removal_strategy_changes_business_answer",
        )

    def test_ambiguous_question_creates_structured_clarification_without_starting_run(self):
        store = InMemoryConversationStore()
        runtime = ConversationRuntime(store)
        store.create_thread("thread-ambiguous", owner_id="analyst-1")

        result = runtime.handle_message("thread-ambiguous", "这个月是不是变好了？")

        self.assertEqual(result.turn_intent.intent, "new_topic")
        self.assertTrue(result.needs_clarification)
        self.assertIsNone(result.run_request)
        self.assertIsNotNone(result.clarification)
        self.assertEqual(
            store.get_thread("thread-ambiguous").pending_clarification_id,
            result.clarification.clarification_id,
        )
        self.assertLessEqual(len(result.clarification.questions), 4)
        question = result.clarification.questions[0]
        self.assertLessEqual(len(question.options), 3)
        self.assertEqual(
            len([option for option in question.options if option.recommended]),
            1,
        )
        self.assertTrue(
            any(option.option_id == "tell_agent_differently" for option in question.options)
        )
        self.assertTrue(
            any(event["event_type"] == "clarification_requested" for event in store.audit_events)
        )

    def test_ambiguous_topic_reference_replaces_stale_pending_clarification(self):
        store = InMemoryConversationStore()
        runtime = ConversationRuntime(store)
        store.create_thread("thread-topic-choice", owner_id="analyst-1")
        q2_topic = store.create_topic("thread-topic-choice", title="Q2 vs Q1", summary="Q2/Q1")
        store.create_topic("thread-topic-choice", title="1 月月初", summary="1 月月初")
        store.set_current_topic("thread-topic-choice", q2_topic.topic_id)
        store.set_pending_clarification("thread-topic-choice", q2_topic.topic_id, "old-clarification")

        result = runtime.handle_message("thread-topic-choice", "刚才那个继续看渠道。")

        self.assertTrue(result.needs_clarification)
        self.assertIsNotNone(result.clarification)
        self.assertEqual(
            store.get_thread("thread-topic-choice").pending_clarification_id,
            result.clarification.clarification_id,
        )
        self.assertEqual(
            store.get_thread("thread-topic-choice").pending_clarification_topic_id,
            q2_topic.topic_id,
        )

    def test_llm_conversation_orchestrator_can_bind_business_intent(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-llm-route", owner_id="analyst-1")
        topic = store.create_topic(
            "thread-llm-route",
            title="Q2 vs Q1",
            summary="当前 topic 关注 Q2 相比 Q1 的变化。",
        )
        store.set_current_topic("thread-llm-route", topic.topic_id)
        fake = FakeConversationLLM(
            {
                "intent": "challenge",
                "topic_relation": "inherit_current",
                "business_summary": "用户在质疑既有结论是否受到 WajeSpecial 干扰。",
                "confidence": 0.91,
            }
        )
        runtime = ConversationRuntime(store, llm_client=fake)

        result = runtime.handle_message(
            "thread-llm-route",
            "这个结论是不是被 WajeSpecial 干扰了？",
        )

        self.assertEqual(result.turn_intent.intent, "challenge")
        self.assertEqual(result.topic_relation, "inherit_current")
        self.assertEqual(result.turn_intent.decision_source, "llm_conversation_orchestrator")
        self.assertEqual(fake.calls[0]["task"], "conversation_orchestrator")
        self.assertTrue(
            any(event["event"] == "conversation_orchestrator_llm_evaluated" for event in result.audit_events)
        )

    def test_local_guard_blocks_unsupported_request_even_when_llm_disagrees(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-llm-guard", owner_id="analyst-1")
        fake = FakeConversationLLM(
            {
                "intent": "follow_up",
                "topic_relation": "inherit_current",
                "business_summary": "用户想继续分析。",
                "confidence": 0.93,
            }
        )
        runtime = ConversationRuntime(store, llm_client=fake)

        result = runtime.handle_message("thread-llm-guard", "直接写 SQL 查所有订单。")

        self.assertEqual(result.turn_intent.intent, "unsupported_request")
        self.assertEqual(result.topic_relation, "rejected")
        self.assertEqual(result.turn_intent.decision_source, "local_conversation_orchestrator_guard")
        self.assertIsNone(result.run_request)

    def test_invalid_llm_orchestration_falls_back_to_local_precheck(self):
        store = InMemoryConversationStore()
        store.create_thread("thread-llm-fallback", owner_id="analyst-1")
        fake = FakeConversationLLM(
            {
                "intent": "raw_sql",
                "topic_relation": "magic_route",
                "business_summary": "无效输出。",
                "confidence": 0.99,
            }
        )
        runtime = ConversationRuntime(store, llm_client=fake)

        result = runtime.handle_message("thread-llm-fallback", "那具体哪些渠道贡献最大？")

        self.assertEqual(result.turn_intent.intent, "follow_up")
        self.assertEqual(result.topic_relation, "inherit_current")
        self.assertEqual(result.turn_intent.decision_source, "local_conversation_orchestrator_fallback")


def _seed_runtime() -> ConversationRuntime:
    store = InMemoryConversationStore()
    runtime = ConversationRuntime(store)
    store.create_thread("thread-phase7", owner_id="analyst-1")
    q2_topic = store.create_topic(
        "thread-phase7",
        title="Q2 vs Q1 付费金额变化",
        summary="当前 topic 关注 2026 Q2 相比 Q1 的付费金额变化。",
    )
    month_topic = store.create_topic(
        "thread-phase7",
        title="1 月月初模式",
        summary="第二个 topic 关注 1 月月初付费模式。",
    )
    store.set_current_topic("thread-phase7", q2_topic.topic_id)
    store.add_result_ref(
        q2_topic.topic_id,
        result_ref="result:q2-q1:paid_amount",
        snapshot_id="2026H1",
        contract_version="contracts-v1",
        permission_scope="analyst",
        semantic_scope="q2_vs_q1_paid_amount",
    )
    store.add_result_ref(
        month_topic.topic_id,
        result_ref="result:jan-month-start",
        snapshot_id="2026H1",
        contract_version="contracts-v1",
        permission_scope="analyst",
        semantic_scope="jan_month_start_pattern",
    )
    store.add_artifact(
        artifact_id="artifact:q2-q1",
        topic_id=q2_topic.topic_id,
        follow_up_context="Q2/Q1 变化的已验证 Answer Package。",
        snapshot_id="2026H1",
        permission_scope="analyst",
    )
    store.add_memory_item(
        owner_scope="org-default",
        text="默认把 WajeSpecial 单独观察。",
        source_ref="memory:accepted:wajespecial",
        visibility="analyst",
        status="accepted",
    )
    store.set_pending_clarification("thread-phase7", q2_topic.topic_id, "metric_choice")
    return runtime


class FakeConversationLLM:
    def __init__(self, output):
        self.output = output
        self.calls = []

    def invoke_json(self, *, task, prompt_version, messages, required_keys):
        self.calls.append(
            {
                "task": task,
                "prompt_version": prompt_version,
                "messages": [dict(message) for message in messages],
                "required_keys": list(required_keys),
            }
        )
        output = dict(self.output)
        for key in required_keys:
            output.setdefault(key, "已完成本轮对话路由判断。")
        return SimpleNamespace(
            output=output,
            audit={
                "task": task,
                "prompt_version": prompt_version,
                "provider": "fake",
                "model": "fake",
                "structured_output": output,
            },
        )


if __name__ == "__main__":
    unittest.main()
