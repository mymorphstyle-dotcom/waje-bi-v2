from __future__ import annotations

from dataclasses import dataclass
import json
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value


class FactorCoverageContractError(ValueError):
    pass


FACTOR_COVERAGE_STATUSES = frozenset(
    {
        "analyzed",
        "screened_no_signal",
        "unavailable_data",
        "missing_contract",
        "unsupported_grain",
        "not_applicable",
        "deferred_by_budget",
        "failed",
    }
)
FACTOR_COVERAGE_ROLES = frozenset(
    {"required", "disclosure", "auxiliary", "conditional"}
)
_RETRYABILITY = frozenset({"never", "same_input", "replan_required"})


def _required_string(value: Any, error: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise FactorCoverageContractError(error)
    return value


def _digest(value: Any, error: str) -> str:
    value = _required_string(value, error)
    if len(value) != 64 or any(item not in "0123456789abcdef" for item in value):
        raise FactorCoverageContractError(error)
    return value


def _string_tuple(
    value: Any,
    error: str,
    *,
    allow_empty: bool = True,
    sort: bool = True,
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise FactorCoverageContractError(error)
    normalized = tuple(_required_string(item, error) for item in value)
    if not allow_empty and not normalized:
        raise FactorCoverageContractError(error)
    if len(normalized) != len(set(normalized)):
        raise FactorCoverageContractError(error)
    return tuple(sorted(normalized)) if sort else normalized


def _freeze(value: Any, error: str) -> Any:
    try:
        normalized = canonical_value(value)
    except ValueError as exc:
        raise FactorCoverageContractError(error) from exc
    if isinstance(normalized, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item, error) for key, item in normalized.items()}
        )
    if isinstance(normalized, list):
        return tuple(_freeze(item, error) for item in normalized)
    return normalized


def _strict_shape(payload: Any, record_type: type, error: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != set(
        record_type.__dataclass_fields__
    ):
        raise FactorCoverageContractError(error)
    return payload


def _plain(value: Any) -> Any:
    return canonical_value(value)


def _compact_json(value: Any) -> str:
    return json.dumps(
        canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


@dataclass(frozen=True)
class FactorCoveragePlanItem:
    coverage_item_ref: str
    factor_domain_id: str
    business_name: str
    role: str
    axis_refs: tuple[str, ...]
    capability_refs: tuple[str, ...]
    dataset_refs: tuple[str, ...]
    dimension_refs: tuple[str, ...]
    reconciliation_group: str
    task_refs: tuple[str, ...]
    source_refs: tuple[str, ...]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        factor_domain_id: str,
        business_name: str,
        role: str,
        axis_refs: Sequence[str],
        capability_refs: Sequence[str],
        dataset_refs: Sequence[str],
        dimension_refs: Sequence[str],
        reconciliation_group: str,
        task_refs: Sequence[str],
        source_refs: Sequence[str],
    ) -> "FactorCoveragePlanItem":
        factor_domain_id = _required_string(
            factor_domain_id, "factor_coverage_domain_id_invalid"
        )
        if role not in FACTOR_COVERAGE_ROLES:
            raise FactorCoverageContractError("factor_coverage_role_invalid")
        body = {
            "factor_domain_id": factor_domain_id,
            "business_name": _required_string(
                business_name, "factor_coverage_business_name_invalid"
            ),
            "role": role,
            "axis_refs": _string_tuple(
                axis_refs, "factor_coverage_axis_refs_invalid", allow_empty=False
            ),
            "capability_refs": _string_tuple(
                capability_refs,
                "factor_coverage_capability_refs_invalid",
                allow_empty=False,
            ),
            "dataset_refs": _string_tuple(
                dataset_refs, "factor_coverage_dataset_refs_invalid"
            ),
            "dimension_refs": _string_tuple(
                dimension_refs, "factor_coverage_dimension_refs_invalid"
            ),
            "reconciliation_group": _required_string(
                reconciliation_group,
                "factor_coverage_reconciliation_group_invalid",
            ),
            "task_refs": _string_tuple(
                task_refs, "factor_coverage_task_refs_invalid", allow_empty=False
            ),
            "source_refs": _string_tuple(
                source_refs, "factor_coverage_source_refs_invalid", allow_empty=False
            ),
        }
        digest = canonical_digest(body)
        return cls(
            coverage_item_ref="factor-coverage-item:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "FactorCoveragePlanItem":
        payload = _strict_shape(
            payload, cls, "factor_coverage_plan_item_shape_invalid"
        )
        rebuilt = cls.create(
            **{
                key: payload[key]
                for key in payload
                if key not in {"coverage_item_ref", "content_digest"}
            }
        )
        if rebuilt.coverage_item_ref != payload.get("coverage_item_ref"):
            raise FactorCoverageContractError("factor_coverage_item_ref_invalid")
        if rebuilt.content_digest != payload.get("content_digest"):
            raise FactorCoverageContractError("factor_coverage_item_digest_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class FactorCoveragePlan:
    coverage_plan_ref: str
    run_attempt_id: str
    intent_revision_id: str
    plan_revision_id: str
    authority_context_ref: str
    runtime_contract_version: str
    runtime_contract_digest: str
    target_metric_ref: str
    coverage_items: tuple[FactorCoveragePlanItem, ...]
    coverage_item_set_digest: str
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        run_attempt_id: str,
        intent_revision_id: str,
        plan_revision_id: str,
        authority_context_ref: str,
        runtime_contract_version: str,
        runtime_contract_digest: str,
        target_metric_ref: str,
        coverage_items: Sequence[FactorCoveragePlanItem | Mapping[str, Any]],
    ) -> "FactorCoveragePlan":
        if isinstance(coverage_items, (str, bytes)) or not isinstance(
            coverage_items, Sequence
        ):
            raise FactorCoverageContractError("factor_coverage_items_invalid")
        items = tuple(
            item
            if isinstance(item, FactorCoveragePlanItem)
            else FactorCoveragePlanItem.from_dict(item)
            for item in coverage_items
        )
        if not items or len({item.factor_domain_id for item in items}) != len(items):
            raise FactorCoverageContractError("factor_coverage_items_invalid")
        items = tuple(sorted(items, key=lambda item: item.factor_domain_id))
        item_set_digest = canonical_digest(tuple(item.to_dict() for item in items))
        body = {
            "run_attempt_id": _required_string(
                run_attempt_id, "factor_coverage_run_id_invalid"
            ),
            "intent_revision_id": _required_string(
                intent_revision_id, "factor_coverage_intent_id_invalid"
            ),
            "plan_revision_id": _required_string(
                plan_revision_id, "factor_coverage_plan_revision_id_invalid"
            ),
            "authority_context_ref": _required_string(
                authority_context_ref, "factor_coverage_authority_ref_invalid"
            ),
            "runtime_contract_version": _required_string(
                runtime_contract_version,
                "factor_coverage_runtime_contract_version_invalid",
            ),
            "runtime_contract_digest": _digest(
                runtime_contract_digest,
                "factor_coverage_runtime_contract_digest_invalid",
            ),
            "target_metric_ref": _required_string(
                target_metric_ref, "factor_coverage_target_metric_invalid"
            ),
            "coverage_items": items,
            "coverage_item_set_digest": item_set_digest,
        }
        digest = canonical_digest(body)
        return cls(
            coverage_plan_ref="factor-coverage-plan:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "FactorCoveragePlan":
        payload = _strict_shape(payload, cls, "factor_coverage_plan_shape_invalid")
        rebuilt = cls.create(
            **{
                key: payload[key]
                for key in payload
                if key
                not in {
                    "coverage_plan_ref",
                    "coverage_item_set_digest",
                    "content_digest",
                }
            }
        )
        if rebuilt.coverage_item_set_digest != payload.get(
            "coverage_item_set_digest"
        ):
            raise FactorCoverageContractError("factor_coverage_item_set_invalid")
        if rebuilt.coverage_plan_ref != payload.get("coverage_plan_ref"):
            raise FactorCoverageContractError("factor_coverage_plan_ref_invalid")
        if rebuilt.content_digest != payload.get("content_digest"):
            raise FactorCoverageContractError("factor_coverage_plan_digest_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class FactorCoverageOutcome:
    coverage_outcome_ref: str
    coverage_plan_ref: str
    coverage_item_ref: str
    factor_domain_id: str
    status: str
    task_refs: tuple[str, ...]
    outcome_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    limitation_refs: tuple[str, ...]
    result_refs: tuple[str, ...]
    retryability: str
    summary_code: str
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        coverage_plan_ref: str,
        coverage_item_ref: str,
        factor_domain_id: str,
        status: str,
        task_refs: Sequence[str],
        outcome_refs: Sequence[str],
        evidence_refs: Sequence[str],
        limitation_refs: Sequence[str],
        result_refs: Sequence[str],
        retryability: str,
        summary_code: str,
    ) -> "FactorCoverageOutcome":
        if status not in FACTOR_COVERAGE_STATUSES:
            raise FactorCoverageContractError("factor_coverage_status_invalid")
        if retryability not in _RETRYABILITY:
            raise FactorCoverageContractError(
                "factor_coverage_retryability_invalid"
            )
        body = {
            "coverage_plan_ref": _required_string(
                coverage_plan_ref, "factor_coverage_plan_ref_invalid"
            ),
            "coverage_item_ref": _required_string(
                coverage_item_ref, "factor_coverage_item_ref_invalid"
            ),
            "factor_domain_id": _required_string(
                factor_domain_id, "factor_coverage_domain_id_invalid"
            ),
            "status": status,
            "task_refs": _string_tuple(
                task_refs, "factor_coverage_task_refs_invalid", allow_empty=False
            ),
            "outcome_refs": _string_tuple(
                outcome_refs, "factor_coverage_outcome_refs_invalid"
            ),
            "evidence_refs": _string_tuple(
                evidence_refs, "factor_coverage_evidence_refs_invalid"
            ),
            "limitation_refs": _string_tuple(
                limitation_refs, "factor_coverage_limitation_refs_invalid"
            ),
            "result_refs": _string_tuple(
                result_refs, "factor_coverage_result_refs_invalid"
            ),
            "retryability": retryability,
            "summary_code": _required_string(
                summary_code, "factor_coverage_summary_code_invalid"
            ),
        }
        digest = canonical_digest(body)
        return cls(
            coverage_outcome_ref="factor-coverage-outcome:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "FactorCoverageOutcome":
        payload = _strict_shape(
            payload, cls, "factor_coverage_outcome_shape_invalid"
        )
        rebuilt = cls.create(
            **{
                key: payload[key]
                for key in payload
                if key not in {"coverage_outcome_ref", "content_digest"}
            }
        )
        if rebuilt.coverage_outcome_ref != payload.get("coverage_outcome_ref"):
            raise FactorCoverageContractError("factor_coverage_outcome_ref_invalid")
        if rebuilt.content_digest != payload.get("content_digest"):
            raise FactorCoverageContractError("factor_coverage_outcome_digest_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class FactorCoverageResult:
    coverage_result_ref: str
    coverage_plan_ref: str
    execution_result_ref: str
    outcomes: tuple[FactorCoverageOutcome, ...]
    outcome_set_digest: str
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        plan: FactorCoveragePlan,
        execution_result_ref: str,
        outcomes: Sequence[FactorCoverageOutcome | Mapping[str, Any]],
    ) -> "FactorCoverageResult":
        if not isinstance(plan, FactorCoveragePlan):
            raise FactorCoverageContractError("factor_coverage_result_plan_invalid")
        normalized = tuple(
            item
            if isinstance(item, FactorCoverageOutcome)
            else FactorCoverageOutcome.from_dict(item)
            for item in outcomes
        )
        expected = {
            item.factor_domain_id: item.coverage_item_ref
            for item in plan.coverage_items
        }
        actual = {item.factor_domain_id: item.coverage_item_ref for item in normalized}
        if (
            actual != expected
            or len(normalized) != len(expected)
            or any(item.coverage_plan_ref != plan.coverage_plan_ref for item in normalized)
        ):
            raise FactorCoverageContractError("factor_coverage_result_closure_invalid")
        normalized = tuple(sorted(normalized, key=lambda item: item.factor_domain_id))
        outcome_set_digest = canonical_digest(
            tuple(item.to_dict() for item in normalized)
        )
        body = {
            "coverage_plan_ref": plan.coverage_plan_ref,
            "execution_result_ref": _required_string(
                execution_result_ref, "factor_coverage_execution_result_ref_invalid"
            ),
            "outcomes": normalized,
            "outcome_set_digest": outcome_set_digest,
        }
        digest = canonical_digest(body)
        return cls(
            coverage_result_ref="factor-coverage-result:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        plan: FactorCoveragePlan,
    ) -> "FactorCoverageResult":
        payload = _strict_shape(payload, cls, "factor_coverage_result_shape_invalid")
        rebuilt = cls.create(
            plan=plan,
            execution_result_ref=payload["execution_result_ref"],
            outcomes=payload["outcomes"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise FactorCoverageContractError("factor_coverage_result_integrity_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class InvestigationBranch:
    branch_ref: str
    coverage_item_ref: str
    factor_domain_id: str
    capability_allowlist: tuple[str, ...]
    task_refs: tuple[str, ...]
    snapshot_refs: tuple[str, ...]
    release_refs: tuple[str, ...]
    stop_policy: Mapping[str, Any]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        item: FactorCoveragePlanItem,
        snapshot_refs: Sequence[str],
        release_refs: Sequence[str],
        stop_policy: Mapping[str, Any],
    ) -> "InvestigationBranch":
        if not isinstance(item, FactorCoveragePlanItem):
            raise FactorCoverageContractError("investigation_branch_item_invalid")
        body = {
            "coverage_item_ref": item.coverage_item_ref,
            "factor_domain_id": item.factor_domain_id,
            "capability_allowlist": item.capability_refs,
            "task_refs": item.task_refs,
            "snapshot_refs": _string_tuple(
                snapshot_refs, "investigation_branch_snapshot_refs_invalid"
            ),
            "release_refs": _string_tuple(
                release_refs, "investigation_branch_release_refs_invalid"
            ),
            "stop_policy": _freeze(
                stop_policy, "investigation_branch_stop_policy_invalid"
            ),
        }
        digest = canonical_digest(body)
        return cls(
            branch_ref="investigation-branch:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        item: FactorCoveragePlanItem,
    ) -> "InvestigationBranch":
        payload = _strict_shape(payload, cls, "investigation_branch_shape_invalid")
        rebuilt = cls.create(
            item=item,
            snapshot_refs=payload["snapshot_refs"],
            release_refs=payload["release_refs"],
            stop_policy=payload["stop_policy"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise FactorCoverageContractError("investigation_branch_integrity_invalid")
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class InvestigationSynthesis:
    synthesis_ref: str
    coverage_result_ref: str
    reconciliation_groups: tuple[Mapping[str, Any], ...]
    ranked_factor_domain_refs: tuple[str, ...]
    limitation_refs: tuple[str, ...]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        plan: FactorCoveragePlan,
        coverage_result: FactorCoverageResult,
        reconciliation_groups: Sequence[Mapping[str, Any]],
        ranked_factor_domain_refs: Sequence[str],
        limitation_refs: Sequence[str],
    ) -> "InvestigationSynthesis":
        if not isinstance(plan, FactorCoveragePlan) or not isinstance(
            coverage_result, FactorCoverageResult
        ):
            raise FactorCoverageContractError("investigation_synthesis_result_invalid")
        if coverage_result.coverage_plan_ref != plan.coverage_plan_ref:
            raise FactorCoverageContractError("investigation_synthesis_result_invalid")
        outcome_by_domain = {
            item.factor_domain_id: item for item in coverage_result.outcomes
        }
        known = set(outcome_by_domain)
        ranked = _string_tuple(
            ranked_factor_domain_refs,
            "investigation_synthesis_ranked_domains_invalid",
            sort=False,
        )
        if not set(ranked) <= known or any(
            outcome_by_domain[domain_id].status != "analyzed" for domain_id in ranked
        ):
            raise FactorCoverageContractError(
                "investigation_synthesis_ranked_domains_invalid"
            )
        groups = tuple(
            _freeze(item, "investigation_synthesis_groups_invalid")
            for item in reconciliation_groups
        )
        _validate_reconciliation_groups(
            groups,
            plan=plan,
            coverage_result=coverage_result,
        )
        body = {
            "coverage_result_ref": coverage_result.coverage_result_ref,
            "reconciliation_groups": groups,
            "ranked_factor_domain_refs": ranked,
            "limitation_refs": _string_tuple(
                limitation_refs, "investigation_synthesis_limitations_invalid"
            ),
        }
        digest = canonical_digest(body)
        return cls(
            synthesis_ref="investigation-synthesis:sha256:" + digest,
            content_digest=digest,
            **body,
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        plan: FactorCoveragePlan,
        coverage_result: FactorCoverageResult,
    ) -> "InvestigationSynthesis":
        payload = _strict_shape(payload, cls, "investigation_synthesis_shape_invalid")
        rebuilt = cls.create(
            plan=plan,
            coverage_result=coverage_result,
            reconciliation_groups=payload["reconciliation_groups"],
            ranked_factor_domain_refs=payload["ranked_factor_domain_refs"],
            limitation_refs=payload["limitation_refs"],
        )
        if rebuilt.to_dict() != canonical_value(payload):
            raise FactorCoverageContractError(
                "investigation_synthesis_integrity_invalid"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


def compile_factor_coverage_plan(
    *,
    plan_revision: Any,
    authority_context: Any,
    runtime_registry: Any,
) -> FactorCoveragePlan:
    from bi_agent.runtime.plan_authority import AuthorityContext, PlanRevision
    from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry

    if not isinstance(plan_revision, PlanRevision):
        raise FactorCoverageContractError("factor_coverage_plan_revision_invalid")
    if not isinstance(authority_context, AuthorityContext):
        raise FactorCoverageContractError("factor_coverage_authority_context_invalid")
    if not isinstance(runtime_registry, RuntimeContractRegistry):
        raise FactorCoverageContractError("factor_coverage_registry_invalid")
    if (
        plan_revision.authority_context_ref != authority_context.authority_context_ref
        or plan_revision.run_attempt_id != authority_context.run_attempt_id
        or authority_context.contract_versions.get("runtime_bindings")
        != runtime_registry.contract_version
        or authority_context.contract_versions.get("runtime_bindings_digest")
        != runtime_registry.source_payload_digest
    ):
        raise FactorCoverageContractError("factor_coverage_authority_closure_invalid")
    target_metric_refs = tuple(
        dict.fromkeys(
            metric_ref
            for axis in plan_revision.analysis_axes
            for metric_ref in axis.target_metric_refs
        )
    )
    if len(target_metric_refs) != 1:
        raise FactorCoverageContractError("factor_coverage_target_metric_invalid")
    goal_ids = tuple(
        dict.fromkeys(
            goal_ref
            for axis in plan_revision.analysis_axes
            for goal_ref in axis.goal_refs
        )
    )
    domain_ids = runtime_registry.factor_domain_ids_for_goals(
        goal_ids,
        target_metric=target_metric_refs[0],
    )
    axis_by_id = {axis.axis_id: axis for axis in plan_revision.analysis_axes}
    task_by_domain: dict[str, list[Any]] = {item: [] for item in domain_ids}
    for task in plan_revision.capability_tasks:
        for input_ref in task.normalized_input_refs:
            prefix = "factor-domain:"
            if input_ref.startswith(prefix):
                factor_domain_id = input_ref[len(prefix) :]
                if factor_domain_id in task_by_domain:
                    task_by_domain[factor_domain_id].append(task)
    items = []
    for factor_domain_id in domain_ids:
        contract = runtime_registry.factor_domain(factor_domain_id)
        axis_refs = tuple(
            axis_id for axis_id in contract["axis_refs"] if axis_id in axis_by_id
        )
        tasks = tuple(task_by_domain[factor_domain_id])
        capability_refs = tuple(
            capability_id
            for capability_id in contract["capability_refs"]
            if any(task.capability_id == capability_id for task in tasks)
        )
        if not axis_refs or not tasks or not capability_refs:
            raise FactorCoverageContractError(
                f"factor_coverage_execution_path_missing:{factor_domain_id}"
            )
        items.append(
            FactorCoveragePlanItem.create(
                factor_domain_id=factor_domain_id,
                business_name=str(contract["business_name"]),
                role=str(contract["role"]),
                axis_refs=axis_refs,
                capability_refs=capability_refs,
                dataset_refs=tuple(contract["dataset_refs"]),
                dimension_refs=tuple(contract["dimension_refs"]),
                reconciliation_group=str(contract["reconciliation_group"]),
                task_refs=tuple(task.task_id for task in tasks),
                source_refs=tuple(contract["source_refs"]),
            )
        )
    return FactorCoveragePlan.create(
        run_attempt_id=plan_revision.run_attempt_id,
        intent_revision_id=plan_revision.intent_revision_id,
        plan_revision_id=plan_revision.plan_revision_id,
        authority_context_ref=authority_context.authority_context_ref,
        runtime_contract_version=runtime_registry.contract_version,
        runtime_contract_digest=runtime_registry.source_payload_digest,
        target_metric_ref=target_metric_refs[0],
        coverage_items=tuple(items),
    )


def settle_factor_coverage(
    *,
    plan: FactorCoveragePlan,
    execution_result: Any,
) -> FactorCoverageResult:
    from bi_agent.runtime.authoritative_execution_result import (
        AuthoritativeExecutionResult,
    )

    if not isinstance(plan, FactorCoveragePlan):
        raise FactorCoverageContractError("factor_coverage_result_plan_invalid")
    if not isinstance(execution_result, AuthoritativeExecutionResult):
        raise FactorCoverageContractError("factor_coverage_execution_result_invalid")
    if (
        execution_result.run_attempt_id != plan.run_attempt_id
        or execution_result.intent_revision_id != plan.intent_revision_id
        or execution_result.plan_revision_id != plan.plan_revision_id
        or execution_result.authority_context_ref != plan.authority_context_ref
    ):
        raise FactorCoverageContractError("factor_coverage_result_closure_invalid")
    bundle_by_task = {
        bundle[1].task_id: bundle for bundle in execution_result.capability_outcome_bundles
    }
    task_by_id = {
        task.task_id: task for task in execution_result.plan_revision.capability_tasks
    }
    outcomes = []
    for item in plan.coverage_items:
        bundles = tuple(
            bundle_by_task[task_ref]
            for task_ref in item.task_refs
            if task_ref in bundle_by_task
        )
        capability_outcomes = tuple(bundle[1] for bundle in bundles)
        evidence_entries = tuple(entry for bundle in bundles for entry in bundle[2])
        status, summary_code = _settled_coverage_status(
            item=item,
            tasks=tuple(task_by_id[task_ref] for task_ref in item.task_refs),
            outcomes=capability_outcomes,
            stop_reason=execution_result.exploration_stop_record.reason,
        )
        retryability = _coverage_retryability(capability_outcomes)
        outcomes.append(
            FactorCoverageOutcome.create(
                coverage_plan_ref=plan.coverage_plan_ref,
                coverage_item_ref=item.coverage_item_ref,
                factor_domain_id=item.factor_domain_id,
                status=status,
                task_refs=item.task_refs,
                outcome_refs=tuple(item.outcome_ref for item in capability_outcomes),
                # Claim support edges bind to persisted evidence-ledger entries.
                # Keeping the same authority identity here makes factor ranking
                # replayable from the ledger without joining on a lower-level
                # capability evidence identifier.
                evidence_refs=tuple(item.entry_ref for item in evidence_entries),
                limitation_refs=tuple(
                    dict.fromkeys(
                        limitation_ref
                        for outcome in capability_outcomes
                        for limitation_ref in outcome.limitation_refs
                    )
                ),
                result_refs=tuple(
                    dict.fromkeys(
                        result_ref
                        for entry in evidence_entries
                        for result_ref in entry.result_refs
                    )
                ),
                retryability=retryability,
                summary_code=summary_code,
            )
        )
    return FactorCoverageResult.create(
        plan=plan,
        execution_result_ref=execution_result.authoritative_execution_result_ref,
        outcomes=tuple(outcomes),
    )


def build_investigation_branches(
    *,
    plan: FactorCoveragePlan,
    authority_context: Any,
) -> tuple[InvestigationBranch, ...]:
    from bi_agent.runtime.plan_authority import AuthorityContext

    if not isinstance(plan, FactorCoveragePlan) or not isinstance(
        authority_context, AuthorityContext
    ):
        raise FactorCoverageContractError("investigation_branch_authority_invalid")
    if authority_context.authority_context_ref != plan.authority_context_ref:
        raise FactorCoverageContractError("investigation_branch_authority_invalid")
    return tuple(
        InvestigationBranch.create(
            item=item,
            snapshot_refs=authority_context.snapshot_refs,
            release_refs=authority_context.release_refs,
            stop_policy={
                "policy": "existing_capability_scheduler_budget",
                "publication_authority": "none",
                "thread_head_authority": "none",
                "query_access": "reviewed_capability_only",
            },
        )
        for item in plan.coverage_items
    )


_RECONCILIATION_GROUP_FIELDS = frozenset(
    {
        "reconciliation_group",
        "factor_domain_refs",
        "coverage_statuses",
        "evidence_refs",
        "limitation_refs",
        "claim_refs",
        "additivity_policy",
    }
)
_CLAIM_STRENGTH_RANK = {
    "descriptive": 0,
    "directional": 1,
    "boundary": 1,
    "trust_boundary": 1,
    "anomaly_candidate": 2,
    "candidate_driver": 2,
    "candidate_mechanism": 2,
    "dimension_localization": 2,
    "accounting_contribution": 3,
    "quantified_contribution": 3,
    "recurring_pattern": 3,
    "statistical_association": 3,
    "causal_effect": 4,
    "scenario": 4,
}


def _validate_reconciliation_groups(
    groups: Sequence[Mapping[str, Any]],
    *,
    plan: FactorCoveragePlan,
    coverage_result: FactorCoverageResult,
) -> None:
    item_by_domain = {item.factor_domain_id: item for item in plan.coverage_items}
    outcome_by_domain = {
        item.factor_domain_id: item for item in coverage_result.outcomes
    }
    seen_domains: set[str] = set()
    seen_groups: set[str] = set()
    for group in groups:
        if not isinstance(group, Mapping) or set(group) != _RECONCILIATION_GROUP_FIELDS:
            raise FactorCoverageContractError(
                "investigation_synthesis_groups_invalid"
            )
        group_id = _required_string(
            group["reconciliation_group"],
            "investigation_synthesis_groups_invalid",
        )
        if group_id in seen_groups or group["additivity_policy"] != "within_group_only":
            raise FactorCoverageContractError(
                "investigation_synthesis_groups_invalid"
            )
        seen_groups.add(group_id)
        domains = _string_tuple(
            group["factor_domain_refs"],
            "investigation_synthesis_groups_invalid",
            allow_empty=False,
            sort=False,
        )
        if (
            set(domains) & seen_domains
            or any(domain_id not in item_by_domain for domain_id in domains)
            or any(
                item_by_domain[domain_id].reconciliation_group != group_id
                for domain_id in domains
            )
        ):
            raise FactorCoverageContractError(
                "investigation_synthesis_groups_invalid"
            )
        seen_domains.update(domains)
        statuses = group["coverage_statuses"]
        if not isinstance(statuses, Mapping) or dict(statuses) != {
            domain_id: outcome_by_domain[domain_id].status for domain_id in domains
        }:
            raise FactorCoverageContractError(
                "investigation_synthesis_groups_invalid"
            )
        evidence_refs = _string_tuple(
            group["evidence_refs"], "investigation_synthesis_groups_invalid"
        )
        limitation_refs = _string_tuple(
            group["limitation_refs"], "investigation_synthesis_groups_invalid"
        )
        _string_tuple(group["claim_refs"], "investigation_synthesis_groups_invalid")
        expected_evidence = tuple(
            sorted(
                {
                    ref
                    for domain_id in domains
                    for ref in outcome_by_domain[domain_id].evidence_refs
                }
            )
        )
        expected_limitations = tuple(
            sorted(
                {
                    ref
                    for domain_id in domains
                    for ref in outcome_by_domain[domain_id].limitation_refs
                }
            )
        )
        if evidence_refs != expected_evidence or limitation_refs != expected_limitations:
            raise FactorCoverageContractError(
                "investigation_synthesis_groups_invalid"
            )
    if seen_domains != set(item_by_domain) or seen_groups != {
        item.reconciliation_group for item in plan.coverage_items
    }:
        raise FactorCoverageContractError("investigation_synthesis_groups_invalid")


def _bind_verified_claims_to_domains(
    *,
    claim_settlement: Any,
    coverage_result: FactorCoverageResult,
    claim_refs_by_domain: dict[str, set[str]],
    strength_by_domain: dict[str, int],
) -> None:
    from bi_agent.runtime.claim_settlement import validate_typed_claim_settlement

    try:
        settlement = validate_typed_claim_settlement(claim_settlement)
    except (TypeError, ValueError) as exc:
        raise FactorCoverageContractError(
            "investigation_synthesis_claim_settlement_invalid"
        ) from exc
    edge_by_ref = {
        edge.support_edge_ref: edge for edge in settlement.accepted_support_edges
    }
    for claim in settlement.accepted_claims:
        if claim.status != "verified":
            continue
        evidence_refs = {
            edge.source_ref
            for edge_ref in claim.support_edge_refs
            if (edge := edge_by_ref.get(edge_ref)) is not None
            and edge.source_type == "evidence"
            and edge.kind == "supports"
        }
        if not evidence_refs:
            continue
        strength_rank = _CLAIM_STRENGTH_RANK.get(
            claim.publication_ceiling.strength,
            -1,
        )
        for outcome in coverage_result.outcomes:
            if evidence_refs.isdisjoint(outcome.evidence_refs):
                continue
            claim_refs_by_domain[outcome.factor_domain_id].add(claim.claim_ref)
            strength_by_domain[outcome.factor_domain_id] = max(
                strength_by_domain[outcome.factor_domain_id],
                strength_rank,
            )


def synthesize_factor_coverage(
    *,
    plan: FactorCoveragePlan,
    coverage_result: FactorCoverageResult,
    claim_settlement: Any | None = None,
) -> InvestigationSynthesis:
    if not isinstance(plan, FactorCoveragePlan) or not isinstance(
        coverage_result, FactorCoverageResult
    ):
        raise FactorCoverageContractError("investigation_synthesis_result_invalid")
    if coverage_result.coverage_plan_ref != plan.coverage_plan_ref:
        raise FactorCoverageContractError("investigation_synthesis_result_invalid")

    claim_refs_by_domain: dict[str, set[str]] = {
        item.factor_domain_id: set() for item in plan.coverage_items
    }
    strength_by_domain: dict[str, int] = {
        item.factor_domain_id: -1 for item in plan.coverage_items
    }
    limitations = {
        "cross_reconciliation_group_additivity_prohibited",
        "coverage_status_does_not_establish_causality",
    }
    if claim_settlement is None:
        limitations.add("claim_settlement_pending_factor_ranking_provisional")
    else:
        _bind_verified_claims_to_domains(
            claim_settlement=claim_settlement,
            coverage_result=coverage_result,
            claim_refs_by_domain=claim_refs_by_domain,
            strength_by_domain=strength_by_domain,
        )

    outcome_by_domain = {
        item.factor_domain_id: item for item in coverage_result.outcomes
    }
    groups: list[dict[str, Any]] = []
    for reconciliation_group in dict.fromkeys(
        item.reconciliation_group for item in plan.coverage_items
    ):
        items = tuple(
            item
            for item in plan.coverage_items
            if item.reconciliation_group == reconciliation_group
        )
        outcomes = tuple(outcome_by_domain[item.factor_domain_id] for item in items)
        groups.append(
            {
                "reconciliation_group": reconciliation_group,
                "factor_domain_refs": tuple(item.factor_domain_id for item in items),
                "coverage_statuses": {
                    item.factor_domain_id: outcome_by_domain[item.factor_domain_id].status
                    for item in items
                },
                "evidence_refs": tuple(
                    sorted(
                        {
                            ref
                            for outcome in outcomes
                            for ref in outcome.evidence_refs
                        }
                    )
                ),
                "limitation_refs": tuple(
                    sorted(
                        {
                            ref
                            for outcome in outcomes
                            for ref in outcome.limitation_refs
                        }
                    )
                ),
                "claim_refs": tuple(
                    sorted(
                        {
                            ref
                            for item in items
                            for ref in claim_refs_by_domain[item.factor_domain_id]
                        }
                    )
                ),
                "additivity_policy": "within_group_only",
            }
        )
    ranked = tuple(
        item.factor_domain_id
        for item in sorted(
            (
                item
                for item in plan.coverage_items
                if outcome_by_domain[item.factor_domain_id].status == "analyzed"
                and (
                    claim_settlement is None
                    or claim_refs_by_domain[item.factor_domain_id]
                )
            ),
            key=lambda item: (
                -strength_by_domain[item.factor_domain_id],
                -len(claim_refs_by_domain[item.factor_domain_id]),
                0 if item.role == "required" else 1,
                item.factor_domain_id,
            ),
        )
    )
    limitations.update(
        ref for outcome in coverage_result.outcomes for ref in outcome.limitation_refs
    )
    return InvestigationSynthesis.create(
        plan=plan,
        coverage_result=coverage_result,
        reconciliation_groups=groups,
        ranked_factor_domain_refs=ranked,
        limitation_refs=tuple(sorted(limitations)),
    )


def narrative_factor_coverage_context(
    *,
    plan: FactorCoveragePlan,
    coverage_result: FactorCoverageResult,
    synthesis: InvestigationSynthesis,
) -> str:
    replayed_result = FactorCoverageResult.from_dict(
        coverage_result.to_dict(),
        plan=plan,
    )
    replayed_synthesis = InvestigationSynthesis.from_dict(
        synthesis.to_dict(),
        plan=plan,
        coverage_result=replayed_result,
    )
    outcome_by_domain = {
        item.factor_domain_id: item for item in replayed_result.outcomes
    }
    ranked = set(replayed_synthesis.ranked_factor_domain_refs)
    payload = {
        "interpretation": (
            "Coverage records describe investigation completion and evidence closure. "
            "Only verified claim payloads may rank business impact."
        ),
        "domains": [
            {
                "business_name": item.business_name,
                "status": outcome_by_domain[item.factor_domain_id].status,
                "verified_claim_support": item.factor_domain_id in ranked,
                "limitation_refs": outcome_by_domain[
                    item.factor_domain_id
                ].limitation_refs,
            }
            for item in plan.coverage_items
        ],
        "reconciliation_policy": "within_group_only",
        "limitation_refs": replayed_synthesis.limitation_refs,
    }
    return "factor_coverage=" + _compact_json(payload)


def _settled_coverage_status(
    *,
    item: FactorCoveragePlanItem,
    tasks: Sequence[Any],
    outcomes: Sequence[Any],
    stop_reason: str,
) -> tuple[str, str]:
    if len(outcomes) < len(tasks):
        if stop_reason == "hard_budget_reached":
            return "deferred_by_budget", "budget_stopped_before_domain_settlement"
        return "failed", "domain_task_outcome_missing"
    statuses = {str(outcome.status) for outcome in outcomes}
    if statuses & {"integrity_failed", "technical_failed"}:
        return "failed", "domain_execution_failed"
    if "skipped" in statuses:
        return "failed", "domain_dependency_not_succeeded"
    if "succeeded" in statuses:
        return "analyzed", (
            "domain_analyzed_with_local_boundaries"
            if "unavailable" in statuses
            else "domain_analyzed"
        )
    if statuses == {"unavailable"}:
        limitation_refs = {
            limitation_ref
            for outcome in outcomes
            for limitation_ref in outcome.limitation_refs
        }
        if limitation_refs == {"no_event_matches"}:
            return "screened_no_signal", "no_material_signal"
        input_states = tuple(
            state
            for task in tasks
            for state in task.execution_policy.get("input_states", ())
        )
        if any(state.get("availability") == "missing_contract" for state in input_states):
            return "missing_contract", "required_contract_unavailable"
        return "unavailable_data", "required_dataset_unavailable"
    if statuses == {"superseded"}:
        return "not_applicable", "coverage_item_superseded"
    return "failed", f"coverage_status_unhandled:{item.factor_domain_id}"


def _coverage_retryability(outcomes: Sequence[Any]) -> str:
    observed = {str(outcome.retryability) for outcome in outcomes}
    if "replan_required" in observed:
        return "replan_required"
    if "same_input" in observed:
        return "same_input"
    return "never"


__all__ = (
    "FACTOR_COVERAGE_ROLES",
    "FACTOR_COVERAGE_STATUSES",
    "FactorCoverageContractError",
    "FactorCoverageOutcome",
    "FactorCoveragePlan",
    "FactorCoveragePlanItem",
    "FactorCoverageResult",
    "InvestigationBranch",
    "InvestigationSynthesis",
    "build_investigation_branches",
    "compile_factor_coverage_plan",
    "narrative_factor_coverage_context",
    "settle_factor_coverage",
    "synthesize_factor_coverage",
)
