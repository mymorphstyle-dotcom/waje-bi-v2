from __future__ import annotations

from dataclasses import replace

from bi_agent.runtime.capability_models import BudgetState


def default_budget(depth: str) -> BudgetState:
    if depth == "deep_attribution":
        return BudgetState(
            mode="research", used_capability_calls=0, soft_limit=100, hard_limit=100
        )
    return BudgetState(
        mode="research", used_capability_calls=0, soft_limit=50, hard_limit=100
    )


def record_capability_call(budget: BudgetState) -> BudgetState:
    return replace(budget, used_capability_calls=budget.used_capability_calls + 1)


def should_ask_before_more_exploration(budget: BudgetState) -> bool:
    return budget.used_capability_calls >= budget.hard_limit
