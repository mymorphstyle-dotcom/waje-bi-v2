from collections import Counter
from pathlib import Path
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
                elif case["expected_topic_relation"] == "ask_topic_choice":
                    self.assertTrue(result.needs_clarification)
                    self.assertIsNone(result.run_request)
                else:
                    self.assertIsNotNone(result.run_request)

    def test_context_manifest_and_reuse_are_claim_safe(self):
        runtime = _seed_runtime()

        blocked = runtime.handle_message(
            "thread-phase7",
            "我现在只有普通权限，继续看刚才的细分。",
            role="business_reader",
        )
        self.assertEqual(blocked.reuse_decisions[0].decision, "blocked")
        self.assertFalse(blocked.context_manifest.can_support_claims)
        self.assertIn("permission_scope_mismatch", blocked.reuse_decisions[0].reason)

        stale = runtime.handle_message(
            "thread-phase7",
            "数据更新以后，这个判断现在还成立吗？",
            current_snapshot="2026H2",
        )
        self.assertEqual(stale.reuse_decisions[0].decision, "context_only")
        self.assertFalse(stale.context_manifest.can_support_claims)
        self.assertIn("snapshot_mismatch", stale.reuse_decisions[0].reason)

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


if __name__ == "__main__":
    unittest.main()
