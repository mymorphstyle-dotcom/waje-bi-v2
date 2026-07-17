import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class GatewayStoreBoundaryTest(unittest.TestCase):
    def test_gateway_store_requires_postgres_outside_explicit_unit_tests(self):
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        source = (ROOT / "app" / "api" / "_conversationStore.ts").read_text(encoding="utf-8")

        self.assertIn("pg", package["dependencies"])
        self.assertIn("WAJE_RUNTIME_DATABASE_URL", source)
        self.assertIn("NODE_ENV", source)
        self.assertIn("WAJE_GATEWAY_UNIT_TEST_STORE", source)
        self.assertIn('process.env.NODE_ENV === "test"', source)
        self.assertNotIn('process.env.NODE_ENV === "production"', source)
        self.assertIn("throw new Error", source)
        self.assertIn("waje_runtime.investigation_threads", source)

    def test_memory_proposal_persistence_has_no_user_visibility_role(self):
        source = (ROOT / "app" / "api" / "_conversationStore.ts").read_text(encoding="utf-8")
        start = source.index("INSERT INTO waje_runtime.memory_proposals")
        end = source.index("await audit", start)
        insert = source[start:end]

        self.assertIn("proposal_id, thread_id, text, source_ref, owner_id, status", insert)
        self.assertNotIn("visibility", insert)
        self.assertNotIn('"analyst"', insert)


if __name__ == "__main__":
    unittest.main()
