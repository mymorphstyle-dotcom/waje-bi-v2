from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
ROUTE = ROOT / "app" / "api" / "runs" / "[runId]" / "clarifications" / "route.ts"
HELPER = ROOT / "app" / "api" / "_agentCore.ts"
STORE = ROOT / "app" / "api" / "_conversationStore.ts"


class GatewayClarificationsTest(unittest.TestCase):
    def test_clarification_route_records_resolution_and_creates_initial_attempt(self):
        route = ROUTE.read_text(encoding="utf-8")
        store = STORE.read_text(encoding="utf-8")

        self.assertIn("runAgentCore", route)
        self.assertIn("claimClarificationResolutionAttempt", route)
        self.assertIn("claim.attemptRunId", route)
        self.assertIn("sourceRunId: runId", route)
        self.assertIn("eventsUrl: `/api/runs/${claim.attemptRunId}/events`", route)
        self.assertIn("clarification_resolutions", store)
        self.assertIn("clarification_execution_attempts", store)
        self.assertIn("clarification_answer_recorded", store)

    def test_clarification_spawn_failure_terminalizes_attempt_queue(self):
        route = ROUTE.read_text(encoding="utf-8")

        self.assertIn("failOwnedRunDispatch", route)
        self.assertIn("if (agentCore.error)", route)
        self.assertIn("failureReason: agentCore.error", route)

    def test_clarification_route_forwards_full_payload_and_waits_for_result(self):
        route = ROUTE.read_text(encoding="utf-8")
        helper = HELPER.read_text(encoding="utf-8")

        self.assertIn("sourceRunId: claim.sourceRunId", route)
        self.assertIn("resolutionId: claim.resolutionId", route)
        self.assertIn("attemptRunId: claim.attemptRunId", route)
        self.assertIn("retryAttempt: false", route)
        self.assertRegex(
            route,
            r"runAgentCore\([^;]*clarification:\s*clarificationPayload[^;]*forceInline:\s*true[^;]*\)",
        )
        self.assertIn("answerPackagePreview = visibleResult.answer_package ?? null", route)
        self.assertIn("projectAgentCoreForCustomer", route)
        self.assertIn("agentCore: visibleAgentCore", route)
        self.assertNotIn("...agentCore", route)
        self.assertNotIn("...resumed", route)

        self.assertRegex(helper, r"clarification\?:\s*\{")
        self.assertIn("sourceRunId: string", helper)
        self.assertIn("resolutionId: string", helper)
        self.assertIn("attemptRunId: string", helper)
        self.assertIn("selectedOptionId: string | null", helper)
        self.assertIn('source: "user"', helper)
        self.assertIn('"--clarification"', helper)
        self.assertIn("JSON.stringify(options.clarification)", helper)

    def test_clarification_attempt_uses_fixed_customer_projection(self):
        route = ROUTE.read_text(encoding="utf-8")
        store = STORE.read_text(encoding="utf-8")

        self.assertIn("projectAgentCoreForCustomer", route)
        self.assertIn("projectAnswerPackageForCustomer", store)
        self.assertIn(
            'new Set(["business_summary", "aggregate_evidence", "diagnostic_detail"])',
            store,
        )

    def test_customer_projection_keeps_diagnostics_and_hides_internal_audit(self):
        store = STORE.read_text(encoding="utf-8")

        self.assertIn('"diagnostic_detail"', store)
        self.assertIn("projectAgentCoreForCustomer", store)
        self.assertIn("projectAnswerPackageForCustomer", store)
        start = store.index("export function projectAnswerPackageForCustomer")
        end = store.index("export function projectAgentCoreForCustomer", start)
        projection = store[start:end]
        self.assertNotIn("return answerPackage", projection)
        self.assertNotIn("admin_audit: answerPackage.admin_audit", projection)


if __name__ == "__main__":
    unittest.main()
