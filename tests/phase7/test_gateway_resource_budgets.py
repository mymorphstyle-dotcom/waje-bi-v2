from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class GatewayResourceBudgetContractTest(unittest.TestCase):
    def test_all_customer_json_writes_use_bounded_reader(self):
        routes = [
            ROOT / "app" / "api" / "threads" / "route.ts",
            ROOT / "app" / "api" / "threads" / "[threadId]" / "messages" / "route.ts",
            ROOT / "app" / "api" / "runs" / "[runId]" / "clarifications" / "route.ts",
        ]
        for path in routes:
            with self.subTest(path=path):
                source = path.read_text(encoding="utf-8")
                self.assertIn("readBoundedCustomerJson", source)
                self.assertNotIn("request.json()", source)

    def test_message_budget_is_enforced_at_gateway_and_python_boundary(self):
        budget = (ROOT / "app" / "api" / "_requestBudget.ts").read_text(
            encoding="utf-8"
        )
        entry = (
            ROOT / "bi_agent" / "runtime" / "general_agent_entry.py"
        ).read_text(encoding="utf-8")
        store = (ROOT / "app" / "api" / "_conversationStore.ts").read_text(
            encoding="utf-8"
        )
        self.assertIn("CUSTOMER_JSON_BODY_MAX_BYTES", budget)
        self.assertIn("CUSTOMER_MESSAGE_MAX_BYTES", budget)
        self.assertIn("Buffer.byteLength", budget)
        self.assertIn("GENERAL_AGENT_COMMAND_MAX_BYTES", entry)
        self.assertIn("GENERAL_AGENT_MESSAGE_MAX_BYTES", entry)
        self.assertIn('code: "request_too_large"', store)
        self.assertIn("httpStatus: 413", store)

    def test_customer_routes_use_safe_error_projection(self):
        routes = [
            ROOT / "app" / "api" / "memory-proposals" / "[proposalId]" / "accept" / "route.ts",
            ROOT / "app" / "api" / "memory-proposals" / "[proposalId]" / "reject" / "route.ts",
            ROOT / "app" / "api" / "runs" / "[runId]" / "rerun-comparability" / "route.ts",
            ROOT / "app" / "api" / "runs" / "[runId]" / "audit-trace" / "route.ts",
        ]
        for path in routes:
            with self.subTest(path=path):
                source = path.read_text(encoding="utf-8")
                self.assertIn("customerJsonError", source)
                self.assertNotIn("jsonError(error)", source)

    def test_customer_snapshot_is_paged_without_publication_duplication(self):
        store = (ROOT / "app" / "api" / "_conversationStore.ts").read_text(
            encoding="utf-8"
        )
        contract = (
            ROOT / "app" / "api" / "_customerAnalysisContract.ts"
        ).read_text(encoding="utf-8")
        self.assertIn("CUSTOMER_MESSAGE_PAGE_SIZE", store)
        self.assertIn("CUSTOMER_MESSAGE_PAGE_SIZE + 1", store)
        self.assertIn("messageHistory", store)
        self.assertIn("messageHistory", contract)
        self.assertNotIn("publicationHistoryResult", store)
        self.assertNotIn("historicalAnswers", store)

    def test_dispatch_lease_configuration_fails_closed(self):
        store = (ROOT / "app" / "api" / "_conversationStore.ts").read_text(
            encoding="utf-8"
        )
        self.assertIn('throw gatewayError("run_dispatch_lease_configuration_invalid")', store)
        self.assertIn("Number.isSafeInteger(configured)", store)
        self.assertNotIn("configured > 0 ? configured : 30000", store)


if __name__ == "__main__":
    unittest.main()
