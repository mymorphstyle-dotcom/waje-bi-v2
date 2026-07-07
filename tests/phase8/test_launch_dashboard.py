from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class LaunchDashboardTest(unittest.TestCase):
    def test_launch_dashboard_route_locates_run_failure_categories(self):
        store = (ROOT / "app" / "api" / "_conversationStore.ts").read_text(encoding="utf-8")
        route = (ROOT / "app" / "api" / "runs" / "launch-dashboard" / "route.ts").read_text(encoding="utf-8")

        self.assertIn("launchDashboard", store)
        for table in ("waje_runtime.analysis_runs", "waje_runtime.answer_packages", "waje_runtime.run_nodes", "waje_runtime.audit_events"):
            with self.subTest(table=table):
                self.assertIn(table, store)

        for category in ("slow_runs", "failed_runs", "degraded_runs", "blocked_runs", "verifier_failed_runs"):
            with self.subTest(category=category):
                self.assertIn(category, store)

        self.assertIn("slowMs", route)
        self.assertIn("launchDashboard", route)
        self.assertNotIn("waje_runtime.", route)


if __name__ == "__main__":
    unittest.main()
