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


if __name__ == "__main__":
    unittest.main()
