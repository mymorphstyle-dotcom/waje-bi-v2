import unittest

from bi_agent.runtime.exploration_budget import (
    default_budget,
    record_capability_call,
    should_ask_before_more_exploration,
)


class ExplorationBudgetTest(unittest.TestCase):
    def test_ordinary_research_budget_defaults_to_50_soft_100_hard(self):
        budget = default_budget("ordinary")

        self.assertEqual(budget.mode, "research")
        self.assertEqual(budget.soft_limit, 50)
        self.assertEqual(budget.hard_limit, 100)

    def test_deep_attribution_budget_defaults_to_100(self):
        budget = default_budget("deep_attribution")

        self.assertEqual(budget.soft_limit, 100)
        self.assertEqual(budget.hard_limit, 100)

    def test_hard_limit_requires_user_question(self):
        budget = default_budget("ordinary")
        for _ in range(100):
            budget = record_capability_call(budget)

        self.assertTrue(should_ask_before_more_exploration(budget))

    def test_soft_limit_does_not_block_research(self):
        budget = default_budget("ordinary")
        for _ in range(50):
            budget = record_capability_call(budget)

        self.assertFalse(should_ask_before_more_exploration(budget))


if __name__ == "__main__":
    unittest.main()
