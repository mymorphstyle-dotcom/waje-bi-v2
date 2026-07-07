import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class GatewayStoreBoundaryTest(unittest.TestCase):
    def test_gateway_store_requires_postgres_in_production(self):
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        source = (ROOT / "app" / "api" / "_conversationStore.ts").read_text(encoding="utf-8")

        self.assertIn("pg", package["dependencies"])
        self.assertIn("WAJE_RUNTIME_DATABASE_URL", source)
        self.assertIn("NODE_ENV", source)
        self.assertIn("production", source)
        self.assertIn("throw new Error", source)
        self.assertIn("waje_runtime.investigation_threads", source)


if __name__ == "__main__":
    unittest.main()
