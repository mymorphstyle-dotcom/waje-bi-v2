from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class WorkbenchAuditVisibilityTest(unittest.TestCase):
    def test_ordinary_workbench_hides_raw_audit_until_debug_mode(self):
        contracts = (ROOT / "app" / "agent-run-workbench" / "contracts.ts").read_text(encoding="utf-8")
        workbench = (
            ROOT / "app" / "agent-run-workbench" / "AgentRunWorkbench.tsx"
        ).read_text(encoding="utf-8")
        canvas = (
            ROOT / "app" / "agent-run-workbench" / "WorkflowCanvasModal.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn("debugAudit?: boolean", contracts)
        self.assertIn("debugAudit={Boolean(active.processSummary.debugAudit)}", workbench)
        self.assertIn("debugAudit?: boolean", canvas)
        self.assertIn("debugAudit: boolean", canvas)
        self.assertIn("debugAudit && node.audit", workbench)
        self.assertIn("debugAudit && data.traceNode?.audit", canvas)


if __name__ == "__main__":
    unittest.main()
