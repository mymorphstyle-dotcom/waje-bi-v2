import unittest

from bi_agent.runtime.answer_package import build_answer_package
from bi_agent.runtime.capability_harness import execute_capability
from bi_agent.runtime.capability_models import BudgetState
from tests.phase4.test_market_window_evidence import (
    _market_context,
    _market_request,
)


class BudgetDegradationTest(unittest.TestCase):
    def test_hard_budget_skip_enters_answer_package_limitations(self):
        context = _market_context()
        request = _market_request(
            context,
            budget_state=BudgetState("research", 100, 50, 100),
        )

        envelope = execute_capability(request)

        self.assertEqual(envelope.wording_limit, "blocked")
        self.assertIn("capability_budget_exhausted", envelope.limitations)
        self.assertIn(
            "capability_budget_exhausted",
            envelope.disabled_degraded_blocked_path_refs,
        )

        package = build_answer_package(
            run_id="budget-run",
            draft_claims=[],
            evidence=[envelope.to_dict()],
            checkpoint_events=[],
            proposed_graph=[],
            accepted_graph=[],
            rejected_or_degraded_mutations=[],
            validator_results=[],
            sql_text="",
            sql_hash="",
            artifact_audit={},
        )
        summary = package["sections"][0]["payload"]
        self.assertIn("capability_budget_exhausted", summary["limitations"])

    def test_authoritative_row_and_timeout_limits_return_blocked_evidence(self):
        context = _market_context()
        cases = (
            ({"row_budget": 1}, "row_budget_exceeded"),
            ({"timeout_exceeded": True}, "capability_timeout"),
        )
        for params, limitation in cases:
            with self.subTest(limitation=limitation):
                envelope = execute_capability(
                    _market_request(context, params=params)
                )
                self.assertEqual(envelope.wording_limit, "blocked")
                self.assertIn(limitation, envelope.limitations)


if __name__ == "__main__":
    unittest.main()
