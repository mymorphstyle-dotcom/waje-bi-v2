from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class ConversationSchemaLoaderTest(unittest.TestCase):
    def test_loader_applies_conversation_schema_with_existing_postgres_env(self):
        loader = (ROOT / "tools" / "runtime" / "load-conversation-runtime-schema.rb").read_text(
            encoding="utf-8"
        )

        self.assertIn("conversation-runtime.sql", loader)
        self.assertIn("WAJE_PG_CONTAINER", loader)
        self.assertIn("WAJE_PG_DB", loader)
        self.assertIn("psql", loader)

    def test_repeated_query_execution_can_reference_the_same_immutable_rows_record(self):
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

    def test_clarification_resolution_and_attempt_schema_separate_choice_from_execution(self):
        schema = (ROOT / "tools" / "runtime" / "conversation-runtime.sql").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "CREATE TABLE IF NOT EXISTS waje_runtime.clarification_resolutions",
            schema,
        )
        self.assertIn("source_run_id text NOT NULL UNIQUE", schema)
        self.assertIn("accepted_choice jsonb NOT NULL", schema)
        self.assertIn("status = 'accepted'", schema)
        self.assertIn(
            "CREATE TABLE IF NOT EXISTS waje_runtime.clarification_execution_attempts",
            schema,
        )
        self.assertIn("attempt_run_id text PRIMARY KEY", schema)
        self.assertIn("previous_attempt_run_id text", schema)
        self.assertIn("UNIQUE(resolution_id, attempt_number)", schema)
        self.assertIn("'clarification_retry'", schema)
        self.assertIn("ALTER COLUMN message_id DROP NOT NULL", schema)


if __name__ == "__main__":
    unittest.main()
