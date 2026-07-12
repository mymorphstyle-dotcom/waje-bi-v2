from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
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
    analysis_contract_from_dict,
    analysis_contract_signature,
    query_contract_signature,
    stable_contract_signature,
)
from bi_agent.runtime.evidence_authority import canonical_value
from bi_agent.runtime.dataset_catalog import (
    DatasetCatalog,
    DatasetReleaseResolver,
    DatasetSnapshot,
)
from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry
from bi_agent.runtime.window_resolver import WindowResolution, resolve_revenue_windows


@dataclass(frozen=True)
class AnalysisCompileOutcome:
    analysis_contract: AnalysisContract
    query_contracts: tuple[QueryContract, ...]
    capability_plans: tuple[CapabilityExecutionPlan, ...]


@dataclass(frozen=True)
class CapabilityDependencySet:
    capability_id: str
    metric_ids: tuple[str, ...]
    dimension_ids: tuple[str, ...]
    dataset_ids: tuple[str, ...]
    context_source_ids: tuple[str, ...]


@dataclass(frozen=True)
class _DependencyIndex:
    metric_ids: tuple[str, ...]
    dimension_ids: tuple[str, ...]
    dataset_ids: tuple[str, ...]
    metric_owners: Mapping[str, tuple[str, ...]]
    dimension_owners: Mapping[str, tuple[str, ...]]
    dataset_owners: Mapping[str, tuple[str, ...]]
    metric_dataset_ids: Mapping[str, tuple[str, ...]]
    dimension_dataset_ids: Mapping[str, tuple[str, ...]]
    source_selection_gaps: tuple[ContractGap, ...]


def compile_analysis_contract(
    *,
    run_id: str,
    proposal: Mapping[str, Any],
    accepted_capabilities: Iterable[str],
    catalog: DatasetCatalog,
    registry: RuntimeContractRegistry,
    as_of: datetime,
    permission_scope: str,
    release_resolver: DatasetReleaseResolver | None = None,
) -> AnalysisCompileOutcome:
    capabilities = _dedupe(accepted_capabilities)
    dependencies = _build_dependency_index(proposal, capabilities, registry)
    capability_dependencies = _capability_dependency_sets(
        proposal, capabilities, dependencies
    )
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
        release_resolver,
    )
    snapshots, dataset_schema_gaps = _validate_snapshot_schemas(
        snapshots,
        registry,
        dependencies.dataset_owners,
    )
    snapshot_evidence_gaps = _snapshot_evidence_gaps(
        snapshots,
        dependencies.dataset_owners,
        registry,
    )
    metric_bindings, metric_gaps = _bind_metrics(
        dependencies.metric_ids,
        registry,
        snapshots,
        dependencies.metric_owners,
        dependencies.metric_dataset_ids,
    )
    dimension_bindings, dimension_gaps = _bind_dimensions(
        dependencies.dimension_ids,
        proposal,
        registry,
        permission_scope,
        snapshots,
        dependencies.dimension_owners,
        dependencies.dimension_dataset_ids,
    )
    accepted_terminal_gaps, clarification_outcome_ref = (
        _accepted_terminal_gap_authority(proposal)
    )
    contract_capabilities = _dedupe(
        (
            *capabilities,
            *(
                capability
                for gap in accepted_terminal_gaps
                for capability in gap.affected_capabilities
                if capability != "analysis_contract"
            ),
        )
    )
    accepted_claim_intents, claim_intent_gaps = _bind_claim_intents(
        proposal,
        contract_capabilities,
        metric_bindings,
        registry,
    )
    affected_capabilities = capabilities or ("analysis_contract",)
    contract_dataset_ids = _dedupe(
        (
            *required_dataset_ids,
            *(
                gap.dataset_id
                for gap in accepted_terminal_gaps
                if gap.dataset_id
            ),
        )
    )
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
        fixed_window_bounds=(
            proposal.get("fixed_window_bounds")
            if isinstance(proposal.get("fixed_window_bounds"), Mapping)
            else {}
        ),
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
        capability_dependencies,
    )
    capability_plans = _build_capability_plans(
        capabilities,
        query_contracts,
        query_refs_by_capability,
        registry,
        analysis_contract_ref=analysis_contract_id,
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
            *snapshot_evidence_gaps,
            *dependencies.source_selection_gaps,
            *metric_gaps,
            *dimension_gaps,
            *capability_input_gaps,
            *accepted_terminal_gaps,
        ),
        affected_capabilities=affected_capabilities,
        affected_claim_types=accepted_claim_intents,
        claim_types_by_capability=_claim_types_by_capability(
            capabilities, accepted_claim_intents, registry
        ),
    )
    gaps = _merge_contract_gaps(
        (
            *scoped_gaps,
            *claim_intent_gaps,
            *resolution.gaps,
        )
    )
    target_metrics = _values(proposal, "target_metrics")
    bound_target_metric_ids = {
        binding.metric_id
        for binding in metric_bindings
        if binding.metric_id in target_metrics
    }
    unresolved_target_metric_refs = _unresolved_target_metric_refs(
        target_metrics,
        bound_target_metric_ids,
        registry,
    )
    analysis = AnalysisContract(
        analysis_contract_id=analysis_contract_id,
        contract_version=registry.contract_version,
        question_families=_values(proposal, "question_families"),
        target_metric_refs=tuple(
            dict.fromkeys(
                (
                    *(
                        binding.contract_ref
                        for binding in metric_bindings
                        if binding.metric_id in target_metrics
                    ),
                    *unresolved_target_metric_refs,
                )
            )
        ),
        claim_intents=accepted_claim_intents,
        scope=_scope(
            proposal,
            requested_metric_ids=dependencies.metric_ids,
            requested_dimension_ids=dependencies.dimension_ids,
        ),
        business_timezone=registry.business_timezone,
        as_of=as_of.isoformat(),
        resolved_windows=resolution.windows,
        metric_bindings=metric_bindings,
        dimension_bindings=dimension_bindings,
        dataset_requirements=contract_dataset_ids,
        capability_requirements=contract_capabilities,
        permission_scope=permission_scope,
        contract_gaps=tuple(gaps),
        clarification_outcome_ref=clarification_outcome_ref,
    )
    return AnalysisCompileOutcome(analysis, query_contracts, capability_plans)


def _accepted_terminal_gap_authority(
    proposal: Mapping[str, Any],
) -> tuple[tuple[ContractGap, ...], str]:
    choice = proposal.get("accepted_degradation_choice")
    if not isinstance(choice, Mapping):
        return (), ""
    if str(choice.get("action_kind") or "") not in {
        "omit_unavailable_context",
        "continue_with_boundary_only",
    }:
        return (), ""
    choice_id = str(choice.get("choice_id") or "")
    source_run_id = str(choice.get("source_run_id") or "")
    raw_affected = choice.get("affected_capabilities")
    if (
        not isinstance(raw_affected, (list, tuple))
        or not raw_affected
        or any(not isinstance(item, str) or not item.strip() for item in raw_affected)
        or len(set(raw_affected)) != len(raw_affected)
    ):
        raise ValueError("accepted_terminal_gap_affected_capabilities_invalid")
    affected = set(raw_affected)
    if not affected:
        return (), ""
    if not choice_id or not source_run_id:
        raise ValueError("accepted_terminal_gap_choice_authority_invalid")
    authority = proposal.get("accepted_terminal_gap_authority")
    if not isinstance(authority, Mapping):
        raise ValueError("accepted_terminal_gap_authority_missing")
    if set(authority) != {
        "source_run_id",
        "thread_id",
        "topic_id",
        "analysis_contract",
        "analysis_contract_signature",
        "clarification_outcome",
    }:
        raise ValueError("accepted_terminal_gap_authority_shape_invalid")
    if str(authority.get("source_run_id") or "") != source_run_id:
        raise ValueError("accepted_terminal_gap_source_mismatch")
    expected_thread = str(proposal.get("resume_thread_id") or "")
    expected_topic = str(proposal.get("resume_topic_id") or "")
    if (
        not expected_thread
        or not expected_topic
        or authority.get("thread_id") != expected_thread
        or authority.get("topic_id") != expected_topic
    ):
        raise ValueError("accepted_terminal_gap_owner_mismatch")
    source = authority.get("analysis_contract")
    if not isinstance(source, Mapping):
        raise ValueError("accepted_terminal_gap_source_invalid")
    try:
        prior = analysis_contract_from_dict(source)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("accepted_terminal_gap_source_invalid") from exc
    if prior.analysis_contract_id != f"analysis:{source_run_id}:1":
        raise ValueError("accepted_terminal_gap_source_mismatch")
    if str(authority.get("analysis_contract_signature") or "") != (
        analysis_contract_signature(prior)
    ):
        raise ValueError("accepted_terminal_gap_contract_signature_invalid")
    outcome = authority.get("clarification_outcome")
    if not isinstance(outcome, Mapping) or set(outcome) != {
        "outcome_ref",
        "source_run_id",
        "thread_id",
        "topic_id",
        "choice",
        "outcome_signature",
    }:
        raise ValueError("accepted_terminal_gap_outcome_invalid")
    outcome_body = {
        key: value for key, value in outcome.items() if key != "outcome_signature"
    }
    if str(outcome.get("outcome_signature") or "") != stable_contract_signature(
        outcome_body
    ):
        raise ValueError("accepted_terminal_gap_outcome_signature_invalid")
    if (
        outcome.get("source_run_id") != source_run_id
        or outcome.get("thread_id") != authority.get("thread_id")
        or outcome.get("topic_id") != authority.get("topic_id")
    ):
        raise ValueError("accepted_terminal_gap_outcome_owner_mismatch")
    if canonical_value(outcome.get("choice")) != canonical_value(dict(choice)):
        raise ValueError("accepted_terminal_gap_outcome_choice_mismatch")
    outcome_ref = str(outcome.get("outcome_ref") or "")
    expected_outcome_ref = "clarification-outcome:" + stable_contract_signature(
        {
            "source_run_id": source_run_id,
            "thread_id": authority.get("thread_id"),
            "topic_id": authority.get("topic_id"),
            "choice": canonical_value(dict(choice)),
        }
    )
    if outcome_ref != expected_outcome_ref:
        raise ValueError("accepted_terminal_gap_outcome_ref_invalid")
    allowed_scope = affected | {"analysis_contract"}
    carried = tuple(
        replace(
            gap,
            affected_capabilities=_dedupe(
                capability
                for capability in (*gap.affected_capabilities, "analysis_contract")
                if capability in allowed_scope
            ),
        )
        for gap in prior.contract_gaps
        if affected.intersection(gap.affected_capabilities)
    )
    if not carried:
        raise ValueError("accepted_terminal_gap_scope_mismatch")
    return carried, outcome_ref


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


def _capability_dependency_sets(
    proposal: Mapping[str, Any],
    accepted_capabilities: tuple[str, ...],
    dependencies: _DependencyIndex,
) -> tuple[CapabilityDependencySet, ...]:
    requested_context = set(_values(proposal, "requested_context_sources"))
    return tuple(
        CapabilityDependencySet(
            capability_id=capability_id,
            metric_ids=tuple(
                metric_id
                for metric_id, owners in dependencies.metric_owners.items()
                if capability_id in owners
            ),
            dimension_ids=tuple(
                dimension_id
                for dimension_id, owners in dependencies.dimension_owners.items()
                if capability_id in owners
            ),
            dataset_ids=tuple(
                dataset_id
                for dataset_id, owners in dependencies.dataset_owners.items()
                if capability_id in owners
            ),
            context_source_ids=tuple(
                dataset_id
                for dataset_id, owners in dependencies.dataset_owners.items()
                if capability_id in owners and dataset_id in requested_context
            ),
        )
        for capability_id in accepted_capabilities
    )


def _claim_types_by_capability(
    capabilities: tuple[str, ...],
    accepted_claim_intents: tuple[str, ...],
    registry: RuntimeContractRegistry,
) -> dict[str, tuple[str, ...]]:
    accepted = set(accepted_claim_intents)
    return {
        capability_id: tuple(
            claim_type
            for claim_type in _mapping_values(
                _registry_entry(registry.capability_inputs, capability_id) or {},
                "supported_claim_types",
            )
            if claim_type in accepted
        )
        for capability_id in capabilities
    }


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
    target_metrics = _values(proposal, "target_metrics")
    capability_metric_gaps: list[ContractGap] = []

    metric_owners: dict[str, list[str]] = {metric_id: [] for metric_id in metric_ids}
    for metric_id in target_metrics:
        _append_owner(metric_owners, metric_id, "analysis_contract")
    for capability_id in accepted_capabilities:
        contract = _registry_entry(registry.capability_inputs, capability_id)
        if contract is None:
            continue
        if str(contract.get("metric_mode") or "") == "requested":
            allowed_metrics = set(_mapping_values(contract, "allowed_metrics"))
            for metric_id in target_metrics:
                if metric_id in allowed_metrics and metric_id in metric_owners:
                    _append_owner(metric_owners, metric_id, capability_id)
                elif metric_id not in allowed_metrics:
                    capability_metric_gaps.append(
                        _contract_gap(
                            gap_type="capability_metric_unsupported",
                            gap_id=(
                                f"metric:{metric_id}:"
                                "capability_metric_family_unsupported"
                            ),
                            affected_capabilities=(capability_id,),
                            repair_options=(
                                "choose_reviewed_metric",
                                "change_capability",
                            ),
                        )
                    )
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
    metric_overrides = _source_overrides(proposal, "metric_dataset_overrides")
    dimension_overrides = _source_overrides(proposal, "dimension_dataset_overrides")
    requested_datasets = _values(proposal, "dataset_requirements")
    requested_claim_intents = _values(proposal, "claim_intents")
    source_selection_gaps: list[ContractGap] = list(capability_metric_gaps)
    metric_dataset_ids: dict[str, tuple[str, ...]] = {}
    dimension_dataset_ids: dict[str, tuple[str, ...]] = {}
    for metric_id in metric_ids:
        try:
            sources = registry.metric_sources(metric_id)
            selected, gap = _select_source_datasets(
                item_kind="metric",
                item_id=metric_id,
                sources=tuple(sources),
                override=metric_overrides.get(metric_id, ""),
                requested_datasets=requested_datasets,
                owners=metric_owners[metric_id],
                registry=registry,
                affected_claim_types=requested_claim_intents,
            )
        except (KeyError, TypeError, ValueError):
            selected, gap = (), _contract_gap(
                gap_type="contract_absent",
                gap_id=f"metric:{metric_id}:contract_absent",
                affected_capabilities=tuple(metric_owners[metric_id]),
                repair_options=("register_metric_contract",),
            )
        metric_dataset_ids[metric_id] = selected
        if gap is not None:
            source_selection_gaps.append(gap)
        for dataset_id in selected:
            for owner in metric_owners[metric_id]:
                if (
                    _capability_reviews_dataset(owner, dataset_id, registry)
                    or not any(
                        _capability_reviews_dataset(owner, candidate, registry)
                        for candidate in selected
                    )
                ):
                    _append_owner(dataset_owners, dataset_id, owner)
    for dimension_id in dimension_ids:
        try:
            sources = registry.dimension_sources(dimension_id)
            selected, gap = _select_source_datasets(
                item_kind="dimension",
                item_id=dimension_id,
                sources=tuple(sources),
                override=dimension_overrides.get(dimension_id, ""),
                requested_datasets=requested_datasets,
                owners=dimension_owners[dimension_id],
                registry=registry,
                affected_claim_types=(),
            )
        except (KeyError, TypeError, ValueError):
            selected, gap = (), _contract_gap(
                gap_type="contract_absent",
                gap_id=f"dimension:{dimension_id}:contract_absent",
                affected_capabilities=tuple(dimension_owners[dimension_id]),
                repair_options=("register_dimension_contract",),
            )
        dimension_dataset_ids[dimension_id] = selected
        if gap is not None:
            source_selection_gaps.append(gap)
        for dataset_id in selected:
            for owner in dimension_owners[dimension_id]:
                if (
                    _capability_reviews_dataset(owner, dataset_id, registry)
                    or not any(
                        _capability_reviews_dataset(owner, candidate, registry)
                        for candidate in selected
                    )
                ):
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
        metric_dataset_ids=metric_dataset_ids,
        dimension_dataset_ids=dimension_dataset_ids,
        source_selection_gaps=tuple(source_selection_gaps),
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
    release_resolver: DatasetReleaseResolver | None,
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
                    evidence_states=("claim_ready", "context_only"),
                    release_resolver=release_resolver,
                )
            )
        except KeyError:
            eligible_candidates = catalog.as_of_candidates(
                dataset_id,
                as_of=as_of,
                evidence_states=("claim_ready", "context_only"),
                release_resolver=release_resolver,
            )
            permission_blocked = bool(eligible_candidates) and not any(
                permission_scope in item.permission_scopes
                for _, item in eligible_candidates
            )
            future_candidates = () if permission_blocked else catalog.future_as_of_candidates(
                dataset_id,
                as_of=as_of,
                evidence_states=("claim_ready", "context_only"),
                release_resolver=release_resolver,
            )
            if future_candidates:
                earliest_loaded_at, earliest = future_candidates[0]
                gaps.append(
                    _contract_gap(
                        gap_type="dataset_snapshot_unavailable_as_of",
                        gap_id=(
                            f"dataset:{dataset_id}:"
                            "dataset_snapshot_unavailable_as_of"
                        ),
                        dataset_id=dataset_id,
                        affected_capabilities=affected_capabilities,
                        owner="data_owner",
                        repair_options=(
                            "use_historical_snapshot_loaded_by_as_of",
                            "wait_for_snapshot_availability",
                        ),
                        requires_clarification=True,
                        diagnostic_context={
                            "as_of": as_of.astimezone(timezone.utc).isoformat(),
                            "earliest_snapshot_ref": earliest.snapshot_ref,
                            "earliest_loaded_at": earliest_loaded_at.isoformat(),
                        },
                    )
                )
                continue
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


def _snapshot_evidence_gaps(
    snapshots: tuple[DatasetSnapshot, ...],
    dataset_owners: Mapping[str, tuple[str, ...]],
    registry: RuntimeContractRegistry,
) -> tuple[ContractGap, ...]:
    gaps = []
    for snapshot in snapshots:
        for capability_id in dataset_owners.get(snapshot.dataset_id, ()):
            if capability_id == "analysis_contract":
                continue
            capability = _registry_entry(registry.capability_inputs, capability_id)
            if capability is None:
                continue
            unsupported = tuple(
                query_family
                for query_family in _mapping_values(capability, "query_families")
                if not _snapshot_supports_query(
                    snapshot,
                    query_family,
                    registry=registry,
                    has_dimensions=(
                        str(capability.get("dimension_mode") or "") == "requested"
                    ),
                )
            )
            if unsupported:
                gaps.append(
                    _contract_gap(
                        gap_type="contract_partial",
                        gap_id=(
                            f"dataset:{snapshot.dataset_id}:evidence_state:"
                            f"{snapshot.evidence_state}:capability:{capability_id}"
                        ),
                        dataset_id=snapshot.dataset_id,
                        affected_capabilities=(capability_id,),
                        owner="data_owner",
                        repair_options=(
                            "use_context_only_query",
                            "publish_claim_ready_release",
                            "resolve_reconciliation",
                        ),
                    )
                )
    return tuple(gaps)


def _snapshot_supports_query(
    snapshot: DatasetSnapshot,
    query_family: str,
    *,
    registry: RuntimeContractRegistry,
    has_dimensions: bool,
) -> bool:
    try:
        query_shape = registry.query_shape(query_family)
    except KeyError:
        return False
    source_fields = _mapping_values(query_shape, "source_fields")
    if source_fields and not set(source_fields).issubset(snapshot.schema_fields):
        return False
    context_families = {
        "data_quality_probe",
        "event_context_probe",
        "gameplay_activity_probe",
        "channel_context_probe",
        "source_reconciliation_probe",
    }
    if query_family in context_families:
        return snapshot.evidence_state in {"claim_ready", "context_only"}
    if snapshot.evidence_state != "claim_ready":
        return False
    if (
        has_dimensions
        and snapshot.reconciliation_ref
        and snapshot.reconciliation_status != "matched"
    ):
        return False
    return True


def _bind_metrics(
    metric_ids: tuple[str, ...],
    registry: RuntimeContractRegistry,
    snapshots: tuple[DatasetSnapshot, ...],
    metric_owners: Mapping[str, tuple[str, ...]],
    metric_dataset_ids: Mapping[str, tuple[str, ...]],
) -> tuple[tuple[MetricBinding, ...], tuple[ContractGap, ...]]:
    snapshots_by_dataset = {item.dataset_id: item for item in snapshots}
    bindings = []
    gaps = []
    for metric_id in metric_ids:
        affected_capabilities = metric_owners.get(metric_id, ("analysis_contract",))
        for dataset_id in metric_dataset_ids.get(metric_id, ()):
            contract = registry.metric(metric_id, dataset_id=dataset_id)
            binding, gap = _bind_metric_source(
                metric_id=metric_id,
                dataset_id=dataset_id,
                contract=contract,
                snapshot=snapshots_by_dataset.get(dataset_id),
                affected_capabilities=affected_capabilities,
                registry=registry,
            )
            if binding is not None:
                bindings.append(binding)
            if gap is not None:
                gaps.append(gap)
    return tuple(bindings), tuple(gaps)


def _bind_metric_source(
    *,
    metric_id: str,
    dataset_id: str,
    contract: Mapping[str, Any],
    snapshot: DatasetSnapshot | None,
    affected_capabilities: tuple[str, ...],
    registry: RuntimeContractRegistry,
) -> tuple[MetricBinding | None, ContractGap | None]:
    required_keys = (
        "contract_ref",
        "dataset_id",
        "expression",
        "aggregation",
        "required_fields",
        "grain",
        "value_semantics",
        "display_format",
    )
    missing = tuple(key for key in required_keys if key not in contract)
    if missing:
        return None, _contract_gap(
            gap_type="contract_partial",
            gap_id=f"metric:{metric_id}:missing:{','.join(missing)}",
            dataset_id=str(contract.get("dataset_id") or ""),
            affected_capabilities=affected_capabilities,
            repair_options=("complete_metric_contract", "remove_metric_path"),
        )
    required_fields = _mapping_values(contract, "required_fields")
    missing_fields = (
        tuple(field for field in required_fields if field not in snapshot.schema_fields)
        if snapshot is not None
        else ()
    )
    if missing_fields:
        return None, _contract_gap(
            gap_type="contract_partial",
            gap_id=f"metric:{metric_id}:schema_missing:{','.join(missing_fields)}",
            dataset_id=dataset_id,
            affected_capabilities=affected_capabilities,
            owner="data_owner",
            repair_options=("refresh_snapshot_schema", "repair_metric_binding"),
        )
    reconciliation_tolerance = _reconciliation_tolerance(contract)
    if reconciliation_tolerance is None:
        return None, _contract_gap(
            gap_type="contract_partial",
            gap_id=f"metric:{metric_id}:invalid:reconciliation_tolerance",
            dataset_id=dataset_id,
            affected_capabilities=affected_capabilities,
            repair_options=("repair_metric_binding",),
        )
    reconciliation_strategy = _reconciliation_strategy(
        contract,
        reconciliation_tolerance,
    )
    if reconciliation_strategy is None:
        return None, _contract_gap(
            gap_type="contract_partial",
            gap_id=f"metric:{metric_id}:invalid:reconciliation_strategy",
            dataset_id=dataset_id,
            affected_capabilities=affected_capabilities,
            repair_options=("repair_metric_binding",),
        )
    display_policy = _metric_display_policy(contract, registry)
    if display_policy is None:
        return None, _contract_gap(
            gap_type="contract_partial",
            gap_id=f"metric:{metric_id}:invalid:display_policy",
            dataset_id=dataset_id,
            affected_capabilities=affected_capabilities,
            repair_options=("repair_metric_binding",),
        )
    return MetricBinding(
        metric_id=metric_id,
        contract_ref=str(contract["contract_ref"]),
        dataset_id=dataset_id,
        expression=str(contract["expression"]),
        aggregation=str(contract["aggregation"]),
        required_fields=required_fields,
        grain=_mapping_values(contract, "grain"),
        numerator_metric=str(contract.get("numerator_metric") or ""),
        denominator_metric=str(contract.get("denominator_metric") or ""),
        zero_denominator_policy=str(contract.get("zero_denominator_policy") or "null"),
        claim_types=_mapping_values(contract, "claim_types"),
        reconciliation_tolerance=reconciliation_tolerance,
        reconciliation_strategy=reconciliation_strategy,
        value_semantics=display_policy[0],
        display_format=display_policy[1],
    ), None


def _metric_display_policy(
    contract: Mapping[str, Any],
    registry: RuntimeContractRegistry,
) -> tuple[str, str] | None:
    policy = (
        str(contract.get("value_semantics") or ""),
        str(contract.get("display_format") or ""),
    )
    return policy if registry.metric_display_policy_allowed(*policy) else None


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
    dimension_dataset_ids: Mapping[str, tuple[str, ...]],
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
        for dataset_id in dimension_dataset_ids.get(dimension_id, ()):
            contract = registry.dimension(dimension_id, dataset_id=dataset_id)
            binding, gap = _bind_dimension_source(
                dimension_id=dimension_id,
                dataset_id=dataset_id,
                contract=contract,
                requested_grain=requested_grain,
                permission_scope=permission_scope,
                snapshot=snapshots_by_dataset.get(dataset_id),
                affected_capabilities=affected_capabilities,
            )
            if binding is not None:
                bindings.append(binding)
            if gap is not None:
                gaps.append(gap)
    return tuple(bindings), tuple(gaps)


def _bind_dimension_source(
    *,
    dimension_id: str,
    dataset_id: str,
    contract: Mapping[str, Any],
    requested_grain: str,
    permission_scope: str,
    snapshot: DatasetSnapshot | None,
    affected_capabilities: tuple[str, ...],
) -> tuple[DimensionBinding | None, ContractGap | None]:
    required_keys = ("contract_ref", "dataset_id", "source_field", "allowed_grains")
    missing = tuple(key for key in required_keys if key not in contract)
    if missing:
        return None, _contract_gap(
            gap_type="contract_partial",
            gap_id=f"dimension:{dimension_id}:missing:{','.join(missing)}",
            dataset_id=str(contract.get("dataset_id") or ""),
            affected_capabilities=affected_capabilities,
            repair_options=("complete_dimension_contract", "remove_dimension_path"),
        )
    allowed_grains = _mapping_values(contract, "allowed_grains")
    source_field = str(contract["source_field"])
    if snapshot is not None and source_field not in snapshot.schema_fields:
        return None, _contract_gap(
            gap_type="contract_partial",
            gap_id=f"dimension:{dimension_id}:schema_missing:{source_field}",
            dataset_id=dataset_id,
            affected_capabilities=affected_capabilities,
            owner="data_owner",
            repair_options=("refresh_snapshot_schema", "repair_dimension_binding"),
        )
    if requested_grain not in allowed_grains:
        return None, _contract_gap(
            gap_type="unsupported_grain",
            gap_id=f"dimension:{dimension_id}:grain:{requested_grain}",
            dataset_id=dataset_id,
            affected_capabilities=affected_capabilities,
            repair_options=("use_supported_grain", "remove_dimension_path"),
            requires_clarification=True,
        )
    return DimensionBinding(
        dimension_id=dimension_id,
        contract_ref=str(contract["contract_ref"]),
        dataset_id=dataset_id,
        source_field=source_field,
        allowed_grains=allowed_grains,
        null_bucket=str(contract.get("null_bucket") or "Unknown"),
        permission_scope=permission_scope,
    ), None


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
    fixed_window_bounds: Mapping[str, tuple[str, str]],
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
            fixed_window_bounds=fixed_window_bounds,
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
    capability_dependencies: tuple[CapabilityDependencySet, ...] = (),
) -> tuple[tuple[QueryContract, ...], dict[str, tuple[str, ...]]]:
    if not windows:
        return (), {
            capability_id: () for capability_id in accepted_capabilities
        }
    snapshot_by_dataset = {item.dataset_id: item for item in snapshots}
    metric_ids_available = {item.metric_id for item in metric_bindings}
    filters = _filters(proposal)
    window_refs = tuple(item.window_id for item in windows)
    logical_queries: list[dict[str, Any]] = []
    query_owners: list[set[str]] = []
    seen: dict[str, int] = {}
    dependencies_by_capability = {
        item.capability_id: item for item in capability_dependencies
    }

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
        dependency_set = dependencies_by_capability.get(capability_id)
        capability_metric_ids = (
            set(dependency_set.metric_ids) if dependency_set is not None else None
        )
        capability_dataset_ids = (
            set(dependency_set.dataset_ids) if dependency_set is not None else None
        )
        optional_metrics = set(_mapping_values(capability, "optional_metrics"))
        family_metrics = capability.get("query_family_metrics")
        if not isinstance(family_metrics, Mapping):
            family_metrics = {}
        for query_family in _mapping_values(capability, "query_families"):
            query_shape = _registry_entry(registry.query_shape, query_family)
            if query_shape is None:
                continue
            configured_metrics = family_metrics.get(query_family)
            if str(capability.get("metric_mode") or "") == "requested":
                allowed_metrics = set(
                    _mapping_values(capability, "allowed_metrics")
                )
                metric_ids = tuple(
                    metric_id
                    for metric_id in _values(proposal, "target_metrics")
                    if metric_id in allowed_metrics
                )
            elif configured_metrics is None:
                metric_ids = (
                    *_mapping_values(capability, "required_metrics"),
                    *(
                        metric_id
                        for metric_id in optional_metrics
                        if metric_id in metric_ids_available
                    ),
                )
            else:
                metric_ids = _sequence_values(configured_metrics)
            selected_metrics = tuple(
                binding
                for metric_id in _dedupe(metric_ids)
                for binding in metric_bindings
                if binding.metric_id == metric_id
                and (
                    capability_metric_ids is None
                    or binding.metric_id in capability_metric_ids
                )
                and (
                    capability_dataset_ids is None
                    or binding.dataset_id in capability_dataset_ids
                )
                and (
                    query_family == "data_quality_probe"
                    or not _mapping_values(capability, "allowed_datasets")
                    or binding.dataset_id
                    in _mapping_values(capability, "allowed_datasets")
                    or _source_overrides(
                        proposal,
                        "metric_dataset_overrides",
                    ).get(metric_id)
                    == binding.dataset_id
                )
            )
            by_dataset: dict[str, list[MetricBinding]] = {}
            for binding in selected_metrics:
                by_dataset.setdefault(binding.dataset_id, []).append(binding)
            if not by_dataset and capability.get("source_mode") == "requested_context_sources":
                by_dataset = {
                    dataset_id: []
                    for dataset_id in (
                        dependency_set.context_source_ids
                        if dependency_set is not None
                        else _values(proposal, "requested_context_sources")
                    )
                }

            for dataset_id, dataset_metrics in by_dataset.items():
                snapshot = snapshot_by_dataset.get(dataset_id)
                if snapshot is None:
                    continue
                include_dimensions = str(capability.get("dimension_mode") or "") == "requested"
                if not _snapshot_supports_query(
                    snapshot,
                    query_family,
                    registry=registry,
                    has_dimensions=include_dimensions,
                ):
                    continue
                normalized_metrics = tuple(
                    sorted(dataset_metrics, key=lambda item: item.metric_id)
                )
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
                    if query_dimensions and query_family not in {
                        "channel_context_probe",
                        "data_quality_probe",
                        "gameplay_activity_probe",
                    }:
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
    *,
    analysis_contract_ref: str,
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
        capability_signature = registry.capability_contract_signature(
            capability_id
        )
        capability_ref = registry.capability_contract_ref(capability_id)
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
                analysis_contract_ref=analysis_contract_ref,
                supported_claim_types=_mapping_values(
                    contract,
                    "supported_claim_types",
                ),
                capability_contract_version=registry.contract_version,
                capability_contract_signature=capability_signature,
                claim_strength_taxonomy_version=(
                    registry.claim_strength_taxonomy_version
                ),
                maximum_claim_strength_rank=(
                    registry.maximum_claim_strength_rank(
                        str(contract.get("maximum_claim_strength") or "")
                    )
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
        dimension_presence_policy=str(query_shape["dimension_presence_policy"]),
    )


def _scope_gaps(
    gaps: Iterable[ContractGap],
    *,
    affected_capabilities: tuple[str, ...],
    affected_claim_types: tuple[str, ...],
    claim_types_by_capability: Mapping[str, tuple[str, ...]] | None = None,
) -> tuple[ContractGap, ...]:
    scoped = []
    for gap in gaps:
        capabilities = gap.affected_capabilities or affected_capabilities
        claim_types = gap.affected_claim_types
        if not claim_types and claim_types_by_capability is not None:
            claim_types = _dedupe(
                claim_type
                for capability_id in capabilities
                for claim_type in claim_types_by_capability.get(capability_id, ())
            )
        if (
            not claim_types
            and _is_direct_analysis_source_ambiguity(gap, capabilities)
        ):
            claim_types = affected_claim_types
        scoped_claim_types = (
            claim_types
            if claim_types_by_capability is not None
            else claim_types or affected_claim_types
        )
        diagnostic_context = gap.diagnostic_context
        if (
            scoped_claim_types
            and _is_direct_analysis_source_ambiguity(gap, capabilities)
        ):
            diagnostic_context = {
                **gap.diagnostic_context,
                "claim_intents": list(scoped_claim_types),
            }
        scoped.append(
            replace(
                gap,
                affected_capabilities=capabilities,
                affected_claim_types=scoped_claim_types,
                diagnostic_context=diagnostic_context,
            )
        )
    return tuple(scoped)


def _is_direct_analysis_source_ambiguity(
    gap: ContractGap,
    capabilities: tuple[str, ...],
) -> bool:
    diagnostic = gap.diagnostic_context
    item_kind = str(diagnostic.get("item_kind") or "")
    item_id = str(diagnostic.get("item_id") or "")
    return (
        capabilities == ("analysis_contract",)
        and item_kind in {"metric", "dimension"}
        and bool(item_id)
        and gap.gap_type == "contract_partial"
        and gap.gap_id.startswith(f"{item_kind}:{item_id}:source_ambiguous:")
        and gap.dataset_id == ""
        and gap.owner == "contract_owner"
        and gap.repair_options
        == ("select_dataset_requirement", "clarify_source_scope")
        and gap.requires_clarification is True
    )


def _merge_contract_gaps(
    gaps: Iterable[ContractGap],
) -> tuple[ContractGap, ...]:
    merged: dict[tuple[Any, ...], ContractGap] = {}
    for gap in gaps:
        identity = (
            gap.gap_type,
            gap.gap_id,
            gap.dataset_id,
            gap.owner,
            gap.requires_clarification,
        )
        existing = merged.get(identity)
        if existing is None:
            merged[identity] = gap
            continue
        merged[identity] = replace(
            existing,
            affected_capabilities=tuple(
                sorted(
                    set(existing.affected_capabilities).union(
                        gap.affected_capabilities
                    )
                )
            ),
            affected_claim_types=tuple(
                sorted(
                    set(existing.affected_claim_types).union(
                        gap.affected_claim_types
                    )
                )
            ),
            repair_options=tuple(
                sorted(set(existing.repair_options).union(gap.repair_options))
            ),
        )
    return tuple(merged.values())


def _contract_gap(
    *,
    gap_type: str,
    gap_id: str,
    dataset_id: str = "",
    affected_capabilities: tuple[str, ...] = (),
    affected_claim_types: tuple[str, ...] = (),
    owner: str = "contract_owner",
    repair_options: tuple[str, ...] = (),
    requires_clarification: bool = False,
    diagnostic_context: Mapping[str, Any] | None = None,
) -> ContractGap:
    return ContractGap(
        gap_type=gap_type,
        gap_id=gap_id,
        dataset_id=dataset_id,
        affected_capabilities=affected_capabilities,
        affected_claim_types=affected_claim_types,
        owner=owner,
        repair_options=repair_options,
        requires_clarification=requires_clarification,
        diagnostic_context=dict(diagnostic_context or {}),
    )


def _scope(
    proposal: Mapping[str, Any],
    *,
    requested_metric_ids: tuple[str, ...] | None = None,
    requested_dimension_ids: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    value = proposal.get("scope")
    if isinstance(value, Mapping):
        scope = dict(value)
    elif value not in (None, ""):
        scope = {"type": str(value)}
    else:
        scope = {"type": "full_sample"}
    scope["requested_metric_ids"] = (
        requested_metric_ids
        if requested_metric_ids is not None
        else _values(proposal, "target_metrics")
    )
    scope["requested_dimension_ids"] = (
        requested_dimension_ids
        if requested_dimension_ids is not None
        else _values(proposal, "requested_dimensions")
    )
    return scope


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


def _source_overrides(proposal: Mapping[str, Any], key: str) -> dict[str, str]:
    value = proposal.get(key)
    if value in (None, {}):
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{key}_must_be_mapping")
    result = {}
    for item_id, dataset_id in value.items():
        if (
            not isinstance(item_id, str)
            or not item_id.strip()
            or not isinstance(dataset_id, str)
            or not dataset_id.strip()
        ):
            raise ValueError(f"{key}_entries_must_be_non_empty_strings")
        result[item_id.strip()] = dataset_id.strip()
    return result


def _select_source_datasets(
    *,
    item_kind: str,
    item_id: str,
    sources: tuple[str, ...],
    override: str,
    requested_datasets: tuple[str, ...],
    owners: list[str],
    registry: RuntimeContractRegistry,
    affected_claim_types: tuple[str, ...],
) -> tuple[tuple[str, ...], ContractGap | None]:
    candidates = tuple(dict.fromkeys(sources))
    affected = tuple(owners) or ("analysis_contract",)
    if override:
        if override in candidates:
            return (override,), None
        return (), _contract_gap(
            gap_type="contract_absent",
            gap_id=f"{item_kind}:{item_id}:source_unavailable:{override}",
            affected_capabilities=affected,
            repair_options=("select_registered_source", "register_source_adapter"),
        )

    owner_contracts = tuple(
        contract
        for owner in owners
        if owner != "analysis_contract"
        for contract in (_registry_entry(registry.capability_inputs, owner),)
        if contract is not None
    )
    reviewed_metric_families = tuple(
        _mapping_values(contract, "allowed_metrics")
        for contract in owner_contracts
        if "allowed_metrics" in contract
    )
    if (
        item_kind == "metric"
        and reviewed_metric_families
        and not any(item_id in family for family in reviewed_metric_families)
    ):
        return (), _contract_gap(
            gap_type="contract_partial",
            gap_id=f"metric:{item_id}:capability_metric_family_unsupported",
            affected_capabilities=affected,
            repair_options=("choose_reviewed_metric", "change_capability"),
            requires_clarification=True,
        )
    all_required = any(
        str(contract.get("source_selection") or "") == "all_required_datasets"
        for contract in owner_contracts
    )
    allowed = {
        dataset_id
        for contract in owner_contracts
        for dataset_id in _mapping_values(contract, "allowed_datasets")
    }
    requested_source_unrestricted = any(
        _mapping_values(contract, "query_families") == ("data_quality_probe",)
        for contract in owner_contracts
    )
    requested = tuple(
        dataset_id
        for dataset_id in candidates
        if dataset_id in requested_datasets
    )
    reviewed_requested = tuple(
        dataset_id for dataset_id in requested if dataset_id in allowed
    )
    purpose_resolved = _requested_sources_resolve_by_capability(
        requested,
        affected,
        registry,
    )
    selected = (
        requested
        if (
            purpose_resolved
            or requested_source_unrestricted
            or not allowed
            or not reviewed_requested
        )
        else reviewed_requested
    )
    if selected:
        if (
            all_required
            or purpose_resolved
            or requested_source_unrestricted
            or len(selected) == 1
        ):
            excluded_requested = tuple(
                dataset_id for dataset_id in requested if dataset_id not in selected
            )
            if excluded_requested:
                restricted_owners = tuple(
                    owner
                    for owner in affected
                    if owner != "analysis_contract"
                    and any(
                        not _capability_reviews_dataset(owner, dataset_id, registry)
                        for dataset_id in excluded_requested
                    )
                ) or affected
                return selected, _contract_gap(
                    gap_type="contract_partial",
                    gap_id=(
                        f"{item_kind}:{item_id}:requested_source_unreviewed:"
                        f"{','.join(excluded_requested)}"
                    ),
                    dataset_id=(
                        excluded_requested[0]
                        if len(excluded_requested) == 1
                        else ""
                    ),
                    affected_capabilities=restricted_owners,
                    affected_claim_types=affected_claim_types,
                    repair_options=(
                        "use_capability_reviewed_source",
                        "bind_independent_context_capability",
                    ),
                    diagnostic_context={
                        "item_kind": item_kind,
                        "item_id": item_id,
                        "requested_dataset_ids": list(requested),
                        "reviewed_dataset_ids": list(selected),
                        "excluded_dataset_ids": list(excluded_requested),
                        "claim_intents": list(affected_claim_types),
                    },
                )
            return selected, None
        return (), _source_ambiguity_gap(
            item_kind, item_id, selected, affected, affected_claim_types
        )

    if allowed:
        constrained = tuple(
            dataset_id for dataset_id in candidates if dataset_id in allowed
        )
        if all_required and constrained:
            return constrained, None
        if len(constrained) == 1:
            return constrained, None
        if len(constrained) > 1:
            return (), _source_ambiguity_gap(
                item_kind, item_id, constrained, affected, affected_claim_types
            )
    if len(candidates) == 1:
        return candidates, None
    if candidates:
        return (), _source_ambiguity_gap(
            item_kind, item_id, candidates, affected, affected_claim_types
        )
    return (), _contract_gap(
        gap_type="contract_absent",
        gap_id=f"{item_kind}:{item_id}:contract_absent",
        affected_capabilities=affected,
        repair_options=(f"register_{item_kind}_contract",),
    )


def _capability_reviews_dataset(
    capability_id: str,
    dataset_id: str,
    registry: RuntimeContractRegistry,
) -> bool:
    if capability_id == "analysis_contract":
        return True
    contract = _registry_entry(registry.capability_inputs, capability_id)
    if contract is None:
        return False
    if _mapping_values(contract, "query_families") == ("data_quality_probe",):
        return True
    allowed = _mapping_values(contract, "allowed_datasets")
    return not allowed or dataset_id in allowed


def _requested_sources_resolve_by_capability(
    requested: tuple[str, ...],
    owners: tuple[str, ...],
    registry: RuntimeContractRegistry,
) -> bool:
    if not requested:
        return False
    covered: set[str] = set()
    observed_owner = False
    for owner in owners:
        if owner == "analysis_contract":
            continue
        contract = _registry_entry(registry.capability_inputs, owner)
        if contract is None:
            continue
        observed_owner = True
        query_families = _mapping_values(contract, "query_families")
        quality_only = query_families == ("data_quality_probe",)
        allowed = _mapping_values(contract, "allowed_datasets")
        owned = (
            requested
            if quality_only or not allowed
            else tuple(dataset_id for dataset_id in requested if dataset_id in allowed)
        )
        all_required = (
            str(contract.get("source_selection") or "")
            == "all_required_datasets"
        )
        if len(owned) > 1 and not quality_only and not all_required:
            return False
        covered.update(owned)
    return observed_owner and covered == set(requested)


def _source_ambiguity_gap(
    item_kind: str,
    item_id: str,
    datasets: tuple[str, ...],
    affected: tuple[str, ...],
    affected_claim_types: tuple[str, ...],
) -> ContractGap:
    return _contract_gap(
        gap_type="contract_partial",
        gap_id=(
            f"{item_kind}:{item_id}:source_ambiguous:{','.join(datasets)}"
        ),
        affected_capabilities=affected,
        affected_claim_types=affected_claim_types,
        repair_options=("select_dataset_requirement", "clarify_source_scope"),
        requires_clarification=True,
        diagnostic_context={
            "item_kind": item_kind,
            "item_id": item_id,
            "claim_intents": list(affected_claim_types),
        },
    )


def _unresolved_target_metric_refs(
    target_metrics: tuple[str, ...],
    bound_target_metric_ids: set[str],
    registry: RuntimeContractRegistry,
) -> tuple[str, ...]:
    refs: list[str] = []
    for metric_id in target_metrics:
        if metric_id in bound_target_metric_ids:
            continue
        try:
            sources = registry.metric_sources(metric_id).values()
        except (KeyError, TypeError, ValueError):
            continue
        refs.extend(
            str(source.get("contract_ref") or "")
            for source in sources
            if str(source.get("contract_ref") or "")
        )
    return tuple(dict.fromkeys(refs))


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
