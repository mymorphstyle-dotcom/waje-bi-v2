from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any, Mapping, Sequence

from bi_agent.runtime.analysis_contracts import (
    CapabilityExecutionPlan,
    CapabilityInputSlot,
    CompletenessReport,
    QueryContract,
    QueryResultEnvelope,
)
from bi_agent.runtime.clickhouse_query_compiler import (
    validate_clickhouse_query_contract,
)
from bi_agent.runtime.canonical_values import canonical_thaw
from bi_agent.runtime.evidence_authority import (
    CapabilityBindingRecord,
    EvidenceIntegrityError,
    QueryExecutionRecord,
    RowsPayloadLoader,
    RuntimeEvidenceResolver,
    canonical_digest,
    canonical_result_rows_hash_matches,
    canonical_rows_storage_ref,
    runtime_evidence_record_integrity_errors,
)
from bi_agent.runtime.query_completeness import (
    validate_query_result,
    validate_query_set,
)
from bi_agent.runtime.dataset_catalog import DatasetReleaseResolver
from bi_agent.runtime.runtime_contract_registry import (
    RuntimeContractRegistry,
    runtime_registry_integrity_error,
)
from bi_agent.runtime.degradation_policy import (
    degraded_binding_projection_is_authorized,
    ready_binding_projection_is_authorized,
)


class AuthoritativeQueryChainError(EvidenceIntegrityError):
    pass


@dataclass(frozen=True)
class ValidatedAuthoritativeQueryChain:
    binding: CapabilityBindingRecord
    primary_results: tuple[QueryResultEnvelope, ...]
    validation_results: tuple[QueryResultEnvelope, ...]
    primary_reports: tuple[CompletenessReport, ...]
    validation_reports: tuple[CompletenessReport, ...]
    query_records: Mapping[str, QueryExecutionRecord]
    rows_by_ref: Mapping[str, tuple[Mapping[str, Any], ...]]


@dataclass(frozen=True)
class _ResolvedItem:
    query_record: QueryExecutionRecord
    result: QueryResultEnvelope
    report: CompletenessReport
    rows: tuple[Mapping[str, Any], ...]


def validate_authoritative_query_chain(
    binding: CapabilityBindingRecord,
    *,
    resolver: RuntimeEvidenceResolver,
    rows_loader: RowsPayloadLoader,
    runtime_registry: RuntimeContractRegistry,
    release_resolver: DatasetReleaseResolver | None = None,
) -> ValidatedAuthoritativeQueryChain:
    registry_error = runtime_registry_integrity_error(runtime_registry)
    if registry_error:
        raise AuthoritativeQueryChainError(registry_error)
    if type(binding) is not CapabilityBindingRecord:
        raise AuthoritativeQueryChainError("capability_binding_record_type_invalid")
    _require_clean_record(binding, "capability_binding_record_integrity")
    if not isinstance(resolver, RuntimeEvidenceResolver):
        raise AuthoritativeQueryChainError("runtime_evidence_resolver_invalid")
    if not isinstance(rows_loader, RowsPayloadLoader):
        raise AuthoritativeQueryChainError("rows_payload_loader_invalid")

    primary = _resolve_group(
        _binding_group(binding, validation=False),
        allowed_snapshot_refs=set(binding.source_snapshot_refs),
        resolver=resolver,
        rows_loader=rows_loader,
        runtime_registry=runtime_registry,
        release_resolver=release_resolver,
    )
    validation = _resolve_group(
        _binding_group(binding, validation=True),
        allowed_snapshot_refs=set(binding.validation_source_snapshot_refs),
        resolver=resolver,
        rows_loader=rows_loader,
        runtime_registry=runtime_registry,
        release_resolver=release_resolver,
    )
    items = (*primary, *validation)
    by_query_ref = {item.query_record.query_contract_ref: item for item in items}
    if len(by_query_ref) != len(items):
        raise AuthoritativeQueryChainError("authoritative_query_ref_duplicate")
    validate_capability_binding_plan_semantics(
        binding,
        runtime_registry,
        {ref: item.query_record.contract for ref, item in by_query_ref.items()},
    )
    ordered_refs = _binding_query_order(binding)
    ordered = tuple(by_query_ref[ref] for ref in ordered_refs)
    base_reports = tuple(
        validate_query_result(
            item.query_record.contract,
            item.result,
            _resolved_snapshots(item.query_record, resolver),
            release_resolver=release_resolver,
        )
        for item in ordered
    )
    recomputed = validate_query_set(
        tuple(item.query_record.contract for item in ordered),
        tuple(item.result for item in ordered),
        base_reports,
    )
    recomputed_by_query = {report.query_contract_ref: report for report in recomputed}
    for item in items:
        expected = recomputed_by_query.get(item.report.query_contract_ref)
        if expected is None or canonical_digest(expected.to_dict()) != canonical_digest(
            item.report.to_dict()
        ):
            raise AuthoritativeQueryChainError(
                f"completeness_report_recomputation_mismatch:"
                f"{item.report.query_contract_ref}"
            )
    expected_statuses = tuple(item.report.completeness_status for item in items)
    if expected_statuses != binding.input_completeness_statuses:
        raise AuthoritativeQueryChainError(
            "capability_binding_completeness_statuses_mismatch"
        )
    return ValidatedAuthoritativeQueryChain(
        binding=binding,
        primary_results=tuple(item.result for item in primary),
        validation_results=tuple(item.result for item in validation),
        primary_reports=tuple(item.report for item in primary),
        validation_reports=tuple(item.report for item in validation),
        query_records={
            item.query_record.result_ref: item.query_record for item in items
        },
        rows_by_ref={item.result.rows_ref: item.rows for item in items},
    )


def _binding_group(
    binding: CapabilityBindingRecord,
    *,
    validation: bool,
) -> tuple[tuple[Any, ...], ...]:
    prefix = "validation_" if validation else ""
    fields = (
        "query_contract_refs",
        "result_refs",
        "query_execution_record_refs",
        "query_execution_record_digests",
        "rows_refs",
        "rows_metadata_record_refs",
        "rows_metadata_record_digests",
        "rows_content_hashes",
        "completeness_report_refs",
        "completeness_record_refs",
        "completeness_record_digests",
    )
    values = tuple(tuple(getattr(binding, f"{prefix}{field}")) for field in fields)
    lengths = {len(value) for value in values}
    if len(lengths) != 1:
        raise AuthoritativeQueryChainError(
            f"{prefix}capability_binding_ref_cardinality"
        )
    return tuple(zip(*values))


def _resolve_group(
    entries: Sequence[tuple[Any, ...]],
    *,
    allowed_snapshot_refs: set[str],
    resolver: RuntimeEvidenceResolver,
    rows_loader: RowsPayloadLoader,
    runtime_registry: RuntimeContractRegistry,
    release_resolver: DatasetReleaseResolver | None,
) -> tuple[_ResolvedItem, ...]:
    resolved = []
    observed_snapshot_refs: set[str] = set()
    for entry in entries:
        (
            query_ref,
            result_ref,
            query_record_ref,
            query_record_digest,
            rows_ref,
            rows_record_ref,
            rows_record_digest,
            rows_hash,
            report_ref,
            completeness_record_ref,
            completeness_record_digest,
        ) = (str(value) for value in entry)
        query = resolver.resolve_query_execution_record(query_record_ref)
        if type(query) is not QueryExecutionRecord:
            raise AuthoritativeQueryChainError("query_execution_record_missing")
        _require_clean_record(query, "query_execution_record_integrity")
        if (
            query.record_digest != query_record_digest
            or query.query_contract_ref != query_ref
            or query.result_ref != result_ref
            or query.rows_ref != rows_ref
            or query.completeness_report_ref != report_ref
        ):
            raise AuthoritativeQueryChainError(
                "query_execution_record_binding_mismatch"
            )

        rows_record = resolver.resolve_rows_record(rows_record_ref)
        if rows_record is None:
            raise AuthoritativeQueryChainError("rows_record_missing")
        _require_clean_record(rows_record, "rows_record_integrity")
        if (
            rows_record.record_digest != rows_record_digest
            or rows_record.rows_ref != rows_ref
            or not rows_record.storage_ref
            or rows_record.row_count != query.row_count
            or rows_record.unique_key_fields != query.contract.result_shape.unique_key
            or rows_record.rows_content_hash != rows_hash
            or rows_hash != query.rows_content_hash
        ):
            raise AuthoritativeQueryChainError("rows_record_binding_mismatch")
        loaded_rows = rows_loader.load_rows(rows_record.storage_ref)
        if loaded_rows is None or any(
            not isinstance(row, Mapping) for row in loaded_rows
        ):
            raise AuthoritativeQueryChainError("rows_payload_missing")
        rows = tuple(dict(row) for row in loaded_rows)
        if len(rows) != query.row_count:
            raise AuthoritativeQueryChainError("rows_payload_count_mismatch")
        try:
            hash_matches = canonical_result_rows_hash_matches(
                rows,
                query.contract.result_shape.unique_key,
                rows_hash,
            )
        except EvidenceIntegrityError as exc:
            raise AuthoritativeQueryChainError(f"rows_payload_invalid:{exc}") from exc
        if not hash_matches:
            raise AuthoritativeQueryChainError("rows_payload_hash_mismatch")
        if canonical_rows_storage_ref(rows) != rows_record.storage_ref:
            raise AuthoritativeQueryChainError("rows_storage_ref_content_mismatch")

        snapshots = _resolved_snapshots(query, resolver)
        observed_snapshot_refs.update(snapshots)
        try:
            validate_clickhouse_query_contract(
                query.contract,
                snapshots,
                registry=runtime_registry,
                release_resolver=release_resolver,
            )
        except (PermissionError, TypeError, ValueError) as exc:
            raise AuthoritativeQueryChainError("query_contract_runtime_policy") from exc
        result = _result_from_record(query, rows)
        if canonical_digest(result.to_dict()) != canonical_digest(query.result_payload):
            raise AuthoritativeQueryChainError("query_result_payload_mismatch")

        completeness = resolver.resolve_completeness(completeness_record_ref)
        if completeness is None:
            raise AuthoritativeQueryChainError("completeness_record_missing")
        _require_clean_record(completeness, "completeness_record_integrity")
        if (
            completeness.report_digest != completeness_record_digest
            or completeness.report_ref != report_ref
            or completeness.query_contract_ref != query_ref
            or completeness.result_ref != result_ref
        ):
            raise AuthoritativeQueryChainError("completeness_record_binding_mismatch")
        report = _report_from_record(completeness.report_payload)
        _validate_report_links(report, query, result)
        resolved.append(
            _ResolvedItem(
                query_record=query,
                result=result,
                report=report,
                rows=rows,
            )
        )
    if observed_snapshot_refs != allowed_snapshot_refs:
        raise AuthoritativeQueryChainError("source_snapshot_refs_mismatch")
    return tuple(resolved)


def _resolved_snapshots(
    query: QueryExecutionRecord,
    resolver: RuntimeEvidenceResolver,
) -> dict[str, Any]:
    if not (
        len(query.source_snapshot_refs)
        == len(query.source_snapshot_record_refs)
        == len(query.source_snapshot_record_digests)
    ):
        raise AuthoritativeQueryChainError("query_snapshot_record_cardinality")
    snapshots = {}
    for snapshot_ref, record_ref, digest in zip(
        query.source_snapshot_refs,
        query.source_snapshot_record_refs,
        query.source_snapshot_record_digests,
    ):
        record = resolver.resolve_snapshot(snapshot_ref)
        if record is None:
            raise AuthoritativeQueryChainError("snapshot_record_missing")
        _require_clean_record(record, "snapshot_record_integrity")
        if record.record_ref != record_ref or record.record_digest != digest:
            raise AuthoritativeQueryChainError("snapshot_record_binding_mismatch")
        snapshots[snapshot_ref] = record.snapshot
    return snapshots


def _result_from_record(
    query: QueryExecutionRecord,
    rows: tuple[Mapping[str, Any], ...],
) -> QueryResultEnvelope:
    payload = canonical_thaw(query.result_payload)
    try:
        return QueryResultEnvelope(
            query_contract_ref=str(payload["query_contract_ref"]),
            query_id=str(payload["query_id"]),
            query_hash=str(payload["query_hash"]),
            result_ref=str(payload["result_ref"]),
            execution_status=str(payload["execution_status"]),
            rows_ref=str(payload["rows_ref"]),
            row_count=int(payload["row_count"]),
            completeness_report_ref=str(payload["completeness_report_ref"]),
            rows=rows,
            observed_schema=canonical_thaw(payload.get("observed_schema") or {}),
            observed_windows=tuple(payload.get("observed_windows") or ()),
            observed_grain=tuple(payload.get("observed_grain") or ()),
            source_snapshot_refs=tuple(payload.get("source_snapshot_refs") or ()),
            provider_stats=canonical_thaw(payload.get("provider_stats") or {}),
            failure_reason=str(payload.get("failure_reason") or ""),
            execution_attempt_ref=str(payload.get("execution_attempt_ref") or ""),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthoritativeQueryChainError("query_result_payload_invalid") from exc


def _report_from_record(payload: Mapping[str, Any]) -> CompletenessReport:
    try:
        return CompletenessReport(
            report_ref=str(payload["report_ref"]),
            query_contract_ref=str(payload["query_contract_ref"]),
            result_ref=str(payload["result_ref"]),
            completeness_status=str(payload["completeness_status"]),
            analysis_readiness=str(payload["analysis_readiness"]),
            assertion_results=tuple(
                canonical_thaw(item) for item in payload.get("assertion_results") or ()
            ),
            failure_reasons=tuple(payload.get("failure_reasons") or ()),
            coverage_summary=canonical_thaw(payload.get("coverage_summary") or {}),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthoritativeQueryChainError(
            "completeness_report_payload_invalid"
        ) from exc


def _validate_report_links(
    report: CompletenessReport,
    query: QueryExecutionRecord,
    result: QueryResultEnvelope,
) -> None:
    coverage = report.coverage_summary
    if (
        report.query_contract_ref != query.query_contract_ref
        or report.result_ref != result.result_ref
        or report.report_ref != result.completeness_report_ref
        or coverage.get("rows_ref") != result.rows_ref
        or coverage.get("row_count") != result.row_count
        or tuple(coverage.get("snapshot_refs") or ()) != result.source_snapshot_refs
        or tuple(coverage.get("required_windows") or ()) != query.contract.window_refs
        or tuple(coverage.get("observed_grain") or ()) != result.observed_grain
    ):
        raise AuthoritativeQueryChainError("completeness_report_link_mismatch")


def _binding_query_order(binding: CapabilityBindingRecord) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            (
                *binding.query_contract_refs,
                *binding.validation_query_contract_refs,
            )
        )
    )


def validate_capability_binding_plan_semantics(
    binding: CapabilityBindingRecord,
    registry: RuntimeContractRegistry,
    query_contracts_by_ref: Mapping[str, QueryContract] | None = None,
) -> None:
    """Validate the reviewed capability plan identity shared by binder consumers."""
    registry_error = runtime_registry_integrity_error(registry)
    if registry_error:
        raise AuthoritativeQueryChainError(registry_error)
    if type(binding) is not CapabilityBindingRecord:
        raise AuthoritativeQueryChainError("capability_binding_record_type_invalid")
    _require_clean_record(binding, "capability_binding_record_integrity")
    plan = binding.plan_payload
    if binding.status == "degraded":
        if not degraded_binding_projection_is_authorized(
            plan,
            binding.binding_payload,
        ):
            raise AuthoritativeQueryChainError(
                "capability_binding_degradation_unauthorized"
            )
        allow_unbound_query_refs = True
    elif binding.status == "ready":
        if not ready_binding_projection_is_authorized(
            plan,
            binding.binding_payload,
        ):
            raise AuthoritativeQueryChainError(
                "capability_binding_ready_projection_invalid"
            )
        allow_unbound_query_refs = False
    else:
        raise AuthoritativeQueryChainError("capability_binding_status_invalid")
    validate_capability_plan_semantics(
        plan,
        registry,
        query_contracts_by_ref,
        allow_unbound_query_refs=allow_unbound_query_refs,
    )
    if (
        str(plan.get("capability_id") or "") != binding.capability_id
        or str(plan.get("analysis_contract_ref") or "") != binding.analysis_contract_ref
        or tuple(binding.supported_claim_types)
        != tuple(plan.get("supported_claim_types") or ())
        or tuple(binding.supported_evidence_types)
        != tuple(plan.get("supported_evidence_types") or ())
        or binding.maximum_claim_strength
        != str(plan.get("maximum_claim_strength") or "")
        or binding.maximum_claim_strength_rank
        != int(plan.get("maximum_claim_strength_rank", -1))
        or binding.claim_strength_taxonomy_version
        != str(plan.get("claim_strength_taxonomy_version") or "")
    ):
        raise AuthoritativeQueryChainError("capability_contract_plan_identity_mismatch")
    if query_contracts_by_ref is None:
        return
    ordered_refs = _binding_query_order(binding)
    if set(ordered_refs) != set(query_contracts_by_ref) or len(ordered_refs) != len(
        query_contracts_by_ref
    ):
        raise AuthoritativeQueryChainError(
            "capability_binding_plan_query_refs_mismatch"
        )


def validate_capability_plan_semantics(
    plan: CapabilityExecutionPlan | Mapping[str, Any],
    registry: RuntimeContractRegistry,
    query_contracts_by_ref: Mapping[str, QueryContract] | None = None,
    *,
    allow_unbound_query_refs: bool = False,
) -> None:
    registry_error = runtime_registry_integrity_error(registry)
    if registry_error:
        raise AuthoritativeQueryChainError(registry_error)
    payload = asdict(plan) if type(plan) is CapabilityExecutionPlan else plan
    if not isinstance(payload, Mapping) or set(payload) != {
        field.name for field in fields(CapabilityExecutionPlan)
    }:
        raise AuthoritativeQueryChainError("capability_contract_plan_schema_invalid")
    capability_id = str(payload.get("capability_id") or "")
    try:
        capability = registry.capability_inputs(capability_id)
        maximum = str(capability.get("maximum_claim_strength") or "")
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthoritativeQueryChainError(
            "capability_contract_policy_invalid"
        ) from exc
    if (
        str(payload.get("capability_contract_ref") or "")
        != registry.capability_contract_ref(capability_id)
        or str(payload.get("capability_contract_version") or "")
        != registry.contract_version
        or str(payload.get("capability_contract_signature") or "")
        != registry.capability_contract_signature(capability_id)
        or not str(payload.get("analysis_contract_ref") or "")
        or str(payload.get("merge_strategy") or "")
        != str(capability.get("merge_strategy") or "by_query_family")
        or canonical_digest(payload.get("minimum_readiness"))
        != canonical_digest(capability.get("minimum_readiness") or {})
        or canonical_digest(payload.get("degradation_policy"))
        != canonical_digest(capability.get("degradation_policy") or {})
        or tuple(payload.get("supported_claim_types") or ())
        != tuple(capability.get("supported_claim_types") or ())
        or tuple(payload.get("supported_evidence_types") or ())
        != tuple(capability.get("supported_evidence_types") or ())
        or str(payload.get("maximum_claim_strength") or "") != maximum
        or int(payload.get("maximum_claim_strength_rank", -1))
        != registry.maximum_claim_strength_rank(maximum)
        or str(payload.get("claim_strength_taxonomy_version") or "")
        != registry.claim_strength_taxonomy_version
    ):
        raise AuthoritativeQueryChainError("capability_contract_plan_policy_mismatch")
    _validate_capability_slots(
        payload,
        registry,
        query_contracts_by_ref,
        allow_unbound_query_refs=allow_unbound_query_refs,
    )


def _validate_capability_slots(
    plan: Mapping[str, Any],
    registry: RuntimeContractRegistry,
    by_query_ref: Mapping[str, QueryContract] | None,
    *,
    allow_unbound_query_refs: bool = False,
) -> None:
    capability = registry.capability_inputs(str(plan.get("capability_id") or ""))
    query_families = tuple(capability.get("query_families") or ())
    optional_families = set(capability.get("optional_query_families") or ())
    accepted = tuple(
        (capability.get("minimum_readiness") or {}).get("accepted_completeness") or ()
    )
    canonical_required_windows = tuple(capability.get("required_windows") or ())
    family_metrics = capability.get("query_family_metrics") or {}
    default_metrics = tuple(capability.get("required_metrics") or ())
    raw_slots = tuple(
        slot
        for field in ("required_input_slots", "optional_input_slots")
        for slot in plan.get(field) or ()
    )
    if any(not isinstance(slot, Mapping) for slot in raw_slots):
        raise AuthoritativeQueryChainError("capability_contract_slot_schema_invalid")
    slots = raw_slots
    if any(
        set(slot) != {field.name for field in fields(CapabilityInputSlot)}
        for slot in slots
    ):
        raise AuthoritativeQueryChainError("capability_contract_slot_schema_invalid")
    slot_families = tuple(_slot_query_family(slot) for slot in slots)
    if set(slot_families) != set(query_families) or any(
        family not in query_families for family in slot_families
    ):
        raise AuthoritativeQueryChainError(
            "capability_contract_slot_query_families_mismatch"
        )
    family_counts = {
        family: sum(item == family for item in slot_families)
        for family in query_families
    }
    for slot, family in zip(slots, slot_families):
        primary_refs = tuple(str(ref) for ref in slot.get("query_contract_refs") or ())
        validation_refs = tuple(
            str(ref) for ref in slot.get("validation_query_contract_refs") or ()
        )
        required = bool(slot.get("required"))
        if required != (family not in optional_families):
            raise AuthoritativeQueryChainError(
                f"capability_contract_slot_requiredness_mismatch:{family}"
            )
        if tuple(slot.get("accepted_completeness") or ()) != accepted:
            raise AuthoritativeQueryChainError(
                f"capability_contract_slot_readiness_mismatch:{family}"
            )
        if not primary_refs:
            if (
                validation_refs
                or str(slot.get("slot_id") or "") != family
                or tuple(slot.get("required_window_ids") or ())
                != canonical_required_windows
                or tuple(slot.get("required_fields") or ())
            ):
                raise AuthoritativeQueryChainError(
                    f"capability_contract_slot_validation_dependency_mismatch:{family}"
                )
            continue
        if by_query_ref is None:
            continue
        primaries = tuple(by_query_ref.get(ref) for ref in primary_refs)
        if any(item is None for item in primaries):
            if allow_unbound_query_refs and all(item is None for item in primaries):
                continue
            raise AuthoritativeQueryChainError(
                f"capability_contract_slot_query_missing:{family}"
            )
        expected_metrics = tuple(family_metrics.get(family) or default_metrics)
        expected_validation_refs = []
        for item in primaries:
            contract = item
            if contract.query_intent != family:
                raise AuthoritativeQueryChainError(
                    f"capability_contract_slot_query_intent_mismatch:{family}"
                )
            actual_metrics = tuple(
                metric.metric_id for metric in contract.metric_bindings
            )
            if not _capability_metrics_match(
                capability,
                actual_metrics,
                expected_metrics=expected_metrics,
            ):
                raise AuthoritativeQueryChainError(
                    f"capability_contract_slot_metrics_mismatch:{family}"
                )
            expected_windows = (
                canonical_required_windows or contract.result_shape.required_window_ids
            )
            if tuple(slot.get("required_window_ids") or ()) != tuple(expected_windows):
                raise AuthoritativeQueryChainError(
                    f"capability_contract_slot_windows_mismatch:{family}"
                )
            if tuple(slot.get("required_fields") or ()) != tuple(
                contract.result_shape.required_fields
            ):
                raise AuthoritativeQueryChainError(
                    f"capability_contract_slot_result_shape_mismatch:{family}"
                )
            expected_slot_id = family
            if family_counts[family] > 1:
                dimension_suffix = "+".join(
                    dimension.dimension_id for dimension in contract.dimension_bindings
                )
                expected_slot_id = (
                    f"{family}:{dimension_suffix or contract.query_contract_id}"
                )
            if str(slot.get("slot_id") or "") != expected_slot_id:
                raise AuthoritativeQueryChainError(
                    f"capability_contract_slot_id_mismatch:{family}"
                )
            reconciliation = contract.reconciliation_binding
            if reconciliation is not None:
                companions = tuple(
                    ref
                    for ref, candidate in by_query_ref.items()
                    if candidate.query_role_ref
                    == reconciliation.reference_query_role_ref
                    and candidate.contract_signature
                    == reconciliation.reference_contract_signature
                )
                expected_validation_refs.extend(companions)
        if tuple(expected_validation_refs) != validation_refs:
            raise AuthoritativeQueryChainError(
                f"capability_contract_slot_validation_dependency_mismatch:{family}"
            )


def _capability_metrics_match(
    capability: Mapping[str, Any],
    actual_metrics: tuple[str, ...],
    *,
    expected_metrics: tuple[str, ...],
) -> bool:
    if str(capability.get("metric_mode") or "") != "requested":
        return bool(
            len(actual_metrics) == len(expected_metrics)
            and len(set(actual_metrics)) == len(actual_metrics)
            and len(set(expected_metrics)) == len(expected_metrics)
            and set(actual_metrics) == set(expected_metrics)
        )
    allowed_metrics = tuple(
        str(metric_id)
        for metric_id in capability.get("allowed_metrics") or ()
        if metric_id
    )
    return (
        bool(actual_metrics)
        and len(set(actual_metrics)) == len(actual_metrics)
        and bool(allowed_metrics)
        and set(actual_metrics).issubset(allowed_metrics)
    )


def _slot_query_family(slot: Mapping[str, Any]) -> str:
    return str(slot.get("slot_id") or "").split(":", 1)[0]


def _require_clean_record(record: Any, code: str) -> None:
    if runtime_evidence_record_integrity_errors(record):
        raise AuthoritativeQueryChainError(code)
