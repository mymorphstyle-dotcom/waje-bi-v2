from __future__ import annotations

from copy import deepcopy

import pytest

from bi_agent.runtime.exploration_budget_policy import (
    ExplorationBudgetPolicy,
    ExplorationBudgetPolicyError,
)
from bi_agent.runtime.contracts import load_contract
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)


def _registry() -> RuntimeContractRegistry:
    return RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)


def test_production_policy_is_content_addressed_and_unbounded() -> None:
    policy = _registry().exploration_budget_policy

    assert policy.schema_version == "exploration-budget-policy.v1"
    assert policy.scope == "run_attempt"
    assert policy.protected_axis_roles == ("required", "disclosure")
    assert policy.auxiliary_budget_limit is None
    assert policy.accounting_unit == "declared_task_unit"
    assert policy.budget_policy_ref == "budget-policy:sha256:" + policy.content_digest
    assert ExplorationBudgetPolicy.from_dict(policy.to_dict()) == policy


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("scope", "plan_revision"),
        ("protected_axis_roles", ["required"]),
        ("auxiliary_budget_limit", -1),
        ("accounting_unit", "provider_token"),
    ),
)
def test_policy_rejects_non_authoritative_shapes(field: str, value: object) -> None:
    payload = deepcopy(_registry().exploration_budget_policy.contract_payload())
    payload[field] = value

    with pytest.raises(ExplorationBudgetPolicyError):
        ExplorationBudgetPolicy.from_contract(payload)


def test_registry_policy_ref_changes_when_auxiliary_cap_changes() -> None:
    registry = _registry()
    payload = load_contract(CANONICAL_RUNTIME_BINDINGS_PATH)
    payload["exploration_budget_policy"]["auxiliary_budget_limit"] = 3
    limited = RuntimeContractRegistry(payload).exploration_budget_policy

    assert limited.auxiliary_budget_limit == 3
    assert (
        limited.budget_policy_ref
        != registry.exploration_budget_policy.budget_policy_ref
    )
