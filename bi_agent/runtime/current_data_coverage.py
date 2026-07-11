from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

from bi_agent.runtime.analysis_contracts import (
    DimensionBinding,
    MetricBinding,
    QueryContract,
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
from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry
from bi_agent.runtime.query_completeness import CURRENT_DATA_ASSERTIONS


_WINDOWS = (
    ResolvedWindow("target_day", "target", "target day", "2026-06-02", "2026-06-03", "Africa/Lagos", "daily_total", 1, "2026-06-02"),
    ResolvedWindow("previous_day", "baseline", "previous day", "2026-06-01", "2026-06-02", "Africa/Lagos", "daily_total", 1, "2026-06-01"),
    ResolvedWindow("rolling_7_day_baseline", "baseline", "rolling seven days", "2026-05-26", "2026-06-02", "Africa/Lagos", "mean_of_complete_days", 7, "2026-06-01"),
    ResolvedWindow("same_weekday_last_week", "baseline", "same weekday last week", "2026-05-26", "2026-05-27", "Africa/Lagos", "daily_total", 1, "2026-05-26"),
)
_COMPLETENESS = CURRENT_DATA_ASSERTIONS


class _CoverageReleaseResolver:
    def __init__(self, record: DatasetReleaseAuthorityRecord) -> None:
        self._record = record

    def resolve_dataset_release(self, release_ref: str) -> DatasetReleaseAuthorityRecord:
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
    supported_specs = (
        ("market-overall", ("market_dashboard",), "paid_amount", (), "daily_metric_baselines", "directional"),
        ("market-channel", ("market_dashboard", "market_dashboard_channel"), "paid_amount", ("channel",), "channel_context_probe", "directional"),
        ("gameplay-overall", ("gameplay",), "player_bet_amount", ("gameplay",), "gameplay_activity_probe", "candidate_mechanism"),
        ("gameplay-channel", ("gameplay", "gameplay_channel"), "player_bet_amount", ("gameplay",), "gameplay_activity_probe", "candidate_mechanism"),
        ("external-event", ("external_event",), "", (), "event_context_probe", "candidate_mechanism"),
        ("internal-event", ("internal_operation_event",), "", (), "event_context_probe", "candidate_mechanism"),
        ("paid-success", ("paid_order_success",), "paid_amount", (), "daily_metric_baselines", "directional"),
    )
    cases = [
        _supported_case(registry, *spec)
        for spec in supported_specs
    ]

    supported_families = {case.query_family for case in cases}
    obligation_families = {
        str(query_family)
        for capability_id in registry.capability_ids
        for query_family in registry.capability_inputs(capability_id).get("query_families", ())
    }
    for query_family in sorted(obligation_families - supported_families):
        cases.append(
            CurrentDataCoverageCase(
                case_id=f"gap:query-family:{query_family}",
                dataset_ids=(),
                metric_ids=(),
                dimension_ids=(),
                query_family=query_family,
                required_window_ids=tuple(window.window_id for window in _WINDOWS),
                expected_state="degraded",
                claim_ceiling="insufficient",
                gap_type="schema_backed_query_adapter_missing",
                owner="analysis_contract_owner",
            )
        )

    covered_sources = {dataset for case in cases for dataset in case.dataset_ids}
    registered_sources = {
        dataset_id
        for metric_id in registry.metric_ids
        for dataset_id in registry.metric_sources(metric_id)
    }
    for dataset_id in sorted(registered_sources - covered_sources):
        cases.append(
            CurrentDataCoverageCase(
                case_id=f"gap:source:{dataset_id}",
                dataset_ids=(dataset_id,),
                metric_ids=tuple(
                    metric_id
                    for metric_id in registry.metric_ids
                    if dataset_id in registry.metric_sources(metric_id)
                ),
                dimension_ids=(),
                query_family="payment_success_scan" if dataset_id == "payment_attempt" else "data_quality_probe",
                required_window_ids=tuple(window.window_id for window in _WINDOWS),
                expected_state="degraded",
                claim_ceiling="insufficient",
                gap_type="active_source_schema_not_reviewed",
                owner="source_contract_owner",
            )
        )
    return tuple(cases)


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
    metrics = (() if not metric_id else (_metric_binding(registry, metric_id, query_dataset),))
    dimensions = tuple(
        _dimension_binding(registry, dimension_id, query_dataset)
        for dimension_id in dimension_ids
    )
    shape_contract = registry.query_shape(query_family)
    windows = _WINDOWS
    required_fields = _dedupe(
        (*tuple(shape_contract["required_fields"]), *(item.metric_id for item in metrics), *dimension_ids)
    )
    unique_key = _dedupe((*tuple(shape_contract["unique_key"]), *dimension_ids))
    grain = _dedupe((*tuple(shape_contract["grain"]), *dimension_ids))
    snapshots, resolver = _snapshots(registry, dataset_ids, query_dataset, metrics, dimensions)
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
            result_semantics=str(shape_contract.get("result_semantics") or "complete_aggregate"),
            dimension_presence_policy=str(shape_contract["dimension_presence_policy"]),
        ),
        completeness_assertions=_COMPLETENESS,
        permission_scope="analyst",
        workload_class="bounded_readonly",
        contract_signature="",
        query_parameters=dict(shape_contract.get("query_parameters") or {}),
        join_expectation=None,
    )
    contract = replace(contract, contract_signature=query_contract_signature(contract))
    source_fields = _dedupe(
        (
            *(field for item in metrics for field in item.required_fields),
            *(item.source_field for item in dimensions),
            *(str(item) for item in shape_contract.get("source_fields", ()) if item != "metric_binding"),
        )
    )
    if not source_fields:
        source_fields = tuple(str(item) for item in registry.dataset(query_dataset).get("schema_fields", ()))
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
        window_policy=str(shape_contract.get("window_policy") or "fixed_resolved_windows"),
        reconciliation_expectation=str(shape_contract.get("reconciliation") or "not_applicable"),
        provider_bounds=str(shape_contract.get("provider_bounds") or "throw_on_overflow"),
        query_contract=contract,
        snapshots=snapshots,
        release_resolver=resolver,
    )


def _metric_binding(registry: RuntimeContractRegistry, metric_id: str, dataset_id: str) -> MetricBinding:
    item = registry.metric(metric_id, dataset_id=dataset_id)
    return MetricBinding(
        metric_id, str(item["contract_ref"]), dataset_id, str(item["expression"]),
        str(item["aggregation"]), tuple(item["required_fields"]), tuple(item["grain"]),
        numerator_metric=str(item.get("numerator_metric") or ""),
        denominator_metric=str(item.get("denominator_metric") or ""),
        claim_types=tuple(item.get("claim_types") or ()),
        reconciliation_tolerance=float(item.get("reconciliation_tolerance") or 0),
        reconciliation_strategy=str(item.get("reconciliation_strategy") or "unsupported_non_additive"),
        value_semantics=str(item.get("value_semantics") or "raw_scalar"),
        display_format=str(item.get("display_format") or "number"),
    )


def _dimension_binding(registry: RuntimeContractRegistry, dimension_id: str, dataset_id: str) -> DimensionBinding:
    item = registry.dimension(dimension_id, dataset_id=dataset_id)
    return DimensionBinding(
        dimension_id, str(item["contract_ref"]), dataset_id, str(item["source_field"]),
        tuple(item["allowed_grains"]), str(item.get("null_bucket") or "Unknown"),
        str(item.get("permission_scope") or "analyst"),
    )


def _snapshots(
    registry: RuntimeContractRegistry,
    dataset_ids: tuple[str, ...],
    query_dataset: str,
    metrics: tuple[MetricBinding, ...],
    dimensions: tuple[DimensionBinding, ...],
) -> tuple[Mapping[str, DatasetSnapshot], _CoverageReleaseResolver | None]:
    release_dataset_ids = tuple(
        registry.dataset(query_dataset).get("release_membership", {}).get(
            "dataset_ids", dataset_ids
        )
    )
    members = []
    for index, dataset_id in enumerate(release_dataset_ids):
        dataset = registry.dataset(dataset_id)
        fingerprint = f"{index + 1:064x}"
        schema_fields = tuple(dataset.get("schema_fields") or ())
        if dataset_id == query_dataset:
            schema_fields = _dedupe(
                (*schema_fields, *dataset.get("required_fields", ()), *(field for item in metrics for field in item.required_fields), *(item.source_field for item in dimensions))
            )
        prefix = str(dataset.get("physical_table_prefix") or "")
        members.append(
            DatasetSnapshot(
                snapshot_ref=f"snapshot:current-data:{dataset_id}", dataset_id=dataset_id,
                physical_table=(f"{prefix}{fingerprint[:16]}" if prefix else f"analytics.{dataset_id}"),
                watermark="2026-06-02", schema_fingerprint=fingerprint,
                schema_fields=schema_fields, contract_ref=f"runtime-dataset:{dataset_id}",
                permission_scopes=("analyst",), loaded_at="2026-06-03T00:00:00Z", status="active",
                evidence_state="claim_ready", reconciliation_status=("matched" if len(release_dataset_ids) > 1 else "not_applicable"),
                reconciliation_ref=(f"reconciliation:{'-'.join(release_dataset_ids)}" if len(release_dataset_ids) > 1 else ""),
                logical_snapshot_id="current-data-logical", load_revision="current-data-load:sha256:reviewed",
                rows_content_hash=(str(index + 1) * 64)[:64], snapshot_id="current-data-logical",
            )
        )
    release_ref = dataset_snapshot_release_ref(
        "current-data-logical", "current-data-load:sha256:reviewed",
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
