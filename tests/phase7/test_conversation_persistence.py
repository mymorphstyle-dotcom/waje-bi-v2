from pathlib import Path
import unittest

from bi_agent.conversation.models import MemoryProposal
from bi_agent.conversation.postgres_store import CONVERSATION_SCHEMA_SQL, PostgresConversationStore


ROOT = Path(__file__).resolve().parents[2]


class ConversationPersistenceTest(unittest.TestCase):
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
            "investigation_artifacts",
            "memory_items",
            "memory_proposals",
            "audit_events",
        }

        for table in required_tables:
            with self.subTest(table=table):
                self.assertIn(f"waje_runtime.{table}", CONVERSATION_SCHEMA_SQL)

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
