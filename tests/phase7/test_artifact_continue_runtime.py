import unittest

from bi_agent.conversation.postgres_store import CONVERSATION_SCHEMA_SQL, PostgresConversationStore
from bi_agent.conversation.runtime import ConversationRuntime
from bi_agent.conversation.store import InMemoryConversationStore


class ArtifactContinueRuntimeTest(unittest.TestCase):
    def test_artifact_follow_up_keeps_matching_snapshot_context_without_claim_authority(self):
        store = InMemoryConversationStore()
        runtime = ConversationRuntime(store)
        store.create_thread("thread-artifact", owner_id="user-1")
        topic = store.create_topic("thread-artifact", title="Q2 vs Q1", summary="Q2/Q1 已验证结果")
        store.set_current_topic("thread-artifact", topic.topic_id)
        store.add_result_ref(
            topic.topic_id,
            result_ref="result:q2-q1",
            snapshot_id="2026H1",
            contract_version="contracts-v1",
            semantic_scope="q2_vs_q1",
        )
        store.add_artifact(
            artifact_id="artifact:q2-q1",
            topic_id=topic.topic_id,
            follow_up_context="Q2/Q1 的 Answer Package，可继续看渠道。",
            snapshot_id="2026H1",
        )

        result = runtime.handle_message(
            "thread-artifact",
            "基于这个结果继续看渠道。",
            current_snapshot="2026H1",
        )

        artifact_items = [
            item for item in result.context_manifest.items if item.source_type == "artifact"
        ]
        self.assertEqual(len(artifact_items), 1)
        self.assertTrue(artifact_items[0].can_support_claims)
        self.assertFalse(result.context_manifest.can_support_claims)
        self.assertEqual(store.get_thread("thread-artifact").owner_id, "user-1")

    def test_artifact_follow_up_does_not_support_claims_when_rerun_required(self):
        store = InMemoryConversationStore()
        runtime = ConversationRuntime(store)
        store.create_thread("thread-artifact", owner_id="user-1")
        topic = store.create_topic("thread-artifact", title="Q2 vs Q1", summary="Q2/Q1 已验证结果")
        store.set_current_topic("thread-artifact", topic.topic_id)
        store.add_result_ref(
            topic.topic_id,
            result_ref="result:q2-q1",
            snapshot_id="2026H1",
            contract_version="contracts-v1",
            semantic_scope="q2_vs_q1",
        )
        store.add_artifact(
            artifact_id="artifact:q2-q1",
            topic_id=topic.topic_id,
            follow_up_context="Q2/Q1 的 Answer Package，可继续看渠道。",
            snapshot_id="2026H1",
        )

        result = runtime.handle_message(
            "thread-artifact",
            "基于这个结果，换成日均再看一遍。",
            current_snapshot="2026H1",
        )

        self.assertEqual(result.reuse_decisions[0].decision, "rerun")
        artifact_items = [
            item for item in result.context_manifest.items if item.source_type == "artifact"
        ]
        self.assertEqual(len(artifact_items), 1)
        self.assertTrue(artifact_items[0].can_support_claims)
        self.assertFalse(result.context_manifest.can_support_claims)

    def test_postgres_store_persists_and_reads_latest_artifact_for_topic(self):
        connection = FakeArtifactConnection()
        store = PostgresConversationStore(connection)

        store.add_artifact(
            artifact_id="artifact:q2-q1",
            topic_id="topic-q2-q1",
            follow_up_context="Q2/Q1 的 Answer Package。",
            snapshot_id="2026H1",
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
