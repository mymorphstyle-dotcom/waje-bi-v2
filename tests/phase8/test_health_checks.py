from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class HealthChecksTest(unittest.TestCase):
    def test_public_liveness_is_constant_cost_and_readiness_is_bounded(self):
        route_path = ROOT / "app" / "api" / "health" / "route.ts"
        self.assertTrue(route_path.exists())

        route = route_path.read_text(encoding="utf-8")
        self.assertIn('mode === "liveness"', route)
        self.assertIn('mode !== "readiness"', route)
        self.assertIn("WAJE_HEALTH_READINESS_TOKEN", route)
        self.assertIn("timingSafeEqual", route)
        self.assertIn("POSTGRES_TIMEOUT_MS", route)
        self.assertIn("WAJE_PYTHON_EXECUTABLE", route)
        self.assertIn("constants.X_OK", route)
        self.assertIn("postgres_runtime_store", route)
        self.assertIn("runtime_configuration", route)
        self.assertIn("SELECT 1", route)
        self.assertIn("WAJE_LLM_PROVIDER", route)
        self.assertIn("WAJE_LLM_BASE_URL", route)
        self.assertIn("WAJE_LLM_MODEL", route)
        self.assertIn("WAJE_LLM_API_KEY", route)
        self.assertIn("DEEPSEEK_API_KEY", route)
        self.assertNotIn("OPENAI_API_KEY", route)
        self.assertNotIn("OPENAI_API_KEY", route)
        self.assertNotIn("spawn", route)
        self.assertNotIn("stderr", route)
        self.assertNotIn("ClickHouseRuntime.from_env", route)
        self.assertNotIn("build_single_authority_graph", route)


if __name__ == "__main__":
    unittest.main()
