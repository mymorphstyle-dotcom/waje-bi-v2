from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from itertools import combinations
from typing import Any, Mapping

from bi_agent.runtime.analysis_contracts import (
    DimensionBinding,
    MetricBinding,
    QueryContract,
    ReconciliationBinding,
    ResolvedWindow,
    ResultShape,
    query_contract_signature,
)
from bi_agent.runtime.dataset_catalog import (
    DatasetReleaseAuthorityRecord,
    DatasetSnapshot,
    build_dataset_release_authority_record,
    dataset_snapshot_release_ref,
)
from bi_agent.runtime.baseline_semantics import CANONICAL_BASELINE_IDS
from bi_agent.runtime.single_authority import DecisionLedger, DecisionRecord
from bi_agent.runtime.temporal_comparison import resolve_effective_comparison
from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry
from bi_agent.runtime.query_completeness import CURRENT_DATA_ASSERTIONS
from bi_agent.runtime.window_resolver import resolve_temporal_windows


_COMPLETENESS = CURRENT_DATA_ASSERTIONS


class _CoverageReleaseResolver:
    def __init__(self, record: DatasetReleaseAuthorityRecord) -> None:
        self._record = record

    def resolve_dataset_release(
        self, release_ref: str
    ) -> DatasetReleaseAuthorityRecord:
        if release_ref != self._record.release_ref:
            raise KeyError(release_ref)
        return self._record


@dataclass(frozen=True)
class CurrentDataCoverageCase:
    case_id: str
    dataset_ids: tuple[str, ...]
    metric_ids: tuple[str, ...]
    dimension_ids: tuple[str, ...]
    query_family: str
    required_window_ids: tuple[str, ...]
    expected_state: str
    claim_ceiling: str
    gap_type: str = ""
    owner: str = ""
    source_fields: tuple[str, ...] = ()
    window_policy: str = ""
    reconciliation_expectation: str = ""
    provider_bounds: str = ""
    query_contract: QueryContract | None = None
    snapshots: Mapping[str, DatasetSnapshot] | None = None
    release_resolver: _CoverageReleaseResolver | None = None


def current_data_coverage_cases(
    registry: RuntimeContractRegistry,
) -> tuple[CurrentDataCoverageCase, ...]:
    """Enumerate reviewed current-data paths and retain unclosed paths as gaps."""
    adapter_pairs = tuple(
        sorted(
            (metric_id, dataset_id)
            for metric_id in registry.metric_ids
            for dataset_id in registry.metric_sources(metric_id)
        )
    )
    cases = [_adapter_case(registry, *pair) for pair in adapter_pairs]
    for metric_id, dataset_id in adapter_pairs:
        cases.extend(_dimension_coverage_cases(registry, metric_id, dataset_id))

    for dataset_id in registry.dataset_ids:
        dataset = registry.dataset(dataset_id)
        schema = set(str(item) for item in dataset.get("schema_fields", ()))
        if {"event_id", "event_start_date", "source_family"} <= schema:
            cases.append(
                _supported_case(
                    registry,
                    f"context:{dataset_id}:event_context_probe",
                    (dataset_id,),
                    "",
                    (),
                    "event_context_probe",
                    "candidate_mechanism",
                )
            )

    cases = _bind_overall_channel_reconciliation(cases)

    supported_families = {case.query_family for case in cases}
    obligation_families = {
        str(query_family)
        for capability_id in registry.capability_ids
        for query_family in registry.capability_inputs(capability_id).get(
            "query_families", ()
        )
    }
    for query_family in sorted(obligation_families - supported_families):
        cases.append(
            CurrentDataCoverageCase(
                case_id=f"gap:query-family:{query_family}",
                dataset_ids=(),
                metric_ids=(),
                dimension_ids=(),
                query_family=query_family,
                required_window_ids=tuple(
                    window.window_id for window in _coverage_windows(registry)
                ),
                expected_state="degraded",
                claim_ceiling="insufficient",
                gap_type="schema_backed_query_adapter_missing",
                owner="analysis_contract_owner",
            )
        )

    return tuple(sorted(cases, key=lambda case: case.case_id))


def _adapter_case(
    registry: RuntimeContractRegistry,
    metric_id: str,
    dataset_id: str,
) -> CurrentDataCoverageCase:
    metric = registry.metric(metric_id, dataset_id=dataset_id)
    dataset = registry.dataset(dataset_id)
    schema_fields = tuple(str(item) for item in dataset.get("schema_fields", ()))
    required_fields = tuple(str(item) for item in metric.get("required_fields", ()))
    missing = tuple(sorted(set(required_fields) - set(schema_fields)))
    query_family, dimension_ids = _adapter_query_family(registry, metric_id, dataset_id)
    case_id = f"adapter:{metric_id}:{dataset_id}:{query_family}"
    if missing:
        return CurrentDataCoverageCase(
            case_id=case_id,
            dataset_ids=(dataset_id,),
            metric_ids=(metric_id,),
            dimension_ids=dimension_ids,
            query_family=query_family,
            required_window_ids=tuple(
                window.window_id for window in _coverage_windows(registry)
            ),
            expected_state="degraded",
            claim_ceiling="insufficient",
            gap_type="source_schema_mismatch",
            owner="source_contract_owner",
            source_fields=required_fields,
        )
    return _supported_case(
        registry,
        case_id,
        (dataset_id,),
        metric_id,
        dimension_ids,
        query_family,
        str(metric.get("maximum_claim_strength") or "directional"),
    )


def _adapter_query_family(
    registry: RuntimeContractRegistry,
    metric_id: str,
    dataset_id: str,
) -> tuple[str, tuple[str, ...]]:
    for capability_id in registry.capability_ids:
        capability = registry.capability_inputs(capability_id)
        for query_family in capability.get("query_families", ()):
            dimension_mode = str(capability.get("dimension_mode") or "")
            if _capability_accepts_adapter(
                capability,
                query_family=str(query_family),
                metric_id=metric_id,
                dataset_id=dataset_id,
                require_dimension=dimension_mode == "requested",
            ):
                dimension_ids = (
                    tuple(
                        dimension_id
                        for dimension_id in registry.dimension_ids
                        if dataset_id in registry.dimension_sources(dimension_id)
                        and str(query_family)
                        in tuple(
                            registry.dimension(
                                dimension_id,
                                dataset_id=dataset_id,
                            ).get("allowed_query_families")
                            or (str(query_family),)
                        )
                    )
                    if dimension_mode == "requested"
                    else ()
                )
                if dimension_mode != "requested" or dimension_ids:
                    return str(query_family), dimension_ids
    for capability_id in registry.capability_ids:
        capability = registry.capability_inputs(capability_id)
        if dataset_id not in tuple(capability.get("allowed_datasets") or ()):
            continue
        dimension_mode = str(capability.get("dimension_mode") or "")
        for query_family in capability.get("query_families", ()):
            query_family = str(query_family)
            dimension_ids = (
                tuple(
                    dimension_id
                    for dimension_id in registry.dimension_ids
                    if dataset_id in registry.dimension_sources(dimension_id)
                    and query_family
                    in tuple(
                        registry.dimension(
                            dimension_id,
                            dataset_id=dataset_id,
                        ).get("allowed_query_families")
                        or (query_family,)
                    )
                )
                if dimension_mode == "requested"
                else ()
            )
            if dimension_mode != "requested" or dimension_ids:
                return query_family, dimension_ids
    raise ValueError(
        f"current_data_adapter_query_family_missing:{metric_id}:{dataset_id}"
    )


def _dimension_coverage_cases(
    registry: RuntimeContractRegistry,
    metric_id: str,
    dataset_id: str,
) -> list[CurrentDataCoverageCase]:
    dimensions = tuple(
        dimension_id
        for dimension_id in registry.dimension_ids
        if dataset_id in registry.dimension_sources(dimension_id)
    )
    if not dimensions:
        return []
    output = []
    for topology in ("independent", "joint"):
        supported_family = _dimension_query_family(
            registry,
            metric_id=metric_id,
            dataset_id=dataset_id,
            topology=topology,
        )
        query_families = (
            (supported_family,)
            if supported_family
            else _query_families_for_topology(
                registry,
                topology,
                dataset_id=dataset_id,
            )
        )
        dimension_groups = (
            tuple((dimension_id,) for dimension_id in dimensions)
            if topology == "independent"
            else tuple(
                tuple(group)
                for size in range(1, len(dimensions) + 1)
                for group in combinations(dimensions, size)
            )
        )
        output.extend(
            _dimension_case(
                registry,
                metric_id=metric_id,
                dataset_id=dataset_id,
                dimension_ids=dimension_ids,
                query_family=query_family,
                capability_supported=bool(supported_family),
            )
            for query_family in query_families
            for dimension_ids in dimension_groups
        )
    return output


def _dimension_case(
    registry: RuntimeContractRegistry,
    *,
    metric_id: str,
    dataset_id: str,
    dimension_ids: tuple[str, ...],
    query_family: str,
    capability_supported: bool,
) -> CurrentDataCoverageCase:
    case_id = f"dimension:{metric_id}:{dataset_id}:{query_family}:" + "+".join(
        dimension_ids
    )
    metric = registry.metric(metric_id, dataset_id=dataset_id)
    dataset_schema = set(registry.dataset(dataset_id).get("schema_fields", ()))
    dimension_contracts = tuple(
        registry.dimension(dimension_id, dataset_id=dataset_id)
        for dimension_id in dimension_ids
    )
    required_fields = {
        *(str(item) for item in metric.get("required_fields", ())),
        *(str(item.get("source_field") or "") for item in dimension_contracts),
    }
    missing = tuple(sorted(required_fields - dataset_schema - {""}))
    gap_type = ""
    owner = ""
    if missing:
        gap_type = "source_schema_mismatch"
        owner = "source_contract_owner"
    elif not capability_supported:
        gap_type = "capability_contract_unsupported_metric"
        owner = "analysis_contract_owner"
    elif any(
        "window_id" not in tuple(item.get("allowed_grains") or ())
        for item in dimension_contracts
    ):
        gap_type = "unsupported_grain"
        owner = "source_contract_owner"
    elif any(
        query_family not in tuple(item.get("allowed_query_families") or (query_family,))
        for item in dimension_contracts
    ):
        gap_type = "contract_partial"
        owner = "analysis_contract_owner"
    if gap_type:
        return CurrentDataCoverageCase(
            case_id=case_id,
            dataset_ids=(dataset_id,),
            metric_ids=(metric_id,),
            dimension_ids=dimension_ids,
            query_family=query_family,
            required_window_ids=tuple(
                window.window_id for window in _coverage_windows(registry)
            ),
            expected_state="degraded",
            claim_ceiling="insufficient",
            gap_type=gap_type,
            owner=owner,
            source_fields=tuple(sorted(required_fields - {""})),
        )
    return _supported_case(
        registry,
        case_id,
        (dataset_id,),
        metric_id,
        dimension_ids,
        query_family,
        str(metric.get("maximum_claim_strength") or "directional"),
    )


def _dimension_query_family(
    registry: RuntimeContractRegistry,
    *,
    metric_id: str,
    dataset_id: str,
    topology: str,
) -> str:
    for capability_id in registry.capability_ids:
        capability = registry.capability_inputs(capability_id)
        if str(capability.get("dimension_mode") or "") != "requested":
            continue
        if str(capability.get("source_selection") or "") == "all_required_datasets":
            continue
        for query_family in capability.get("query_families", ()):
            query_family = str(query_family)
            try:
                shape = registry.query_shape(query_family)
            except KeyError:
                continue
            if str(shape.get("dimension_topology") or "") != topology:
                continue
            if _capability_accepts_adapter(
                capability,
                query_family=query_family,
                metric_id=metric_id,
                dataset_id=dataset_id,
                require_dimension=True,
            ):
                return query_family
    return ""


def _query_families_for_topology(
    registry: RuntimeContractRegistry,
    topology: str,
    *,
    dataset_id: str,
) -> tuple[str, ...]:
    families = {
        str(query_family)
        for capability_id in registry.capability_ids
        if dataset_id
        in tuple(registry.capability_inputs(capability_id).get("allowed_datasets", ()))
        for query_family in registry.capability_inputs(capability_id).get(
            "query_families", ()
        )
        if str(registry.query_shape(str(query_family)).get("dimension_topology") or "")
        == topology
    }
    return tuple(sorted(families))


def _capability_accepts_adapter(
    capability: Mapping[str, Any],
    *,
    query_family: str,
    metric_id: str,
    dataset_id: str,
    require_dimension: bool,
) -> bool:
    allowed_datasets = tuple(capability.get("allowed_datasets") or ())
    if dataset_id not in allowed_datasets:
        return False
    if require_dimension != (
        str(capability.get("dimension_mode") or "") == "requested"
    ):
        return False
    family_metrics = capability.get("query_family_metrics") or {}
    if query_family in family_metrics:
        return metric_id in tuple(family_metrics[query_family] or ())
    allowed_metrics = tuple(capability.get("allowed_metrics") or ())
    required_metrics = tuple(capability.get("required_metrics") or ())
    optional_metrics = tuple(capability.get("optional_metrics") or ())
    reviewed_metrics = (*allowed_metrics, *required_metrics, *optional_metrics)
    return not reviewed_metrics or metric_id in reviewed_metrics


def _bind_overall_channel_reconciliation(
    cases: list[CurrentDataCoverageCase],
) -> list[CurrentDataCoverageCase]:
    by_pair = {
        (case.metric_ids[0], case.dataset_ids[0]): case
        for case in cases
        if len(case.metric_ids) == 1 and len(case.dataset_ids) == 1
    }
    pairs = (
        ("market_dashboard", "market_dashboard_channel"),
        ("gameplay", "gameplay_channel"),
    )
    output = []
    for case in cases:
        replacement = case
        if case.expected_state == "supported" and case.metric_ids:
            for overall_dataset, channel_dataset in pairs:
                if case.dataset_ids != (channel_dataset,):
                    continue
                overall = by_pair.get((case.metric_ids[0], overall_dataset))
                if overall is None or overall.expected_state != "supported":
                    continue
                contract = replace(
                    case.query_contract,
                    reconciliation_binding=ReconciliationBinding(
                        reference_query_role_ref=overall.query_contract.query_role_ref,
                        reference_contract_signature=overall.query_contract.contract_signature,
                    ),
                    contract_signature="",
                )
                contract = replace(
                    contract,
                    contract_signature=query_contract_signature(contract),
                )
                replacement = replace(case, query_contract=contract)
        output.append(replacement)
    return output


def _supported_case(
    registry: RuntimeContractRegistry,
    case_id: str,
    dataset_ids: tuple[str, ...],
    metric_id: str,
    dimension_ids: tuple[str, ...],
    query_family: str,
    claim_ceiling: str,
) -> CurrentDataCoverageCase:
    query_dataset = dataset_ids[-1]
    metrics = (
        () if not metric_id else (_metric_binding(registry, metric_id, query_dataset),)
    )
    dimensions = tuple(
        _dimension_binding(registry, dimension_id, query_dataset)
        for dimension_id in dimension_ids
    )
    shape_contract = registry.query_shape(query_family)
    windows = _coverage_windows(registry)
    required_fields = _dedupe(
        (
            *tuple(shape_contract["required_fields"]),
            *(item.metric_id for item in metrics),
            *dimension_ids,
        )
    )
    unique_key = _dedupe((*tuple(shape_contract["unique_key"]), *dimension_ids))
    grain = _dedupe((*tuple(shape_contract["grain"]), *dimension_ids))
    snapshots, resolver = _snapshots(
        registry, dataset_ids, query_dataset, metrics, dimensions
    )
    selected = next(iter(snapshots.values()))
    contract = QueryContract(
        query_contract_id=f"current-data:{case_id}",
        analysis_contract_ref="current-data-coverage:generated",
        query_intent=query_family,
        dataset_snapshot_refs=(selected.snapshot_ref,),
        metric_bindings=metrics,
        dimension_bindings=dimensions,
        window_refs=tuple(window.window_id for window in windows),
        resolved_windows=windows,
        filters=(),
        result_shape=ResultShape(
            required_fields=required_fields,
            unique_key=unique_key,
            grain=grain,
            required_window_ids=tuple(window.window_id for window in windows),
            result_semantics=str(
                shape_contract.get("result_semantics") or "complete_aggregate"
            ),
            dimension_presence_policy=str(shape_contract["dimension_presence_policy"]),
        ),
        completeness_assertions=_COMPLETENESS,
        workload_class="bounded_readonly",
        contract_signature="",
        query_parameters=dict(shape_contract.get("query_parameters") or {}),
        query_role_ref=f"current-data-role:{case_id}",
        join_expectation=None,
    )
    contract = replace(contract, contract_signature=query_contract_signature(contract))
    source_fields = _dedupe(
        (
            *(field for item in metrics for field in item.required_fields),
            *(item.source_field for item in dimensions),
            *(
                str(item)
                for item in shape_contract.get("source_fields", ())
                if item != "metric_binding"
            ),
        )
    )
    if not source_fields:
        source_fields = tuple(
            str(item)
            for item in registry.dataset(query_dataset).get("required_fields", ())
        )
    return CurrentDataCoverageCase(
        case_id=case_id,
        dataset_ids=dataset_ids,
        metric_ids=tuple(item.metric_id for item in metrics),
        dimension_ids=dimension_ids,
        query_family=query_family,
        required_window_ids=contract.window_refs,
        expected_state="supported",
        claim_ceiling=claim_ceiling,
        source_fields=source_fields,
        window_policy=str(
            shape_contract.get("window_policy") or "fixed_resolved_windows"
        ),
        reconciliation_expectation=str(
            shape_contract.get("reconciliation") or "not_applicable"
        ),
        provider_bounds=str(
            shape_contract.get("provider_bounds") or "throw_on_overflow"
        ),
        query_contract=contract,
        snapshots=snapshots,
        release_resolver=resolver,
    )


def _metric_binding(
    registry: RuntimeContractRegistry, metric_id: str, dataset_id: str
) -> MetricBinding:
    item = registry.metric(metric_id, dataset_id=dataset_id)
    return MetricBinding(
        metric_id,
        str(item["contract_ref"]),
        dataset_id,
        str(item["expression"]),
        str(item["aggregation"]),
        tuple(item["required_fields"]),
        tuple(item["grain"]),
        numerator_metric=str(item.get("numerator_metric") or ""),
        denominator_metric=str(item.get("denominator_metric") or ""),
        claim_types=tuple(item.get("claim_types") or ()),
        reconciliation_tolerance=float(item.get("reconciliation_tolerance") or 0),
        reconciliation_strategy=str(
            item.get("reconciliation_strategy") or "unsupported_non_additive"
        ),
        value_semantics=str(item.get("value_semantics") or "raw_scalar"),
        display_format=str(item.get("display_format") or "number"),
    )


def _dimension_binding(
    registry: RuntimeContractRegistry, dimension_id: str, dataset_id: str
) -> DimensionBinding:
    item = registry.dimension(dimension_id, dataset_id=dataset_id)
    return DimensionBinding(
        dimension_id,
        str(item["contract_ref"]),
        dataset_id,
        str(item["source_field"]),
        tuple(item["allowed_grains"]),
        str(item.get("null_bucket") or "Unknown"),
    )


def _snapshots(
    registry: RuntimeContractRegistry,
    dataset_ids: tuple[str, ...],
    query_dataset: str,
    metrics: tuple[MetricBinding, ...],
    dimensions: tuple[DimensionBinding, ...],
) -> tuple[Mapping[str, DatasetSnapshot], _CoverageReleaseResolver | None]:
    release_dataset_ids = tuple(
        registry.dataset(query_dataset)
        .get("release_membership", {})
        .get("dataset_ids", dataset_ids)
    )
    members = []
    for index, dataset_id in enumerate(release_dataset_ids):
        dataset = registry.dataset(dataset_id)
        fingerprint = f"{index + 1:064x}"
        schema_fields = tuple(dataset.get("schema_fields") or ())
        prefix = str(dataset.get("physical_table_prefix") or "")
        members.append(
            DatasetSnapshot(
                snapshot_ref=f"snapshot:current-data:{dataset_id}",
                dataset_id=dataset_id,
                physical_table=(
                    f"{prefix}{fingerprint[:16]}"
                    if prefix
                    else f"analytics.{dataset_id}"
                ),
                watermark="2026-06-02",
                schema_fingerprint=fingerprint,
                schema_fields=schema_fields,
                contract_ref=str(
                    dataset.get("schema_contract_ref")
                    or f"runtime-dataset:{dataset_id}"
                ),
                loaded_at="2026-06-03T00:00:00Z",
                status="active",
                evidence_state="claim_ready",
                reconciliation_status=(
                    "matched" if len(release_dataset_ids) > 1 else "not_applicable"
                ),
                reconciliation_ref=(
                    f"reconciliation:{'-'.join(release_dataset_ids)}"
                    if len(release_dataset_ids) > 1
                    else ""
                ),
                logical_snapshot_id="current-data-logical",
                load_revision="current-data-load:sha256:reviewed",
                rows_content_hash=(str(index + 1) * 64)[:64],
                snapshot_id="current-data-logical",
            )
        )
    release_ref = dataset_snapshot_release_ref(
        "current-data-logical",
        "current-data-load:sha256:reviewed",
        tuple(item.snapshot_ref for item in members),
    )
    members = [replace(item, release_ref=release_ref) for item in members]
    record = build_dataset_release_authority_record(
        tuple({**item.to_dict(), "requires_release": True} for item in members)
    )
    selected = next(item for item in members if item.dataset_id == query_dataset)
    selected = replace(selected, authority_record_ref=record.authority_record_ref)
    return {selected.snapshot_ref: selected}, _CoverageReleaseResolver(record)


def _dedupe(values: tuple[Any, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(item) for item in values))


def _coverage_windows(
    registry: RuntimeContractRegistry,
) -> tuple[ResolvedWindow, ...]:
    windows: dict[str, ResolvedWindow] = {}
    for baseline_id in CANONICAL_BASELINE_IDS:
        decision = DecisionRecord.create(
            intent_revision_id="intent-current-data-coverage",
            slot_id="comparison_baseline",
            value={"baseline_id": baseline_id},
            source="system",
            status="inferred",
            materiality="material",
            affected_plan_fields=("resolved_window_refs",),
            option_id=f"comparison_baseline.{baseline_id}",
        )
        temporal_authority = resolve_effective_comparison(
            time_spec={"kind": "date", "target": "2026-06-02"},
            comparison_spec={
                "kind": "decision_slot",
                "slot_id": "comparison_baseline",
            },
            decision_ledger=DecisionLedger().append(decision),
            require_physical_baseline=True,
        )
        resolved = resolve_temporal_windows(
            temporal_authority,
            context_window_specs=(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            timezone_name=registry.business_timezone,
            dataset_watermarks={"coverage": date.fromisoformat("2026-06-02")},
            affected_capabilities=(),
            affected_claim_types=(),
        )
        for window in resolved.windows:
            existing = windows.get(window.window_id)
            if existing is not None and existing != window:
                raise ValueError(
                    f"current_data_temporal_window_conflict:{window.window_id}"
                )
            windows[window.window_id] = window
    return tuple(windows.values())
