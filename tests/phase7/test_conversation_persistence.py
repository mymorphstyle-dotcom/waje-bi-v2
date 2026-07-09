from pathlib import Path
import unittest

from bi_agent.conversation.models import ContextManifest, MemoryProposal
from bi_agent.conversation.postgres_store import CONVERSATION_SCHEMA_SQL, PostgresConversationStore
from bi_agent.conversation.runtime import evaluate_reuse_candidate
from bi_agent.conversation.store import InMemoryConversationStore


ROOT = Path(__file__).resolve().parents[2]


class ConversationPersistenceTest(unittest.TestCase):
    def test_context_manifest_records_claim_usable_sources(self):
        store = InMemoryConversationStore()
        manifest = ContextManifest(
            manifest_id="manifest-1",
            thread_id="t1",
            turn_id="turn-1",
            topic_id="topic-1",
            sources=[{"type": "answer_package", "ref": "artifact-1", "can_support_claim": True}],
            claim_use_policy={"requires_evidence_ref": True},
            snapshot_version="2026-H1",
            permission_context={"role": "analyst"},
            created_at="2026-07-08T00:00:00Z",
        )

        store.save_context_manifest(manifest)

        loaded = store.list_context_manifests("t1")[0]
        self.assertIs(loaded.sources[0]["can_support_claim"], True)
        self.assertIs(loaded.claim_use_policy["requires_evidence_ref"], True)

    def test_context_manifest_partial_claim_policy_keeps_defaults(self):
        manifest = ContextManifest(
            manifest_id="manifest-partial-policy",
            thread_id="t1",
            turn_id="turn-1",
            sources=[{"type": "answer_package", "ref": "artifact-1", "can_support_claim": True}],
            claim_use_policy={"requires_evidence_ref": False},
        )

        self.assertIs(manifest.claim_use_policy["requires_evidence_ref"], False)
        self.assertIs(manifest.claim_use_policy["can_support_bi_claim"], True)

    def test_reuse_decision_blocks_stale_snapshot_claim_support(self):
        decision = evaluate_reuse_candidate(
            source_snapshot="2026-H1",
            current_snapshot="2026-H2",
            permission_match=True,
            semantic_scope_match=True,
        )

        self.assertEqual(decision.decision, "context_only")
        self.assertIs(decision.can_support_claim, False)

    def test_reuse_decision_uses_stable_reason_codes(self):
        stale = evaluate_reuse_candidate(
            source_snapshot="2026-H1",
            current_snapshot="2026-H2",
            permission_match=True,
            semantic_scope_match=True,
        )
        blocked = evaluate_reuse_candidate(
            source_snapshot="2026-H1",
            current_snapshot="2026-H1",
            permission_match=False,
            semantic_scope_match=True,
        )
        scoped = evaluate_reuse_candidate(
            source_snapshot="2026-H1",
            current_snapshot="2026-H1",
            permission_match=True,
            semantic_scope_match=False,
        )
        reusable = evaluate_reuse_candidate(
            source_snapshot="2026-H1",
            current_snapshot="2026-H1",
            permission_match=True,
            semantic_scope_match=True,
        )

        self.assertEqual(stale.reason, "snapshot_mismatch")
        self.assertEqual(blocked.reason, "permission_scope_mismatch")
        self.assertEqual(scoped.reason, "semantic_scope_mismatch")
        self.assertEqual(reusable.reason, "validated_same_thread_scope")

    def test_schema_declares_required_runtime_tables(self):
        required_tables = {
            "investigation_threads",
            "conversation_topics",
            "conversation_turns",
            "conversation_messages",
            "analysis_runs",
            "run_nodes",
            "context_manifests",
            "result_refs",
            "evidence_refs",
            "answer_packages",
            "analysis_assets",
            "investigation_artifacts",
            "memory_items",
            "memory_proposals",
            "audit_events",
        }

        for table in required_tables:
            with self.subTest(table=table):
                self.assertIn(f"waje_runtime.{table}", CONVERSATION_SCHEMA_SQL)
        self.assertIn("refresh_rule", CONVERSATION_SCHEMA_SQL)
        self.assertIn("revocation_path", CONVERSATION_SCHEMA_SQL)

    def test_store_writes_audit_events_for_state_changes(self):
        connection = FakeConnection()
        store = PostgresConversationStore(connection)

        thread = store.create_thread("thread-pg", owner_id="analyst-1")
        topic = store.create_topic(thread.thread_id, title="Q2 vs Q1", summary="Q2 变化")
        store.set_current_topic(thread.thread_id, topic.topic_id)
        store.add_turn(thread.thread_id, {"turn_id": "turn-1", "intent": "new_topic"})
        store.add_result_ref(
            topic.topic_id,
            result_ref="result-1",
            snapshot_id="2026H1",
            contract_version="contracts-v1",
            permission_scope="analyst",
            semantic_scope="q2_vs_q1_paid_amount",
        )
        store.add_memory_proposal(
            MemoryProposal(
                proposal_id="proposal-1",
                thread_id=thread.thread_id,
                text="默认单独看 WajeSpecial",
                source_ref="turn-1",
                owner_scope="org-default",
                visibility="analyst",
            )
        )

        executed_sql = "\n".join(statement for statement, _params in connection.statements)
        self.assertIn("waje_runtime.investigation_threads", executed_sql)
        self.assertIn("waje_runtime.conversation_topics", executed_sql)
        self.assertIn("waje_runtime.conversation_turns", executed_sql)
        self.assertIn("waje_runtime.result_refs", executed_sql)
        self.assertIn("waje_runtime.memory_proposals", executed_sql)
        self.assertGreaterEqual(executed_sql.count("waje_runtime.audit_events"), 5)
        self.assertGreaterEqual(connection.commits, 5)

    def test_postgres_store_records_analysis_assets(self):
        connection = FakeConnection()
        store = PostgresConversationStore(connection)

        store.save_analysis_assets(
            "thread-pg",
            "topic-pg",
            [{"asset_type": "compiler_runtime_plan", "status": "usable", "payload": {"query_intents": ["dimension_scan_reuse"]}}],
        )

        executed_sql = "\n".join(statement for statement, _params in connection.statements)
        self.assertIn("waje_runtime.analysis_assets", executed_sql)
        self.assertGreaterEqual(executed_sql.count("waje_runtime.audit_events"), 1)


class FakeConnection:
    def __init__(self):
        self.statements = []
        self.commits = 0

    def execute(self, statement, params=None):
        self.statements.append((statement, params or {}))
        return FakeCursor()

    def commit(self):
        self.commits += 1


class FakeCursor:
    def fetchone(self):
        return None

    def fetchall(self):
        return []


if __name__ == "__main__":
    unittest.main()
