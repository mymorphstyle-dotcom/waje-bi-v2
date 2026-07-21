from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value
from bi_agent.runtime.plan_authority import PlanRevision


EXPLORATION_BUDGET_POLICY_SCHEMA_VERSION = "exploration-budget-policy.v1"
_POLICY_SCOPE = "run_attempt"
_PROTECTED_AXIS_ROLES = ("required", "disclosure")
_ACCOUNTING_UNIT = "declared_task_unit"
_CONTRACT_FIELDS = frozenset(
    {
        "schema_version",
        "scope",
        "protected_axis_roles",
        "auxiliary_budget_limit",
        "accounting_unit",
    }
)


class ExplorationBudgetPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class ExplorationBudgetPolicy:
    budget_policy_ref: str
    schema_version: str
    scope: str
    protected_axis_roles: tuple[str, ...]
    auxiliary_budget_limit: int | None
    accounting_unit: str
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        schema_version: str,
        scope: str,
        protected_axis_roles: Sequence[str],
        auxiliary_budget_limit: int | None,
        accounting_unit: str,
    ) -> "ExplorationBudgetPolicy":
        if schema_version != EXPLORATION_BUDGET_POLICY_SCHEMA_VERSION:
            raise ExplorationBudgetPolicyError(
                "exploration_budget_policy_schema_version_invalid"
            )
        if scope != _POLICY_SCOPE:
            raise ExplorationBudgetPolicyError(
                "exploration_budget_policy_scope_invalid"
            )
        if isinstance(protected_axis_roles, (str, bytes)) or not isinstance(
            protected_axis_roles, Sequence
        ):
            raise ExplorationBudgetPolicyError(
                "exploration_budget_policy_protected_roles_invalid"
            )
        roles = tuple(protected_axis_roles)
        if roles != _PROTECTED_AXIS_ROLES:
            raise ExplorationBudgetPolicyError(
                "exploration_budget_policy_protected_roles_invalid"
            )
        if auxiliary_budget_limit is not None and (
            type(auxiliary_budget_limit) is not int or auxiliary_budget_limit < 0
        ):
            raise ExplorationBudgetPolicyError(
                "exploration_budget_policy_auxiliary_limit_invalid"
            )
        if accounting_unit != _ACCOUNTING_UNIT:
            raise ExplorationBudgetPolicyError(
                "exploration_budget_policy_accounting_unit_invalid"
            )
        body = {
            "schema_version": schema_version,
            "scope": scope,
            "protected_axis_roles": roles,
            "auxiliary_budget_limit": auxiliary_budget_limit,
            "accounting_unit": accounting_unit,
        }
        digest = canonical_digest(body)
        return cls(
            budget_policy_ref="budget-policy:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_contract(cls, payload: Mapping[str, Any]) -> "ExplorationBudgetPolicy":
        if not isinstance(payload, Mapping) or set(payload) != _CONTRACT_FIELDS:
            raise ExplorationBudgetPolicyError(
                "exploration_budget_policy_contract_shape_invalid"
            )
        return cls.create(
            schema_version=payload["schema_version"],
            scope=payload["scope"],
            protected_axis_roles=payload["protected_axis_roles"],
            auxiliary_budget_limit=payload["auxiliary_budget_limit"],
            accounting_unit=payload["accounting_unit"],
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ExplorationBudgetPolicy":
        if not isinstance(payload, Mapping) or set(payload) != set(
            cls.__dataclass_fields__
        ):
            raise ExplorationBudgetPolicyError(
                "exploration_budget_policy_shape_invalid"
            )
        rebuilt = cls.create(
            schema_version=payload["schema_version"],
            scope=payload["scope"],
            protected_axis_roles=payload["protected_axis_roles"],
            auxiliary_budget_limit=payload["auxiliary_budget_limit"],
            accounting_unit=payload["accounting_unit"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise ExplorationBudgetPolicyError(
                "exploration_budget_policy_integrity_invalid"
            )
        return rebuilt

    def contract_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "scope": self.scope,
            "protected_axis_roles": list(self.protected_axis_roles),
            "auxiliary_budget_limit": self.auxiliary_budget_limit,
            "accounting_unit": self.accounting_unit,
        }

    def to_dict(self) -> dict[str, Any]:
        return canonical_value(self)

    def protected_task_ids(self, plan_revision: PlanRevision) -> frozenset[str]:
        self._validate_plan_binding(plan_revision)
        axis_role_by_ref = {
            axis.analysis_axis_ref: axis.role for axis in plan_revision.analysis_axes
        }
        protected: set[str] = set()
        for task in plan_revision.capability_tasks:
            matching_axis_refs = set(task.normalized_input_refs).intersection(
                axis_role_by_ref
            )
            if len(matching_axis_refs) != 1:
                raise ExplorationBudgetPolicyError(
                    "exploration_budget_policy_task_axis_binding_invalid:"
                    + task.task_id
                )
            axis_ref = next(iter(matching_axis_refs))
            if axis_role_by_ref[axis_ref] in set(self.protected_axis_roles) or any(
                bool(edge["required"]) for edge in task.obligation_edges
            ):
                protected.add(task.task_id)

        dependencies_by_task = {
            task.task_id: set(task.dependency_task_ids)
            for task in plan_revision.capability_tasks
        }
        while True:
            dependency_closure = {
                dependency_id
                for task_id in protected
                for dependency_id in dependencies_by_task[task_id]
            }
            expanded = protected | dependency_closure
            if expanded == protected:
                break
            protected = expanded
        return frozenset(protected)

    def effective_hard_budget_limit(self, plan_revision: PlanRevision) -> int | None:
        protected_task_ids = self.protected_task_ids(plan_revision)
        if self.auxiliary_budget_limit is None:
            return None
        protected_budget = sum(
            task.declared_budget_units
            for task in plan_revision.capability_tasks
            if task.task_id in protected_task_ids
        )
        return protected_budget + self.auxiliary_budget_limit

    def _validate_plan_binding(self, plan_revision: PlanRevision) -> None:
        if not isinstance(plan_revision, PlanRevision):
            raise ExplorationBudgetPolicyError("exploration_budget_policy_plan_invalid")
        if plan_revision.budget_policy_ref != self.budget_policy_ref:
            raise ExplorationBudgetPolicyError(
                "exploration_budget_policy_plan_ref_mismatch"
            )


__all__ = [
    "EXPLORATION_BUDGET_POLICY_SCHEMA_VERSION",
    "ExplorationBudgetPolicy",
    "ExplorationBudgetPolicyError",
]
