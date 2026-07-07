import unittest

from bi_agent.runtime.answer_package import build_answer_package
from bi_agent.runtime.capability_harness import execute_capability
from bi_agent.runtime.capability_models import BudgetState, CapabilityRequest


class BudgetDegradationTest(unittest.TestCase):
    def test_hard_budget_skip_enters_answer_package_limitations(self):
        envelope = execute_capability(_request(BudgetState("research", 100, 50, 100)))

        self.assertEqual(envelope.wording_limit, "blocked")
        self.assertIn("capability_budget_exhausted", envelope.limitations)
        self.assertIn("capability_budget_exhausted", envelope.disabled_degraded_blocked_path_refs)

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

    def test_row_result_and_timeout_limits_return_blocked_evidence(self):
        cases = [
            ({"row_budget": 1}, "row_budget_exceeded"),
            ({"result_ref_budget": 1}, "result_ref_budget_exceeded"),
            ({"timeout_exceeded": True}, "capability_timeout"),
        ]
        for params, limitation in cases:
            with self.subTest(limitation=limitation):
                envelope = execute_capability(_request(params=params))
                self.assertEqual(envelope.wording_limit, "blocked")
                self.assertIn(limitation, envelope.limitations)


def _request(budget=None, params=None):
    merged_params = {
        "rows": [
            {"period": "h1", "group": "baseline", "amount": 100},
            {"period": "h1", "group": "target", "amount": 120},
        ],
        "result_refs": ("sqlhash-1", "sqlhash-2"),
        "pattern_family": "custom_baseline",
        "period_key": "period",
        "group_key": "group",
        "target_group": "target",
        "baseline_group": "baseline",
        "min_periods": 1,
    }
    merged_params.update(params or {})
    return CapabilityRequest(
        run_id="budget-run",
        accepted_graph_id="graph-1",
        graph_version=1,
        capability_id="compare_periods",
        question_family="custom_baseline_comparison",
        target_claim="Q2 相比 Q1 的付费金额变化",
        claim_type="comparative_change",
        metric="paid_amount",
        scope="all_users",
        time_window="2026-01-01..2026-06-30",
        baseline={"label": "Q1"},
        target={"label": "Q2"},
        grain="custom_baseline",
        filters={},
        dimensions=(),
        contract_versions={},
        role="analyst",
        budget_state=budget or BudgetState("research", 0, 50, 100),
        llm_business_reason="Check budget behavior.",
        params=merged_params,
    )


if __name__ == "__main__":
    unittest.main()
