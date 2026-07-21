from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class HealthChecksTest(unittest.TestCase):
    def test_health_route_checks_launch_dependencies_without_starting_analysis(self):
        route_path = ROOT / "app" / "api" / "health" / "route.ts"
        self.assertTrue(route_path.exists())

        route = route_path.read_text(encoding="utf-8")
        runtime = (ROOT / "app" / "api" / "_pythonRuntime.ts").read_text(
            encoding="utf-8"
        )
        for check in (
            "frontend_gateway",
            "python_bi_agent_core",
            "postgres_runtime_store",
            "llm_access",
            "clickhouse_access",
            "langgraph_adapter",
        ):
            with self.subTest(check=check):
                self.assertIn(check, route)

        self.assertIn("SELECT 1", route)
        self.assertIn("WAJE_LLM_MODEL", route)
        self.assertIn("WAJE_LLM_API_KEY", route)
        self.assertIn("ClickHouseRuntime.from_env", route)
        self.assertIn("build_single_authority_graph", route)
        self.assertNotIn("build_pattern_graph", route)
        self.assertNotIn("run_pattern_workflow", route)
        self.assertNotIn("runAgentCore", route)
        self.assertIn("wajePythonInvocation", route)
        self.assertIn('"uv"', runtime)
        self.assertIn('"3.12"', runtime)
        self.assertIn('"--with-requirements"', runtime)
        self.assertIn('"requirements.txt"', runtime)
        self.assertNotIn('spawn("python3"', route)


if __name__ == "__main__":
    unittest.main()
