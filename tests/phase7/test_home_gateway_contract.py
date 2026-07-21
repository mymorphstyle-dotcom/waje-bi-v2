from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class HomeGatewayContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = (ROOT / "app" / "page.tsx").read_text(encoding="utf-8")
        cls.contract = (
            ROOT / "app" / "api" / "_customerAnalysisContract.ts"
        ).read_text(encoding="utf-8")
        cls.styles = (ROOT / "app" / "globals.css").read_text(encoding="utf-8")
        cls.prompt_input = (
            ROOT / "components" / "ai-elements" / "prompt-input.tsx"
        ).read_text(encoding="utf-8")

    def test_home_consumes_only_customer_snapshot_contract(self):
        self.assertIn("parseCustomerAnalysisSnapshot", self.page)
        self.assertIn('fetch("/api/threads"', self.page)
        self.assertIn("customer_state_changed", self.page)
        self.assertIn("snapshot?.transport.eventsUrl", self.page)
        self.assertNotIn("GatewayEvent", self.page)
        self.assertNotIn("clarification_state_saved", self.page)
        self.assertNotIn("node_process", self.page)
        self.assertNotIn("accepted_blocks", self.page)

    def test_home_uses_one_discriminated_customer_state(self):
        for status in (
            "idle",
            "working",
            "needs_input",
            "completed",
            "completed_with_limits",
            "failed",
        ):
            self.assertIn(f'| "{status}"', self.contract)
        self.assertIn("type CustomerAnalysisState =", self.contract)
        self.assertIn("CUSTOMER_PHASES", self.contract)
        self.assertNotIn("runStatus", self.page)
        self.assertNotIn("agentStatus", self.page)
        self.assertNotIn("publicationStatus", self.page)
        self.assertNotIn("deliveryStatus", self.page)

    def test_one_time_actions_keep_a_versioned_idempotency_identity(self):
        self.assertIn('PENDING_OPERATION_PREFIX = "waje-pending-operation:v2:"', self.page)
        self.assertIn('INITIAL_MESSAGE_SCOPE = "message:new"', self.page)
        self.assertIn("PENDING_OPERATION_TTL_MS", self.page)
        self.assertIn("const existing = loadPendingOperation(scope)", self.page)
        self.assertIn('"Idempotency-Key": currentOperation.operationId', self.page)
        self.assertIn("requestIdentity: currentOperation.operationId", self.page)
        self.assertIn("activeExecutionIdsRef", self.page)
        self.assertIn("acceptedOperationIds.includes(currentOperation.operationId)", self.page)
        self.assertNotIn('"Idempotency-Key": crypto.randomUUID()', self.page)

    def test_home_does_not_render_transport_handles_or_internal_errors(self):
        rendered_terms = (
            "Gateway →",
            "ConversationAgentCore",
            "Authority Publication",
            "Run：",
            "Agent：",
            "dispatchId",
            "threadId ||",
            "clarification_source_not_waiting",
            "gateway_http_",
        )
        for term in rendered_terms:
            with self.subTest(term=term):
                self.assertNotIn(term, self.page)
        self.assertIn("technicalDetailRef", self.page)
        self.assertNotIn("{error.technicalDetailRef}", self.page)

    def test_accessibility_and_mobile_navigation_remain_available(self):
        self.assertIn('aria-label="分析历史"', self.page)
        self.assertIn('aria-label="分析进展"', self.page)
        self.assertIn('role="alert"', self.page)
        self.assertIn("inputRequestRef.current?.focus()", self.page)
        self.assertIn('event.key !== "Enter"', self.prompt_input)
        self.assertIn("event.shiftKey", self.prompt_input)
        self.assertIn("button:focus-visible", self.styles)
        self.assertIn("prefers-reduced-motion: reduce", self.styles)
        mobile = self.styles.split("@media (max-width: 760px)", 1)[1]
        self.assertIn(".thread-sidebar nav.open", mobile)
        self.assertIn("transform: translateX(-104%)", mobile)
        self.assertIn(".history-backdrop", mobile)

    def test_customer_page_excludes_demo_prefill_and_audit_navigation(self):
        self.assertNotIn("DEFAULT_QUESTION", self.page)
        self.assertNotIn("agent-run-workbench", self.page)
        self.assertNotIn("运行审计", self.page)
        self.assertIn('useState("")', self.page)

    def test_home_uses_ai_elements_as_a_codex_style_conversation(self):
        self.assertIn('from "@/components/ai-elements/conversation"', self.page)
        self.assertIn('from "@/components/ai-elements/message"', self.page)
        self.assertIn('from "@/components/ai-elements/prompt-input"', self.page)
        self.assertIn("ProgressTimeline", self.page)
        self.assertNotIn("ProgressPanel", self.page)
        self.assertNotIn("customer-milestones", self.page)
        self.assertNotIn("工作流画布", self.page)
        self.assertIn('initial={answerComplete ? false : "smooth"}', self.page)

    def test_customer_answer_does_not_render_raw_material_fact_names(self):
        self.assertNotIn("state.answer.facts", self.page)
        self.assertNotIn("answer-facts", self.styles)
        self.assertNotIn('"facts",', self.contract)

    def test_home_contains_no_demo_result_or_fake_progress(self):
        for forbidden in (
            "/api/langgraph",
            "LangGraph mock",
            "monthEvents",
            "monthAnswer",
            "serverWait",
            "playFlow",
            "+18.9%",
            "25/29",
            "54%",
            "预计完成",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.page)


if __name__ == "__main__":
    unittest.main()
