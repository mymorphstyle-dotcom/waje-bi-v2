from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class WorkbenchProcessRenderingTest(unittest.TestCase):
    def test_workbench_maps_question_tool_and_evidence_summary_to_business_stages(self):
        workbench = (
            ROOT / "app" / "agent-run-workbench" / "AgentRunWorkbench.tsx"
        ).read_text(encoding="utf-8")
        canvas = (
            ROOT / "app" / "agent-run-workbench" / "WorkflowCanvasModal.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn('"question_tool"', workbench)
        self.assertIn('"question_tool"', canvas)
        self.assertIn('"reduce_evidence"', canvas)
        self.assertIn("等待用户确认", canvas)


if __name__ == "__main__":
    unittest.main()
