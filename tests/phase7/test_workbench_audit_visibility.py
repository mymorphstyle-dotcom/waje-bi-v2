from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class WorkbenchAuditVisibilityTest(unittest.TestCase):
    def test_customer_read_model_does_not_send_internal_audit_to_the_browser(self):
        contracts = (ROOT / "app" / "agent-run-workbench" / "contracts.ts").read_text(
            encoding="utf-8"
        )
        workbench = (
            ROOT / "app" / "agent-run-workbench" / "AgentRunWorkbench.tsx"
        ).read_text(encoding="utf-8")
        canvas = (
            ROOT / "app" / "agent-run-workbench" / "WorkflowCanvasModal.tsx"
        ).read_text(encoding="utf-8")
        projection = (ROOT / "app" / "api" / "_customerRunProjection.ts").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("audit?: unknown", contracts)
        self.assertNotIn("debugAudit", contracts + workbench + canvas)
        self.assertNotIn("debugStage", contracts)
        self.assertNotIn("sourceRef", contracts)
        self.assertNotIn("traceNode.audit", canvas)
        self.assertNotIn("audit:", projection)


if __name__ == "__main__":
    unittest.main()
