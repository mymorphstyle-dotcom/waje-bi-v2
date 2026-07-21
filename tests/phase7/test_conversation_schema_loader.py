from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class ConversationSchemaLoaderTest(unittest.TestCase):
    def test_loader_applies_conversation_schema_with_existing_postgres_env(self):
        loader = (
            ROOT / "tools" / "runtime" / "load-conversation-runtime-schema.rb"
        ).read_text(encoding="utf-8")

        self.assertIn("conversation-runtime.sql", loader)
        self.assertIn("WAJE_PG_CONTAINER", loader)
        self.assertIn("WAJE_PG_DB", loader)
        self.assertIn("psql", loader)

    def test_repeated_query_execution_can_reference_the_same_immutable_rows_record(
        self,
    ):
        schema = (ROOT / "tools" / "runtime" / "conversation-runtime.sql").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "DROP INDEX IF EXISTS waje_runtime.idx_query_execution_authority_rows_ref",
            schema,
        )
        self.assertNotIn(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_query_execution_authority_rows_ref",
            schema,
        )

    def test_decision_ledger_replaces_clarification_attempt_schema(self):
        schema = (ROOT / "tools" / "runtime" / "conversation-runtime.sql").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "CREATE TABLE IF NOT EXISTS waje_runtime.decision_records", schema
        )
        self.assertIn("option_id text", schema)
        self.assertIn("idx_decision_records_option_idempotency", schema)
        self.assertIn("intent_revision_id, slot_id, option_id", schema)
        self.assertNotIn("waje_runtime.clarification_resolutions", schema)
        self.assertNotIn("waje_runtime.clarification_execution_attempts", schema)
        self.assertNotIn("'clarification_retry'", schema)
        self.assertNotIn("'clarification_resume'", schema)
        self.assertNotIn("'artifact_continue'", schema)
        self.assertIn("ALTER COLUMN message_id SET NOT NULL", schema)
        self.assertIn("'thread_message'", schema)
        self.assertIn("'clarification_resolution'", schema)


if __name__ == "__main__":
    unittest.main()
