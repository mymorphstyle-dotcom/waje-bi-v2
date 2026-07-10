from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
import math
from typing import Any, Iterable, Mapping

from bi_agent.runtime.analysis_contracts import (
    AnalysisContract,
    CapabilityExecutionPlan,
    CapabilityInputSlot,
    ContractGap,
    DimensionBinding,
    JoinExpectation,
    MetricBinding,
    QueryContract,
    ReconciliationBinding,
    ResolvedWindow,
    ResultShape,
    query_contract_signature,
    stable_contract_signature,
)
from bi_agent.runtime.dataset_catalog import DatasetCatalog, DatasetSnapshot
from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry
from bi_agent.runtime.window_resolver import WindowResolution, resolve_revenue_windows


@dataclass(frozen=True)
class AnalysisCompileOutcome:
    analysis_contract: AnalysisContract
    query_contracts: tuple[QueryContract, ...]
    capability_plans: tuple[CapabilityExecutionPlan, ...]


@dataclass(frozen=True)
class _DependencyIndex:
    metric_ids: tuple[str, ...]
    dimension_ids: tuple[str, ...]
    dataset_ids: tuple[str, ...]
    metric_owners: Mapping[str, tuple[str, ...]]
    dimension_owners: Mapping[str, tuple[str, ...]]
    dataset_owners: Mapping[str, tuple[str, ...]]


def compile_analysis_contract(
    *,
    run_id: str,
    proposal: Mapping[str, Any],
    accepted_capabilities: Iterable[str],
    catalog: DatasetCatalog,
    registry: RuntimeContractRegistry,
    as_of: datetime,
    permission_scope: str,
) -> AnalysisCompileOutcome:
    capabilities = _dedupe(accepted_capabilities)
    dependencies = _build_dependency_index(proposal, capabilities, registry)
    required_dataset_ids = dependencies.dataset_ids
    capability_gaps = _capability_contract_gaps(capabilities, registry)
    executable_dataset_ids, dataset_contract_gaps = _validate_dataset_contracts(
        required_dataset_ids,
        registry,
        dependencies.dataset_owners,
    )
    snapshots, source_gaps = _resolve_snapshots(
        executable_dataset_ids,
        catalog,
        registry,
        as_of,
        permission_scope,
        dependencies.dataset_owners,
    )
    snapshots, dataset_schema_gaps = _validate_snapshot_schemas(
        snapshots,
        registry,
        dependencies.dataset_owners,
    )
    metric_bindings, metric_gaps = _bind_metrics(
        dependencies.metric_ids,
        registry,
        snapshots,
        dependencies.metric_owners,
    )
    dimension_bindings, dimension_gaps = _bind_dimensions(
        dependencies.dimension_ids,
        proposal,
        registry,
        permission_scope,
        snapshots,
        dependencies.dimension_owners,
    )
    accepted_claim_intents, claim_intent_gaps = _bind_claim_intents(
        proposal,
        capabilities,
        metric_bindings,
        registry,
    )
    affected_capabilities = capabilities or ("analysis_contract",)
    resolution = _resolve_advisory_windows(
        target_semantic=str(proposal.get("target_semantic") or "yesterday"),
        baselines=_ordered_values(proposal, "baselines"),
        as_of=as_of,
        timezone_name=registry.business_timezone,
        dataset_watermarks={
            item.dataset_id: date.fromisoformat(item.watermark)
            for item in snapshots
        },
        affected_capabilities=affected_capabilities,
        affected_claim_types=accepted_claim_intents,
    )
    analysis_contract_id = f"analysis:{run_id}:1"
    query_contracts, query_refs_by_capability = _build_query_contracts(
        run_id,
        analysis_contract_id,
        capabilities,
        proposal,
        snapshots,
        resolution.windows,
        metric_bindings,
        dimension_bindings,
        registry,
        permission_scope,
    )
    capability_plans = _build_capability_plans(
        capabilities,
        query_contracts,
        query_refs_by_capability,
        registry,
    )
    capability_input_gaps = _reconcile_capability_inputs(
        capabilities,
        proposal,
        resolution.windows,
        dimension_bindings,
        capability_plans,
        registry,
    )
    scoped_gaps = _scope_gaps(
        (
            *capability_gaps,
            *dataset_contract_gaps,
            *source_gaps,
            *dataset_schema_gaps,
            *metric_gaps,
            *dimension_gaps,
            *capability_input_gaps,
        ),
        affected_capabilities=affected_capabilities,
        affected_claim_types=accepted_claim_intents,
    )
    gaps = (
        *scoped_gaps,
        *claim_intent_gaps,
        *resolution.gaps,
    )
    target_metrics = set(_values(proposal, "target_metrics"))
    analysis = AnalysisContract(
        analysis_contract_id=analysis_contract_id,
        contract_version=registry.contract_version,
        question_families=_values(proposal, "question_families"),
        target_metric_refs=tuple(
            binding.contract_ref
            for binding in metric_bindings
            if binding.metric_id in target_metrics
        ),
        claim_intents=accepted_claim_intents,
        scope=_scope(proposal),
        business_timezone=registry.business_timezone,
        as_of=as_of.isoformat(),
        resolved_windows=resolution.windows,
        metric_bindings=metric_bindings,
        dimension_bindings=dimension_bindings,
        dataset_requirements=required_dataset_ids,
        capability_requirements=capabilities,
        permission_scope=permission_scope,
        contract_gaps=tuple(gaps),
    )
    return AnalysisCompileOutcome(analysis, query_contracts, capability_plans)


def _required_metric_ids(
    proposal: Mapping[str, Any],
    accepted_capabilities: tuple[str, ...],
    registry: RuntimeContractRegistry,
) -> tuple[str, ...]:
    metric_ids = list(_values(proposal, "target_metrics"))
    requested_components = _values(proposal, "requested_components")
    metric_ids.extend(requested_components)
    explicitly_requested = set(metric_ids)
    for capability_id in accepted_capabilities:
        contract = _registry_entry(registry.capability_inputs, capability_id)
        if contract is None:
            continue
        metric_ids.extend(_mapping_values(contract, "required_metrics"))
        metric_ids.extend(
            metric_id
            for metric_id in _mapping_values(contract, "optional_metrics")
            if metric_id in explicitly_requested
        )
    return _dedupe(metric_ids)


def _build_dependency_index(
    proposal: Mapping[str, Any],
    accepted_capabilities: tuple[str, ...],
    registry: RuntimeContractRegistry,
) -> _DependencyIndex:
    metric_ids = _required_metric_ids(proposal, accepted_capabilities, registry)
    dimension_ids = _values(proposal, "requested_dimensions")
    explicit_metrics = set(
        (*_values(proposal, "target_metrics"), *_values(proposal, "requested_components"))
    )
    target_metrics = set(_values(proposal, "target_metrics"))

    metric_owners: dict[str, list[str]] = {metric_id: [] for metric_id in metric_ids}
    for metric_id in target_metrics:
        _append_owner(metric_owners, metric_id, "analysis_contract")
    for capability_id in accepted_capabilities:
        contract = _registry_entry(registry.capability_inputs, capability_id)
        if contract is None:
            continue
        for metric_id in _mapping_values(contract, "required_metrics"):
            if metric_id in metric_owners:
                _append_owner(metric_owners, metric_id, capability_id)
        for metric_id in _mapping_values(contract, "optional_metrics"):
            if metric_id in explicit_metrics and metric_id in metric_owners:
                _append_owner(metric_owners, metric_id, capability_id)
    for metric_id in metric_ids:
        if not metric_owners[metric_id]:
            _append_owner(metric_owners, metric_id, "analysis_contract")

    dimension_owners: dict[str, list[str]] = {
        dimension_id: [] for dimension_id in dimension_ids
    }
    for capability_id in accepted_capabilities:
        contract = _registry_entry(registry.capability_inputs, capability_id)
        if contract is None or str(contract.get("dimension_mode") or "") != "requested":
            continue
        for dimension_id in dimension_ids:
            _append_owner(dimension_owners, dimension_id, capability_id)
    for dimension_id in dimension_ids:
        if not dimension_owners[dimension_id]:
            _append_owner(dimension_owners, dimension_id, "analysis_contract")

    requested_sources = _values(proposal, "requested_context_sources")
    source_owners: dict[str, list[str]] = {
        dataset_id: [] for dataset_id in requested_sources
    }
    for capability_id in accepted_capabilities:
        contract = _registry_entry(registry.capability_inputs, capability_id)
        if (
            contract is None
            or str(contract.get("source_mode") or "")
            != "requested_context_sources"
        ):
            continue
        for dataset_id in requested_sources:
            _append_owner(source_owners, dataset_id, capability_id)
    for dataset_id in requested_sources:
        if not source_owners[dataset_id]:
            _append_owner(source_owners, dataset_id, "analysis_contract")

    dataset_owners: dict[str, list[str]] = {}
    for metric_id in metric_ids:
        contract = _registry_entry(registry.metric, metric_id)
        if contract is not None and contract.get("dataset_id"):
            dataset_id = str(contract["dataset_id"])
            for owner in metric_owners[metric_id]:
                _append_owner(dataset_owners, dataset_id, owner)
    for dimension_id in dimension_ids:
        contract = _registry_entry(registry.dimension, dimension_id)
        if contract is not None and contract.get("dataset_id"):
            dataset_id = str(contract["dataset_id"])
            for owner in dimension_owners[dimension_id]:
                _append_owner(dataset_owners, dataset_id, owner)
    for dataset_id, owners in source_owners.items():
        for owner in owners:
            _append_owner(dataset_owners, dataset_id, owner)

    return _DependencyIndex(
        metric_ids=metric_ids,
        dimension_ids=dimension_ids,
        dataset_ids=tuple(dataset_owners),
        metric_owners={key: tuple(value) for key, value in metric_owners.items()},
        dimension_owners={key: tuple(value) for key, value in dimension_owners.items()},
        dataset_owners={key: tuple(value) for key, value in dataset_owners.items()},
    )


def _validate_dataset_contracts(
    required_dataset_ids: tuple[str, ...],
    registry: RuntimeContractRegistry,
    dataset_owners: Mapping[str, tuple[str, ...]],
) -> tuple[tuple[str, ...], tuple[ContractGap, ...]]:
    executable = []
    gaps = []
    for dataset_id in required_dataset_ids:
        contract = _registry_entry(registry.dataset, dataset_id)
        if contract is None:
            executable.append(dataset_id)
            continue

        raw_required_fields = contract.get("required_fields")
        required_fields_valid = (
            isinstance(raw_required_fields, Iterable)
            and not isinstance(raw_required_fields, (str, bytes, Mapping))
            and bool(raw_required_fields)
            and all(
                isinstance(field, str) and bool(field.strip())
                for field in raw_required_fields
            )
        )
        date_values = {
            key: contract.get(key)
            for key in ("date_field", "date_expression")
            if key in contract
        }
        valid_date_sources = tuple(
            key
            for key, value in date_values.items()
            if isinstance(value, str) and bool(value.strip())
        )
        invalid_present_date_sources = tuple(
            key
            for key, value in date_values.items()
            if not isinstance(value, str) or not value.strip()
        )

        issues = []
        if not required_fields_valid:
            issues.append("required_fields")
        if len(valid_date_sources) != 1:
            issues.append("date_source_one_of")
        if invalid_present_date_sources:
            issues.append("date_source_empty_or_invalid")
        if not issues:
            executable.append(dataset_id)
            continue

        gaps.append(
            _contract_gap(
                gap_type="contract_partial",
                gap_id=(
                    f"dataset:{dataset_id}:contract_partial:"
                    f"{','.join(issues)}"
                ),
                dataset_id=dataset_id,
                affected_capabilities=dataset_owners.get(
                    dataset_id,
                    ("analysis_contract",),
                ),
                owner="contract_owner",
                repair_options=("complete_dataset_contract",),
            )
        )
    return tuple(executable), tuple(gaps)


def _resolve_snapshots(
    required_dataset_ids: tuple[str, ...],
    catalog: DatasetCatalog,
    registry: RuntimeContractRegistry,
    as_of: datetime,
    permission_scope: str,
    dataset_owners: Mapping[str, tuple[str, ...]],
) -> tuple[tuple[DatasetSnapshot, ...], tuple[ContractGap, ...]]:
    snapshots = []
    gaps = []
    for dataset_id in required_dataset_ids:
        affected_capabilities = dataset_owners.get(
            dataset_id,
            ("analysis_contract",),
        )
        if _registry_entry(registry.dataset, dataset_id) is None:
            gaps.append(
                _contract_gap(
                    gap_type="contract_absent",
                    gap_id=f"dataset:{dataset_id}:contract_absent",
                    dataset_id=dataset_id,
                    affected_capabilities=affected_capabilities,
                    repair_options=("register_dataset_contract",),
                )
            )
            continue
        try:
            snapshots.append(
                catalog.resolve(
                    dataset_id,
                    as_of=as_of,
                    permission_scope=permission_scope,
                )
            )
        except KeyError:
            eligible_candidates = catalog.as_of_candidates(dataset_id, as_of=as_of)
            permission_blocked = bool(eligible_candidates) and not any(
                permission_scope in item.permission_scopes
                for _, item in eligible_candidates
            )
            gap_type = "permission_blocked" if permission_blocked else "source_unbound"
            repair_options = (
                ("request_permission", "use_permitted_aggregate")
                if permission_blocked
                else ("register_dataset_snapshot", "bind_source")
            )
            gaps.append(
                _contract_gap(
                    gap_type=gap_type,
                    gap_id=f"dataset:{dataset_id}:{gap_type}",
                    dataset_id=dataset_id,
                    affected_capabilities=affected_capabilities,
                    owner="permission_owner" if permission_blocked else "data_owner",
                    repair_options=repair_options,
                    requires_clarification=permission_blocked,
                )
            )
    return tuple(snapshots), tuple(gaps)


def _validate_snapshot_schemas(
    snapshots: tuple[DatasetSnapshot, ...],
    registry: RuntimeContractRegistry,
    dataset_owners: Mapping[str, tuple[str, ...]],
) -> tuple[tuple[DatasetSnapshot, ...], tuple[ContractGap, ...]]:
    accepted = []
    gaps = []
    for snapshot in snapshots:
        dataset_contract = _registry_entry(registry.dataset, snapshot.dataset_id)
        if dataset_contract is None:
            continue
        required_fields = _mapping_values(dataset_contract, "required_fields")
        missing_fields = tuple(
            field for field in required_fields if field not in snapshot.schema_fields
        )
        if not missing_fields:
            accepted.append(snapshot)
            continue
        gaps.append(
            _contract_gap(
                gap_type="contract_partial",
                gap_id=(
                    f"dataset:{snapshot.dataset_id}:schema_missing:"
                    f"{','.join(missing_fields)}"
                ),
                dataset_id=snapshot.dataset_id,
                affected_capabilities=dataset_owners.get(
                    snapshot.dataset_id,
                    ("analysis_contract",),
                ),
                owner="data_owner",
                repair_options=("refresh_snapshot_schema", "repair_dataset_binding"),
            )
        )
    return tuple(accepted), tuple(gaps)


def _bind_metrics(
    metric_ids: tuple[str, ...],
    registry: RuntimeContractRegistry,
    snapshots: tuple[DatasetSnapshot, ...],
    metric_owners: Mapping[str, tuple[str, ...]],
) -> tuple[tuple[MetricBinding, ...], tuple[ContractGap, ...]]:
    snapshots_by_dataset = {item.dataset_id: item for item in snapshots}
    bindings = []
    gaps = []
    for metric_id in metric_ids:
        affected_capabilities = metric_owners.get(metric_id, ("analysis_contract",))
        contract = _registry_entry(registry.metric, metric_id)
        if contract is None:
            gaps.append(
                _contract_gap(
                    gap_type="contract_absent",
                    gap_id=f"metric:{metric_id}:contract_absent",
                    affected_capabilities=affected_capabilities,
                    repair_options=("register_metric_contract", "remove_metric_path"),
                )
            )
            continue
        required_keys = (
            "contract_ref",
            "dataset_id",
            "expression",
            "aggregation",
            "required_fields",
            "grain",
        )
        missing = tuple(key for key in required_keys if key not in contract)
        if missing:
            gaps.append(
                _contract_gap(
                    gap_type="contract_partial",
                    gap_id=f"metric:{metric_id}:missing:{','.join(missing)}",
                    dataset_id=str(contract.get("dataset_id") or ""),
                    affected_capabilities=affected_capabilities,
                    repair_options=("complete_metric_contract", "remove_metric_path"),
                )
            )
            continue
        dataset_id = str(contract["dataset_id"])
        snapshot = snapshots_by_dataset.get(dataset_id)
        required_fields = _mapping_values(contract, "required_fields")
        missing_fields = (
            tuple(field for field in required_fields if field not in snapshot.schema_fields)
            if snapshot is not None
            else ()
        )
        if missing_fields:
            gaps.append(
                _contract_gap(
                    gap_type="contract_partial",
                    gap_id=(
                        f"metric:{metric_id}:schema_missing:"
                        f"{','.join(missing_fields)}"
                    ),
                    dataset_id=dataset_id,
                    affected_capabilities=affected_capabilities,
                    owner="data_owner",
                    repair_options=("refresh_snapshot_schema", "repair_metric_binding"),
                )
            )
            continue
        reconciliation_tolerance = _reconciliation_tolerance(contract)
        if reconciliation_tolerance is None:
            gaps.append(
                _contract_gap(
                    gap_type="contract_partial",
                    gap_id=f"metric:{metric_id}:invalid:reconciliation_tolerance",
                    dataset_id=dataset_id,
                    affected_capabilities=affected_capabilities,
                    repair_options=("repair_metric_binding",),
                )
            )
            continue
        reconciliation_strategy = _reconciliation_strategy(
            contract,
            reconciliation_tolerance,
        )
        if reconciliation_strategy is None:
            gaps.append(
                _contract_gap(
                    gap_type="contract_partial",
                    gap_id=f"metric:{metric_id}:invalid:reconciliation_strategy",
                    dataset_id=dataset_id,
                    affected_capabilities=affected_capabilities,
                    repair_options=("repair_metric_binding",),
                )
            )
            continue
        bindings.append(
            MetricBinding(
                metric_id=metric_id,
                contract_ref=str(contract["contract_ref"]),
                dataset_id=dataset_id,
                expression=str(contract["expression"]),
                aggregation=str(contract["aggregation"]),
                required_fields=required_fields,
                grain=_mapping_values(contract, "grain"),
                numerator_metric=str(contract.get("numerator_metric") or ""),
                denominator_metric=str(contract.get("denominator_metric") or ""),
                zero_denominator_policy=str(
                    contract.get("zero_denominator_policy") or "null"
                ),
                claim_types=_mapping_values(contract, "claim_types"),
                reconciliation_tolerance=reconciliation_tolerance,
                reconciliation_strategy=reconciliation_strategy,
            )
        )
    return tuple(bindings), tuple(gaps)


def _reconciliation_tolerance(contract: Mapping[str, Any]) -> float | None:
    value = contract.get("reconciliation_tolerance", 0.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    tolerance = float(value)
    if not math.isfinite(tolerance) or tolerance < 0:
        return None
    return tolerance


def _reconciliation_strategy(
    contract: Mapping[str, Any],
    tolerance: float,
) -> str | None:
    strategy = str(
        contract.get("reconciliation_strategy")
        or "unsupported_non_additive"
    )
    if strategy not in {
        "additive_sum",
        "exact_additive_count",
        "ratio_from_components",
        "unsupported_non_additive",
    }:
        return None
    if strategy == "exact_additive_count" and tolerance != 0:
        return None
    if strategy == "ratio_from_components" and (
        not str(contract.get("numerator_metric") or "")
        or not str(contract.get("denominator_metric") or "")
    ):
        return None
    return strategy


def _bind_dimensions(
    dimension_ids: tuple[str, ...],
    proposal: Mapping[str, Any],
    registry: RuntimeContractRegistry,
    permission_scope: str,
    snapshots: tuple[DatasetSnapshot, ...],
    dimension_owners: Mapping[str, tuple[str, ...]],
) -> tuple[tuple[DimensionBinding, ...], tuple[ContractGap, ...]]:
    snapshots_by_dataset = {item.dataset_id: item for item in snapshots}
    bindings = []
    gaps = []
    requested_grain = str(proposal.get("grain") or "window_id")
    for dimension_id in dimension_ids:
        affected_capabilities = dimension_owners.get(
            dimension_id,
            ("analysis_contract",),
        )
        contract = _registry_entry(registry.dimension, dimension_id)
        if contract is None:
            gaps.append(
                _contract_gap(
                    gap_type="contract_absent",
                    gap_id=f"dimension:{dimension_id}:contract_absent",
                    affected_capabilities=affected_capabilities,
                    repair_options=("register_dimension_contract", "remove_dimension_path"),
                )
            )
            continue
        required_keys = (
            "contract_ref",
            "dataset_id",
            "source_field",
            "allowed_grains",
        )
        missing = tuple(key for key in required_keys if key not in contract)
        if missing:
            gaps.append(
                _contract_gap(
                    gap_type="contract_partial",
                    gap_id=f"dimension:{dimension_id}:missing:{','.join(missing)}",
                    dataset_id=str(contract.get("dataset_id") or ""),
                    affected_capabilities=affected_capabilities,
                    repair_options=("complete_dimension_contract", "remove_dimension_path"),
                )
            )
            continue
        allowed_grains = _mapping_values(contract, "allowed_grains")
        dataset_id = str(contract["dataset_id"])
        source_field = str(contract["source_field"])
        snapshot = snapshots_by_dataset.get(dataset_id)
        if snapshot is not None and source_field not in snapshot.schema_fields:
            gaps.append(
                _contract_gap(
                    gap_type="contract_partial",
                    gap_id=f"dimension:{dimension_id}:schema_missing:{source_field}",
                    dataset_id=dataset_id,
                    affected_capabilities=affected_capabilities,
                    owner="data_owner",
                    repair_options=(
                        "refresh_snapshot_schema",
                        "repair_dimension_binding",
                    ),
                )
            )
            continue
        if requested_grain not in allowed_grains:
            gaps.append(
                _contract_gap(
                    gap_type="unsupported_grain",
                    gap_id=f"dimension:{dimension_id}:grain:{requested_grain}",
                    dataset_id=dataset_id,
                    affected_capabilities=affected_capabilities,
                    repair_options=("use_supported_grain", "remove_dimension_path"),
                    requires_clarification=True,
                )
            )
        bindings.append(
            DimensionBinding(
                dimension_id=dimension_id,
                contract_ref=str(contract["contract_ref"]),
                dataset_id=dataset_id,
                source_field=source_field,
                allowed_grains=allowed_grains,
                null_bucket=str(contract.get("null_bucket") or "Unknown"),
                permission_scope=permission_scope,
            )
        )
    return tuple(bindings), tuple(gaps)


def _bind_claim_intents(
    proposal: Mapping[str, Any],
    accepted_capabilities: tuple[str, ...],
    metric_bindings: tuple[MetricBinding, ...],
    registry: RuntimeContractRegistry,
) -> tuple[tuple[str, ...], tuple[ContractGap, ...]]:
    explicit = _values(proposal, "claim_intents")
    capability_inferred = []
    for capability_id in accepted_capabilities:
        capability_contract = _registry_entry(
            registry.capability_inputs,
            capability_id,
        )
        if capability_contract is not None:
            capability_inferred.extend(
                _mapping_values(capability_contract, "supported_claim_types")
            )
    capability_ceiling = _dedupe(capability_inferred)
    if explicit:
        supported = tuple(
            claim_intent
            for claim_intent in explicit
            if claim_intent in capability_ceiling
        )
        unsupported = tuple(
            claim_intent
            for claim_intent in explicit
            if claim_intent not in capability_ceiling
        )
        gaps = tuple(
            ContractGap(
                gap_type="contract_partial",
                gap_id=f"claim_intent:{claim_intent}:unsupported",
                affected_capabilities=(
                    accepted_capabilities or ("analysis_contract",)
                ),
                affected_claim_types=(claim_intent,),
                owner="contract_owner",
                repair_options=(
                    "choose_supported_claim_intent",
                    "clarify_claim_intent",
                ),
                requires_clarification=True,
            )
            for claim_intent in unsupported
        )
        return supported or ("unbound_claim_intent",), gaps

    if capability_ceiling:
        return capability_ceiling, ()

    metric_inferred = []
    for binding in metric_bindings:
        metric_inferred.extend(binding.claim_types)
    accepted = _dedupe(metric_inferred)
    if accepted:
        return accepted, ()

    diagnosed = ("unbound_claim_intent",)
    return diagnosed, (
        ContractGap(
            gap_type="contract_partial",
            gap_id="claim_intents:unbound",
            affected_capabilities=accepted_capabilities or ("analysis_contract",),
            affected_claim_types=diagnosed,
            owner="contract_owner",
            repair_options=(
                "bind_capability_claim_types",
                "bind_metric_claim_types",
                "clarify_claim_intent",
            ),
            requires_clarification=True,
        ),
    )


def _resolve_advisory_windows(
    *,
    target_semantic: str,
    baselines: tuple[str, ...],
    as_of: datetime,
    timezone_name: str,
    dataset_watermarks: Mapping[str, date],
    affected_capabilities: tuple[str, ...],
    affected_claim_types: tuple[str, ...],
) -> WindowResolution:
    try:
        return resolve_revenue_windows(
            target_semantic=target_semantic,
            baselines=baselines,
            as_of=as_of,
            timezone_name=timezone_name,
            dataset_watermarks=dataset_watermarks,
            affected_capabilities=affected_capabilities,
            affected_claim_types=affected_claim_types,
        )
    except ValueError as exc:
        reason = str(exc)
        error_type = reason.partition(":")[0]
        if error_type not in {
            "unsupported_target_semantic",
            "unsupported_baseline",
            "duplicate_baseline",
        }:
            raise
        return WindowResolution(
            windows=(),
            gaps=(
                ContractGap(
                    gap_type="contract_partial",
                    gap_id=f"window:{reason}",
                    affected_capabilities=affected_capabilities,
                    affected_claim_types=affected_claim_types,
                    owner="contract_owner",
                    repair_options=(
                        "choose_supported_window",
                        "clarify_window_contract",
                    ),
                    requires_clarification=True,
                ),
            ),
        )


def _capability_contract_gaps(
    accepted_capabilities: tuple[str, ...],
    registry: RuntimeContractRegistry,
) -> tuple[ContractGap, ...]:
    gaps = []
    required_plan_fields = ("minimum_readiness", "degradation_policy")
    for capability_id in accepted_capabilities:
        contract = _registry_entry(registry.capability_inputs, capability_id)
        if contract is None:
            gaps.append(
                _contract_gap(
                    gap_type="contract_absent",
                    gap_id=f"capability:{capability_id}:contract_absent",
                    affected_capabilities=(capability_id,),
                    repair_options=("register_capability_inputs", "remove_capability"),
                )
            )
            continue
        missing = tuple(field for field in required_plan_fields if field not in contract)
        if missing:
            gaps.append(
                _contract_gap(
                    gap_type="contract_partial",
                    gap_id=f"capability:{capability_id}:missing:{','.join(missing)}",
                    affected_capabilities=(capability_id,),
                    repair_options=("complete_capability_input_contract",),
                )
            )
        for query_family in _mapping_values(contract, "query_families"):
            query_shape = _registry_entry(registry.query_shape, query_family)
            required_shape_fields = ("required_fields", "unique_key", "grain")
            if query_shape is None:
                gaps.append(
                    _contract_gap(
                        gap_type="contract_absent",
                        gap_id=(
                            f"capability:{capability_id}:query_shape:"
                            f"{query_family}:contract_absent"
                        ),
                        affected_capabilities=(capability_id,),
                        repair_options=("register_query_shape", "remove_query_family"),
                    )
                )
                continue
            missing_shape_fields = tuple(
                field
                for field in required_shape_fields
                if not _mapping_values(query_shape, field)
            )
            if missing_shape_fields:
                gaps.append(
                    _contract_gap(
                        gap_type="contract_partial",
                        gap_id=(
                            f"capability:{capability_id}:query_shape:"
                            f"{query_family}:missing:"
                            f"{','.join(missing_shape_fields)}"
                        ),
                        affected_capabilities=(capability_id,),
                        repair_options=("complete_query_shape", "remove_query_family"),
                    )
                )
    return tuple(gaps)


def _build_query_contracts(
    run_id: str,
    analysis_contract_id: str,
    accepted_capabilities: tuple[str, ...],
    proposal: Mapping[str, Any],
    snapshots: tuple[DatasetSnapshot, ...],
    windows: tuple[ResolvedWindow, ...],
    metric_bindings: tuple[MetricBinding, ...],
    dimension_bindings: tuple[DimensionBinding, ...],
    registry: RuntimeContractRegistry,
    permission_scope: str,
) -> tuple[tuple[QueryContract, ...], dict[str, tuple[str, ...]]]:
    if not windows:
        return (), {
            capability_id: () for capability_id in accepted_capabilities
        }
    snapshot_by_dataset = {item.dataset_id: item for item in snapshots}
    metric_by_id = {item.metric_id: item for item in metric_bindings}
    filters = _filters(proposal)
    window_refs = tuple(item.window_id for item in windows)
    logical_queries: list[dict[str, Any]] = []
    query_owners: list[set[str]] = []
    seen: dict[str, int] = {}

    def record_logical(logical: dict[str, Any], owner: str) -> None:
        dedupe_key = query_contract_signature(logical)
        existing_index = seen.get(dedupe_key)
        if existing_index is None:
            seen[dedupe_key] = len(logical_queries)
            logical_queries.append(logical)
            query_owners.append({owner})
            return
        query_owners[existing_index].add(owner)

    for capability_id in accepted_capabilities:
        capability = _registry_entry(registry.capability_inputs, capability_id)
        if capability is None:
            continue
        optional_metrics = set(_mapping_values(capability, "optional_metrics"))
        family_metrics = capability.get("query_family_metrics")
        if not isinstance(family_metrics, Mapping):
            family_metrics = {}
        for query_family in _mapping_values(capability, "query_families"):
            query_shape = _registry_entry(registry.query_shape, query_family)
            if query_shape is None:
                continue
            configured_metrics = family_metrics.get(query_family)
            if configured_metrics is None:
                metric_ids = (
                    *_mapping_values(capability, "required_metrics"),
                    *(
                        metric_id
                        for metric_id in optional_metrics
                        if metric_id in metric_by_id
                    ),
                )
            else:
                metric_ids = _sequence_values(configured_metrics)
            selected_metrics = tuple(
                metric_by_id[metric_id]
                for metric_id in _dedupe(metric_ids)
                if metric_id in metric_by_id
            )
            by_dataset: dict[str, list[MetricBinding]] = {}
            for binding in selected_metrics:
                by_dataset.setdefault(binding.dataset_id, []).append(binding)
            if not by_dataset and capability.get("source_mode") == "requested_context_sources":
                by_dataset = {
                    dataset_id: []
                    for dataset_id in _values(proposal, "requested_context_sources")
                }

            for dataset_id, dataset_metrics in by_dataset.items():
                snapshot = snapshot_by_dataset.get(dataset_id)
                if snapshot is None:
                    continue
                normalized_metrics = tuple(
                    sorted(dataset_metrics, key=lambda item: item.metric_id)
                )
                include_dimensions = str(capability.get("dimension_mode") or "") == "requested"
                requested_dimensions = tuple(
                    sorted(
                        (
                            item
                            for item in dimension_bindings
                            if include_dimensions and item.dataset_id == dataset_id
                        ),
                        key=lambda item: item.dimension_id,
                    )
                )
                if include_dimensions and not requested_dimensions:
                    continue
                dimension_topology = str(
                    query_shape.get("dimension_topology") or "joint"
                )
                if dimension_topology == "independent":
                    dimension_groups = tuple(
                        (dimension,) for dimension in requested_dimensions
                    )
                elif dimension_topology == "joint":
                    dimension_groups = (requested_dimensions,)
                else:
                    continue
                for query_dimensions in dimension_groups:
                    result_shape = _result_shape(
                        normalized_metrics,
                        query_dimensions,
                        window_refs,
                        query_shape,
                    )
                    logical: dict[str, Any] = {
                        "analysis_contract_ref": analysis_contract_id,
                        "query_intent": query_family,
                        "dataset_snapshot_refs": (snapshot.snapshot_ref,),
                        "metric_bindings": normalized_metrics,
                        "dimension_bindings": query_dimensions,
                        "window_refs": window_refs,
                        "resolved_windows": windows,
                        "filters": filters,
                        "result_shape": result_shape,
                        "completeness_assertions": (
                            "required_fields_present",
                            "required_windows_complete",
                            "unique_result_grain",
                            "source_snapshot_matches_contract",
                        ),
                        "permission_scope": permission_scope,
                        "workload_class": str(
                            capability.get("workload_class")
                            or "interactive_aggregate"
                        ),
                        "query_parameters": _query_parameters(query_shape),
                        "query_role_ref": "",
                        "reconciliation_binding": None,
                        "join_expectation": _join_expectation(query_shape),
                    }
                    if query_dimensions:
                        companion_shape_contract = _registry_entry(
                            registry.query_shape,
                            "daily_metric_baselines",
                        )
                        if companion_shape_contract is None:
                            continue
                        companion = {
                            **logical,
                            "query_intent": "daily_metric_baselines",
                            "dimension_bindings": (),
                            "result_shape": _result_shape(
                                normalized_metrics,
                                (),
                                window_refs,
                                companion_shape_contract,
                            ),
                            "query_parameters": _query_parameters(
                                companion_shape_contract
                            ),
                            "reconciliation_binding": None,
                            "join_expectation": _join_expectation(
                                companion_shape_contract
                            ),
                        }
                        companion_signature = query_contract_signature(companion)
                        companion_role_ref = f"query-role:{companion_signature}"
                        companion["query_role_ref"] = companion_role_ref
                        record_logical(companion, capability_id)
                        logical["reconciliation_binding"] = ReconciliationBinding(
                            reference_query_role_ref=companion_role_ref,
                            reference_contract_signature=companion_signature,
                        )
                    logical_signature = query_contract_signature(logical)
                    logical["query_role_ref"] = f"query-role:{logical_signature}"
                    record_logical(logical, capability_id)

    contracts = []
    refs_by_capability: dict[str, list[str]] = {
        capability_id: [] for capability_id in accepted_capabilities
    }
    for index, logical in enumerate(logical_queries, start=1):
        query_contract_id = f"query:{run_id}:{index}"
        contracts.append(
            QueryContract(
                query_contract_id=query_contract_id,
                contract_signature=query_contract_signature(logical),
                **logical,
            )
        )
        for capability_id in sorted(query_owners[index - 1]):
            refs_by_capability[capability_id].append(query_contract_id)
    return (
        tuple(contracts),
        {
            capability_id: tuple(query_refs)
            for capability_id, query_refs in refs_by_capability.items()
        },
    )


def _build_capability_plans(
    accepted_capabilities: tuple[str, ...],
    query_contracts: tuple[QueryContract, ...],
    query_refs_by_capability: Mapping[str, tuple[str, ...]],
    registry: RuntimeContractRegistry,
) -> tuple[CapabilityExecutionPlan, ...]:
    plans = []
    for capability_id in accepted_capabilities:
        contract = _registry_entry(registry.capability_inputs, capability_id)
        if contract is None:
            continue
        minimum_readiness = contract.get("minimum_readiness")
        degradation_policy = contract.get("degradation_policy")
        if not isinstance(minimum_readiness, Mapping) or not isinstance(
            degradation_policy,
            Mapping,
        ):
            continue
        optional_families = set(_mapping_values(contract, "optional_query_families"))
        required_windows = _mapping_values(contract, "required_windows")
        owned_query_refs = set(query_refs_by_capability.get(capability_id, ()))
        accepted_completeness = _mapping_values(
            minimum_readiness,
            "accepted_completeness",
        )
        required_slots = []
        optional_slots = []
        for query_family in _mapping_values(contract, "query_families"):
            primary_queries = tuple(
                query
                for query in query_contracts
                if query.query_contract_id in owned_query_refs
                and query.query_intent == query_family
            )
            required = query_family not in optional_families
            slot_primaries: tuple[QueryContract | None, ...] = (
                primary_queries or (None,)
            )
            for primary in slot_primaries:
                companion_role = (
                    primary.reconciliation_binding.reference_query_role_ref
                    if primary is not None
                    and primary.reconciliation_binding is not None
                    else ""
                )
                validation_queries = tuple(
                    query
                    for query in query_contracts
                    if query.query_contract_id in owned_query_refs
                    and companion_role
                    and query.query_role_ref == companion_role
                )
                dimension_suffix = "+".join(
                    item.dimension_id
                    for item in (
                        primary.dimension_bindings if primary is not None else ()
                    )
                )
                slot_id = (
                    query_family
                    if len(slot_primaries) == 1
                    else (
                        f"{query_family}:"
                        f"{dimension_suffix or primary.query_contract_id}"
                    )
                )
                slot = CapabilityInputSlot(
                    slot_id=slot_id,
                    query_contract_refs=(
                        (primary.query_contract_id,)
                        if primary is not None
                        else ()
                    ),
                    required=required,
                    accepted_completeness=accepted_completeness,
                    required_fields=(
                        tuple(primary.result_shape.required_fields)
                        if primary is not None
                        else ()
                    ),
                    required_window_ids=_dedupe(
                        required_windows
                        or (
                            primary.result_shape.required_window_ids
                            if primary is not None
                            else ()
                        )
                    ),
                    validation_query_contract_refs=tuple(
                        query.query_contract_id for query in validation_queries
                    ),
                )
                (required_slots if required else optional_slots).append(slot)
        capability_ref = str(
            contract.get("contract_ref")
            or f"{registry.source_ref}#capability_inputs.{capability_id}"
        )
        plans.append(
            CapabilityExecutionPlan(
                capability_id=capability_id,
                capability_contract_ref=capability_ref,
                required_input_slots=tuple(required_slots),
                optional_input_slots=tuple(optional_slots),
                merge_strategy=str(contract.get("merge_strategy") or "by_query_family"),
                minimum_readiness=dict(minimum_readiness),
                degradation_policy=dict(degradation_policy),
                supported_evidence_types=_mapping_values(
                    contract,
                    "supported_evidence_types",
                ),
                maximum_claim_strength=str(
                    contract.get("maximum_claim_strength") or ""
                ),
            )
        )
    return tuple(plans)


def _reconcile_capability_inputs(
    accepted_capabilities: tuple[str, ...],
    proposal: Mapping[str, Any],
    windows: tuple[ResolvedWindow, ...],
    dimension_bindings: tuple[DimensionBinding, ...],
    capability_plans: tuple[CapabilityExecutionPlan, ...],
    registry: RuntimeContractRegistry,
) -> tuple[ContractGap, ...]:
    available_windows = {window.window_id for window in windows}
    plan_by_capability = {
        plan.capability_id: plan for plan in capability_plans
    }
    gaps: dict[str, ContractGap] = {}
    for capability_id in accepted_capabilities:
        contract = _registry_entry(registry.capability_inputs, capability_id)
        if contract is None:
            continue
        for window_id in _mapping_values(contract, "required_windows"):
            if window_id not in available_windows:
                gap = _contract_gap(
                    gap_type="contract_partial",
                    gap_id=(
                        f"capability:{capability_id}:required_window:"
                        f"{window_id}:unbound"
                    ),
                    affected_capabilities=(capability_id,),
                    repair_options=(
                        "add_required_window",
                        "remove_capability_path",
                        "clarify_window_contract",
                    ),
                    requires_clarification=True,
                )
                gaps[gap.gap_id] = gap
        if (
            str(contract.get("source_mode") or "")
            == "requested_context_sources"
            and not _values(proposal, "requested_context_sources")
        ):
            gap = _contract_gap(
                gap_type="contract_partial",
                gap_id=f"capability:{capability_id}:required_context_source:unbound",
                affected_capabilities=(capability_id,),
                repair_options=(
                    "bind_context_source",
                    "remove_capability_path",
                    "clarify_context_source",
                ),
                requires_clarification=True,
            )
            gaps[gap.gap_id] = gap
        if (
            str(contract.get("dimension_mode") or "") == "requested"
            and not dimension_bindings
        ):
            gap = _contract_gap(
                gap_type="contract_partial",
                gap_id=f"capability:{capability_id}:required_dimension:unbound",
                affected_capabilities=(capability_id,),
                repair_options=(
                    "bind_supported_dimension",
                    "remove_capability_path",
                    "clarify_dimension",
                ),
                requires_clarification=True,
            )
            gaps[gap.gap_id] = gap

        plan = plan_by_capability.get(capability_id)
        if plan is None:
            continue
        for slot in plan.required_input_slots:
            if slot.query_contract_refs:
                continue
            gap = _contract_gap(
                gap_type="contract_partial",
                gap_id=(
                    f"capability:{capability_id}:required_query:"
                    f"{slot.slot_id}:unbound"
                ),
                affected_capabilities=(capability_id,),
                repair_options=(
                    "bind_required_query_contract",
                    "repair_source_or_contract_inputs",
                    "remove_capability_path",
                ),
            )
            gaps[gap.gap_id] = gap
    return tuple(gaps.values())


def _result_shape(
    metric_bindings: tuple[MetricBinding, ...],
    dimension_bindings: tuple[DimensionBinding, ...],
    window_refs: tuple[str, ...],
    query_shape: Mapping[str, Any],
) -> ResultShape:
    dimension_ids = tuple(item.dimension_id for item in dimension_bindings)
    required_fields = _dedupe(
        (
            *_mapping_values(query_shape, "required_fields"),
            *(item.metric_id for item in metric_bindings),
            *dimension_ids,
        )
    )
    unique_key = _dedupe(
        (*_mapping_values(query_shape, "unique_key"), *dimension_ids)
    )
    grain = _dedupe(
        (*_mapping_values(query_shape, "grain"), *dimension_ids)
    )
    return ResultShape(
        required_fields=required_fields,
        unique_key=unique_key,
        grain=grain,
        required_window_ids=window_refs,
        result_semantics=str(
            query_shape.get("result_semantics") or "complete_aggregate"
        ),
    )


def _scope_gaps(
    gaps: Iterable[ContractGap],
    *,
    affected_capabilities: tuple[str, ...],
    affected_claim_types: tuple[str, ...],
) -> tuple[ContractGap, ...]:
    return tuple(
        replace(
            gap,
            affected_capabilities=(
                gap.affected_capabilities or affected_capabilities
            ),
            affected_claim_types=(
                gap.affected_claim_types or affected_claim_types
            ),
        )
        for gap in gaps
    )


def _contract_gap(
    *,
    gap_type: str,
    gap_id: str,
    dataset_id: str = "",
    affected_capabilities: tuple[str, ...] = (),
    owner: str = "contract_owner",
    repair_options: tuple[str, ...] = (),
    requires_clarification: bool = False,
) -> ContractGap:
    return ContractGap(
        gap_type=gap_type,
        gap_id=gap_id,
        dataset_id=dataset_id,
        affected_capabilities=affected_capabilities,
        owner=owner,
        repair_options=repair_options,
        requires_clarification=requires_clarification,
    )


def _scope(proposal: Mapping[str, Any]) -> dict[str, Any]:
    value = proposal.get("scope")
    if isinstance(value, Mapping):
        return dict(value)
    if value not in (None, ""):
        return {"type": str(value)}
    return {"type": "full_sample"}


def _filters(proposal: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    value = proposal.get("filters") or ()
    if isinstance(value, Mapping):
        return (dict(value),)
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        return tuple(dict(item) for item in value if isinstance(item, Mapping))
    return ()


def _values(proposal: Mapping[str, Any], key: str) -> tuple[str, ...]:
    return _sequence_values(proposal.get(key) or ())


def _ordered_values(proposal: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = proposal.get(key) or ()
    if isinstance(value, (str, bytes)):
        values = (value,)
    elif isinstance(value, Iterable) and not isinstance(value, Mapping):
        values = value
    else:
        values = ()
    return tuple(str(item).strip() for item in values if str(item).strip())


def _mapping_values(mapping: Mapping[str, Any], key: str) -> tuple[str, ...]:
    return _sequence_values(mapping.get(key) or ())


def _sequence_values(value: Any) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        values = (value,)
    elif isinstance(value, Iterable) and not isinstance(value, Mapping):
        values = value
    else:
        values = ()
    return _dedupe(str(item).strip() for item in values if str(item).strip())


def _dedupe(values: Iterable[Any]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _append_owner(
    owners_by_id: dict[str, list[str]],
    item_id: str,
    owner: str,
) -> None:
    owners = owners_by_id.setdefault(item_id, [])
    if owner not in owners:
        owners.append(owner)


def _registry_entry(accessor: Any, item_id: str) -> dict[str, Any] | None:
    try:
        return accessor(item_id)
    except KeyError:
        return None


def _join_expectation(query_shape: Mapping[str, Any]) -> JoinExpectation | None:
    value = query_shape.get("join_expectation")
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("query_shape_join_expectation_must_be_mapping")
    audit_fields = _mapping_values(value, "audit_fields")
    cardinality = str(value.get("cardinality") or "")
    max_duplicate_keys = value.get("max_duplicate_keys")
    max_unmatched_rows = value.get("max_unmatched_rows")
    if (
        cardinality not in {"one_to_one", "many_to_one"}
        or not audit_fields
        or isinstance(max_duplicate_keys, bool)
        or not isinstance(max_duplicate_keys, int)
        or max_duplicate_keys < 0
        or isinstance(max_unmatched_rows, bool)
        or not isinstance(max_unmatched_rows, int)
        or max_unmatched_rows < 0
    ):
        raise ValueError("invalid_query_shape_join_expectation")
    return JoinExpectation(
        cardinality=cardinality,
        audit_fields=audit_fields,
        max_duplicate_keys=max_duplicate_keys,
        max_unmatched_rows=max_unmatched_rows,
    )


def _query_parameters(query_shape: Mapping[str, Any]) -> dict[str, Any]:
    value = query_shape.get("query_parameters") or {}
    if not isinstance(value, Mapping):
        raise ValueError("query_shape_parameters_must_be_mapping")
    return {
        str(key): _freeze_contract_value(item)
        for key, item in value.items()
    }


def _freeze_contract_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _freeze_contract_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_contract_value(item) for item in value)
    return value
