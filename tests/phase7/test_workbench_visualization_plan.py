from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class WorkbenchVisualizationPlanTest(unittest.TestCase):
    def test_workbench_uses_answer_package_visualization_plan(self):
        contracts = (ROOT / "app" / "agent-run-workbench" / "contracts.ts").read_text(encoding="utf-8")
        replay_adapter = (ROOT / "app" / "api" / "replays" / "route.ts").read_text(encoding="utf-8")
        workbench = (
            ROOT / "app" / "agent-run-workbench" / "AgentRunWorkbench.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn("TraceVisualBlock", contracts)
        self.assertIn("visualBlocks", contracts)
        self.assertIn("visualBlocksFromPlan", replay_adapter)
        self.assertIn("summary.visualization_plan", replay_adapter)
        self.assertIn("可视化计划", workbench)
        self.assertIn("answer.visualBlocks", workbench)


if __name__ == "__main__":
    unittest.main()
