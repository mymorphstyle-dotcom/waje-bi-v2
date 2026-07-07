import unittest

from bi_agent.conversation.postgres_store import CONVERSATION_SCHEMA_SQL, PostgresConversationStore
from bi_agent.conversation.runtime import ConversationRuntime
from bi_agent.conversation.store import InMemoryConversationStore


class ArtifactContinueRuntimeTest(unittest.TestCase):
    def test_artifact_follow_up_supports_claims_only_when_permission_and_snapshot_match(self):
        store = InMemoryConversationStore()
        runtime = ConversationRuntime(store)
        store.create_thread("thread-artifact", owner_id="analyst-1")
        topic = store.create_topic("thread-artifact", title="Q2 vs Q1", summary="Q2/Q1 已验证结果")
        store.set_current_topic("thread-artifact", topic.topic_id)
        store.add_result_ref(
            topic.topic_id,
            result_ref="result:q2-q1",
            snapshot_id="2026H1",
            contract_version="contracts-v1",
            permission_scope="business_reader",
            semantic_scope="q2_vs_q1",
        )
        store.add_artifact(
            artifact_id="artifact:q2-q1",
            topic_id=topic.topic_id,
            follow_up_context="Q2/Q1 的 Answer Package，可继续看渠道。",
            snapshot_id="2026H1",
            permission_scope="business_reader",
        )

        result = runtime.handle_message(
            "thread-artifact",
            "基于这个结果继续看渠道。",
            role="business_reader",
            current_snapshot="2026H1",
        )

        artifact_items = [
            item for item in result.context_manifest.items if item.source_type == "artifact"
        ]
        self.assertEqual(len(artifact_items), 1)
        self.assertTrue(artifact_items[0].can_support_claims)
        self.assertTrue(result.context_manifest.can_support_claims)

    def test_artifact_follow_up_becomes_context_only_when_permission_does_not_match(self):
        store = InMemoryConversationStore()
        runtime = ConversationRuntime(store)
        store.create_thread("thread-artifact", owner_id="analyst-1")
        topic = store.create_topic("thread-artifact", title="Q2 vs Q1", summary="Q2/Q1 已验证结果")
        store.set_current_topic("thread-artifact", topic.topic_id)
        store.add_result_ref(
            topic.topic_id,
            result_ref="result:q2-q1",
            snapshot_id="2026H1",
            contract_version="contracts-v1",
            permission_scope="analyst",
            semantic_scope="q2_vs_q1",
        )
        store.add_artifact(
            artifact_id="artifact:q2-q1",
            topic_id=topic.topic_id,
            follow_up_context="Q2/Q1 的 analyst Answer Package。",
            snapshot_id="2026H1",
            permission_scope="analyst",
        )

        result = runtime.handle_message(
            "thread-artifact",
            "基于这个结果继续看渠道。",
            role="business_reader",
            current_snapshot="2026H1",
        )

        artifact_items = [
            item for item in result.context_manifest.items if item.source_type == "artifact"
        ]
        self.assertEqual(len(artifact_items), 1)
        self.assertFalse(artifact_items[0].can_support_claims)
        self.assertFalse(result.context_manifest.can_support_claims)
        self.assertEqual(result.reuse_decisions[0].decision, "blocked")

    def test_postgres_store_persists_and_reads_latest_artifact_for_topic(self):
        connection = FakeArtifactConnection()
        store = PostgresConversationStore(connection)

        store.add_artifact(
            artifact_id="artifact:q2-q1",
            topic_id="topic-q2-q1",
            follow_up_context="Q2/Q1 的 Answer Package。",
            snapshot_id="2026H1",
            permission_scope="analyst",
        )
        artifact = store.latest_artifact_for_topic("topic-q2-q1")

        executed_sql = "\n".join(statement for statement, _params in connection.statements)
        self.assertIn("waje_runtime.investigation_artifacts", CONVERSATION_SCHEMA_SQL)
        self.assertIn("waje_runtime.investigation_artifacts", executed_sql)
        self.assertIsNotNone(artifact)
        self.assertEqual(artifact.artifact_id, "artifact:q2-q1")
        self.assertEqual(artifact.topic_id, "topic-q2-q1")
        self.assertEqual(artifact.snapshot_id, "2026H1")


class FakeArtifactConnection:
    def __init__(self):
        self.statements = []
        self.commits = 0

    def execute(self, statement, params=None):
        self.statements.append((statement, params or {}))
        if "SELECT artifact_id" in statement:
            return FakeCursor(
                {
                    "artifact_id": "artifact:q2-q1",
                    "topic_id": "topic-q2-q1",
                    "follow_up_context": "Q2/Q1 的 Answer Package。",
                    "snapshot_id": "2026H1",
                    "permission_scope": "analyst",
                }
            )
        return FakeCursor(None)

    def commit(self):
        self.commits += 1


class FakeCursor:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row

    def fetchall(self):
        return [] if self.row is None else [self.row]


if __name__ == "__main__":
    unittest.main()
