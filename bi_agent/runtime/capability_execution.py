from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from bi_agent.runtime.analysis_contracts import (
    CapabilityExecutionPlan,
    CapabilityInputSlot,
    CompletenessReport,
    QueryResultEnvelope,
)
from bi_agent.runtime.canonical_values import canonical_thaw
from bi_agent.runtime.authoritative_query_chain import (
    AuthoritativeQueryChainError,
    validate_authoritative_query_chain,
    validate_capability_plan_semantics,
)
from bi_agent.runtime.evidence_authority import (
    EvidenceIntegrityError,
    RowsPayloadLoader,
    RuntimeEvidenceAuthority,
    RuntimeEvidenceResolver,
    RuntimeEvidenceWriter,
    _record_capability_binding,
    canonical_value,
    canonical_digest,
    canonical_rows_hash,
    legacy_fixture_enabled,
    runtime_evidence_record_integrity_errors,
)
from bi_agent.runtime.query_completeness import (
    validate_query_result,
    validate_query_set,
)
from bi_agent.runtime.runtime_contract_registry import (
    RuntimeContractRegistry,
    runtime_registry_integrity_error,
)
from bi_agent.runtime.dataset_catalog import (
    DatasetReleaseResolver,
    canonical_dataset_requires_release,
)


_BOUND_CONSTRUCTION_TOKEN = object()


@dataclass(frozen=True, init=False)
class BoundCapabilityInput:
    capability_id: str
    capability_contract_ref: str
    capability_contract_version: str
    capability_contract_signature: str
    analysis_contract_ref: str
    status: str
    rows_by_slot: Mapping[str, tuple[Mapping[str, Any], ...]]
    reasons: tuple[str, ...]
    query_contract_refs: tuple[str, ...]
    result_refs: tuple[str, ...]
    query_execution_record_refs: tuple[str, ...]
    query_execution_record_digests: tuple[str, ...]
    rows_refs: tuple[str, ...]
    rows_metadata_record_refs: tuple[str, ...]
    rows_metadata_record_digests: tuple[str, ...]
    rows_content_hashes: tuple[str, ...]
    completeness_report_refs: tuple[str, ...]
    completeness_record_refs: tuple[str, ...]
    completeness_record_digests: tuple[str, ...]
    source_snapshot_refs: tuple[str, ...]
    validation_query_contract_refs: tuple[str, ...]
    validation_result_refs: tuple[str, ...]
    validation_query_execution_record_refs: tuple[str, ...]
    validation_query_execution_record_digests: tuple[str, ...]
    validation_rows_refs: tuple[str, ...]
    validation_rows_metadata_record_refs: tuple[str, ...]
    validation_rows_metadata_record_digests: tuple[str, ...]
    validation_rows_content_hashes: tuple[str, ...]
    validation_completeness_report_refs: tuple[str, ...]
    validation_completeness_record_refs: tuple[str, ...]
    validation_completeness_record_digests: tuple[str, ...]
    validation_source_snapshot_refs: tuple[str, ...]
    supported_evidence_types: tuple[str, ...]
    supported_claim_types: tuple[str, ...]
    maximum_claim_strength: str
    maximum_claim_strength_rank: int
    claim_strength_taxonomy_version: str
    input_completeness_statuses: tuple[str, ...]
    binding_manifest: Mapping[str, Any]
    binding_manifest_ref: str
    binding_manifest_digest: str

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("bound_capability_input_factory_required")


@dataclass(frozen=True)
class _SlotMatch:
    result: QueryResultEnvelope
    report: CompletenessReport
    validation_dependencies: tuple[
        tuple[QueryResultEnvelope, CompletenessReport], ...
    ]


def capability_plan_has_executable_query_contracts(
    plan: CapabilityExecutionPlan,
    available_query_contract_refs: set[str],
) -> bool:
    required_slots = tuple(plan.required_input_slots)
    if not required_slots:
        return False
    for slot in required_slots:
        primary_refs = (
            tuple(slot.get("query_contract_refs") or ())
            if isinstance(slot, Mapping)
            else slot.query_contract_refs
        )
        validation_refs = (
            tuple(slot.get("validation_query_contract_refs") or ())
            if isinstance(slot, Mapping)
            else slot.validation_query_contract_refs
        )
        if len(primary_refs) != 1:
            return False
        if primary_refs[0] not in available_query_contract_refs:
            return False
        if any(ref not in available_query_contract_refs for ref in validation_refs):
            return False
    return True


def bind_capability_inputs(
    plan: CapabilityExecutionPlan,
    *,
    results: Mapping[str, QueryResultEnvelope],
    reports: Mapping[str, CompletenessReport],
    evidence_authority: RuntimeEvidenceAuthority | None = None,
    evidence_resolver: RuntimeEvidenceResolver | None = None,
    rows_loader: RowsPayloadLoader | None = None,
    evidence_writer: RuntimeEvidenceWriter | None = None,
    runtime_registry: RuntimeContractRegistry | None = None,
    release_resolver: DatasetReleaseResolver | None = None,
    run_mode: str = "production",
) -> BoundCapabilityInput:
    if evidence_authority is not None:
        evidence_resolver = evidence_resolver or evidence_authority
        rows_loader = rows_loader or evidence_authority.rows_loader
        evidence_writer = evidence_writer or evidence_authority._runtime_writer()
    if (
        evidence_resolver is None
        or rows_loader is None
        or evidence_writer is None
    ) and not legacy_fixture_enabled(run_mode):
        return _blocked_bound(plan, "runtime_evidence_authority_missing")
    if not legacy_fixture_enabled(run_mode):
        registry_error = runtime_registry_integrity_error(runtime_registry)
        if registry_error:
            return _blocked_bound(plan, registry_error)
    completeness_records: Mapping[str, Any] = {}
    authority_records: Mapping[str, Mapping[str, Any]] = {}
    if evidence_resolver is not None and rows_loader is not None and evidence_writer is not None:
        registry = runtime_registry
        if registry is None:
            return _blocked_bound(plan, "runtime_contract_registry_missing")
        try:
            validate_capability_plan_semantics(plan, registry)
        except (AuthoritativeQueryChainError, KeyError, TypeError, ValueError) as exc:
            return _blocked_bound(
                plan,
                f"capability_contract_resolution_failed:{exc}",
            )
        try:
            (
                results,
                reports,
                completeness_records,
                authority_records,
            ) = _resolve_authoritative_inputs(
                plan,
                results,
                reports,
                evidence_resolver,
                rows_loader,
                evidence_writer,
                release_resolver,
            )
        except Exception as exc:
            return _blocked_bound(
                plan,
                f"runtime_evidence_resolution_failed:{exc}",
            )
    rows_by_slot: dict[str, tuple[Mapping[str, Any], ...]] = {}
    plan_schema_reasons = _plan_schema_reasons(plan)
    reasons: list[str] = list(plan_schema_reasons)
    primary_results: list[QueryResultEnvelope] = []
    primary_reports: list[CompletenessReport] = []
    validation_results: list[QueryResultEnvelope] = []
    validation_reports: list[CompletenessReport] = []
    required_match_failed = bool(plan_schema_reasons)
    seen_slot_ids: set[str] = set()
    seen_primary_refs: set[str] = set()
    plan_completeness = tuple(
        str(item)
        for item in plan.minimum_readiness.get("accepted_completeness", ())
        if item
    )

    for expected_required, slots in (
        (True, plan.required_input_slots),
        (False, plan.optional_input_slots),
    ):
        for slot in slots:
            if slot.slot_id in seen_slot_ids:
                required_match_failed = required_match_failed or slot.required
                reasons.append(f"duplicate_slot_id:{slot.slot_id}")
                continue
            seen_slot_ids.add(slot.slot_id)
            if slot.required != expected_required:
                required_match_failed = True
                reasons.append(f"slot_requiredness_mismatch:{slot.slot_id}")
                continue
            if plan_completeness and tuple(slot.accepted_completeness) != plan_completeness:
                required_match_failed = required_match_failed or slot.required
                reasons.append(f"slot_readiness_contract_mismatch:{slot.slot_id}")
                continue
            duplicate_primary = next(
                (
                    ref
                    for ref in slot.query_contract_refs
                    if ref in seen_primary_refs
                ),
                "",
            )
            if duplicate_primary:
                required_match_failed = required_match_failed or slot.required
                reasons.append(f"primary_query_ref_reused:{slot.slot_id}:{duplicate_primary}")
                continue
            match, reason = _match_exact_slot_with_validation_dependencies(
                slot,
                results=results,
                reports=reports,
            )
            if match is None:
                if slot.required:
                    required_match_failed = True
                elif _optional_failure_blocks(plan, reason):
                    required_match_failed = True
                reasons.append(
                    reason
                    or (
                        f"missing_required_slot:{slot.slot_id}"
                        if slot.required
                        else f"missing_optional_slot:{slot.slot_id}"
                    )
                )
                continue
            seen_primary_refs.add(match.result.query_contract_ref)
            if match.report.completeness_status != "complete":
                reasons.append(
                    "accepted_incomplete_input:"
                    f"{slot.slot_id}:{match.report.completeness_status}"
                )
            rows_by_slot[slot.slot_id] = tuple(
                dict(row) for row in match.result.rows
            )
            primary_results.append(match.result)
            primary_reports.append(match.report)
            for validation_result, validation_report in match.validation_dependencies:
                validation_results.append(validation_result)
                validation_reports.append(validation_report)

    status = "blocked" if required_match_failed else "degraded" if reasons else "ready"
    collision_reasons = _reference_collision_reasons(
        primary_results,
        primary_reports,
        validation_results,
        validation_reports,
    )
    if collision_reasons:
        reasons.extend(collision_reasons)
        status = "blocked"
    values = {
        "capability_id": plan.capability_id,
        "capability_contract_ref": plan.capability_contract_ref,
        "capability_contract_version": plan.capability_contract_version,
        "capability_contract_signature": plan.capability_contract_signature,
        "analysis_contract_ref": plan.analysis_contract_ref,
        "status": status,
        "rows_by_slot": rows_by_slot,
        "reasons": tuple(reasons),
        "query_contract_refs": _dedupe(item.query_contract_ref for item in primary_results),
        "result_refs": _dedupe(item.result_ref for item in primary_results),
        "query_execution_record_refs": _authority_record_values(
            primary_results, authority_records, "query", "record_ref"
        ),
        "query_execution_record_digests": _authority_record_values(
            primary_results, authority_records, "query", "record_digest"
        ),
        "rows_refs": _dedupe(item.rows_ref for item in primary_results),
        "rows_metadata_record_refs": _authority_record_values(
            primary_results, authority_records, "rows", "record_ref"
        ),
        "rows_metadata_record_digests": _authority_record_values(
            primary_results, authority_records, "rows", "record_digest"
        ),
        "rows_content_hashes": _rows_hashes(
            evidence_resolver,
            primary_results,
        ),
        "completeness_report_refs": _dedupe(item.report_ref for item in primary_reports),
        "completeness_record_refs": _completeness_record_values(
            primary_reports,
            completeness_records,
            "record_ref",
        ),
        "completeness_record_digests": _completeness_record_values(
            primary_reports,
            completeness_records,
            "report_digest",
        ),
        "source_snapshot_refs": _dedupe(
            ref for item in primary_results for ref in item.source_snapshot_refs
        ),
        "validation_query_contract_refs": _dedupe(
            item.query_contract_ref for item in validation_results
        ),
        "validation_result_refs": _dedupe(item.result_ref for item in validation_results),
        "validation_query_execution_record_refs": _authority_record_values(
            validation_results, authority_records, "query", "record_ref"
        ),
        "validation_query_execution_record_digests": _authority_record_values(
            validation_results, authority_records, "query", "record_digest"
        ),
        "validation_rows_refs": _dedupe(
            item.rows_ref for item in validation_results
        ),
        "validation_rows_metadata_record_refs": _authority_record_values(
            validation_results, authority_records, "rows", "record_ref"
        ),
        "validation_rows_metadata_record_digests": _authority_record_values(
            validation_results, authority_records, "rows", "record_digest"
        ),
        "validation_rows_content_hashes": _rows_hashes(
            evidence_resolver,
            validation_results,
        ),
        "validation_completeness_report_refs": _dedupe(
            item.report_ref for item in validation_reports
        ),
        "validation_completeness_record_refs": _completeness_record_values(
            validation_reports,
            completeness_records,
            "record_ref",
        ),
        "validation_completeness_record_digests": _completeness_record_values(
            validation_reports,
            completeness_records,
            "report_digest",
        ),
        "validation_source_snapshot_refs": _dedupe(
            ref for item in validation_results for ref in item.source_snapshot_refs
        ),
        "supported_evidence_types": tuple(plan.supported_evidence_types),
        "supported_claim_types": tuple(plan.supported_claim_types),
        "maximum_claim_strength": plan.maximum_claim_strength,
        "maximum_claim_strength_rank": plan.maximum_claim_strength_rank,
        "claim_strength_taxonomy_version": plan.claim_strength_taxonomy_version,
        "input_completeness_statuses": tuple(
            report.completeness_status
            for report in (*primary_reports, *validation_reports)
        ),
    }
    try:
        return _create_bound_capability_input(
            plan,
            values,
            evidence_writer=evidence_writer,
            evidence_resolver=evidence_resolver,
            rows_loader=rows_loader,
            runtime_registry=runtime_registry,
            release_resolver=release_resolver,
        )
    except Exception as exc:
        return _blocked_bound(
            plan,
            f"runtime_evidence_writer_record_invalid:{exc}",
        )


def _resolve_authoritative_inputs(
    plan: CapabilityExecutionPlan,
    result_selectors: Mapping[str, QueryResultEnvelope],
    report_selectors: Mapping[str, CompletenessReport],
    resolver: RuntimeEvidenceResolver,
    rows_loader: RowsPayloadLoader,
    writer: RuntimeEvidenceWriter,
    release_resolver: DatasetReleaseResolver | None,
) -> tuple[
    Mapping[str, QueryResultEnvelope],
    Mapping[str, CompletenessReport],
    Mapping[str, Any],
    Mapping[str, Mapping[str, Any]],
]:
    query_refs = tuple(
        dict.fromkeys(
            ref
            for slot in (*plan.required_input_slots, *plan.optional_input_slots)
            for ref in (
                *slot.query_contract_refs,
                *slot.validation_query_contract_refs,
            )
        )
    )
    selected = []
    for query_ref in query_refs:
        result_selector = result_selectors.get(query_ref)
        report_selector = report_selectors.get(query_ref)
        if result_selector is None or report_selector is None:
            continue
        record = resolver.resolve_query_execution(result_selector.result_ref)
        if record is None:
            raise EvidenceIntegrityError(f"query_execution_ref_missing:{query_ref}")
        if runtime_evidence_record_integrity_errors(record):
            raise EvidenceIntegrityError(
                f"query_execution_record_integrity:{query_ref}"
            )
        if (
            record.query_contract_ref != query_ref
            or record.result_ref != result_selector.result_ref
            or record.rows_ref != result_selector.rows_ref
        ):
            raise EvidenceIntegrityError(f"query_execution_ref_missing:{query_ref}")
        rows_record = resolver.resolve_rows(record.rows_ref)
        if (
            rows_record is None
            or runtime_evidence_record_integrity_errors(rows_record)
            or rows_record.rows_content_hash != record.rows_content_hash
        ):
            raise EvidenceIntegrityError(f"rows_record_missing:{query_ref}")
        rows = rows_loader.load_rows(rows_record.storage_ref)
        if rows is None:
            raise EvidenceIntegrityError(f"rows_payload_missing:{query_ref}")
        if (
            len(rows) != rows_record.row_count
            or canonical_rows_hash(rows, rows_record.unique_key_fields)
            != rows_record.rows_content_hash
        ):
            raise EvidenceIntegrityError(f"rows_payload_invalid:{query_ref}")
        result = QueryResultEnvelope(
            **canonical_thaw(record.result_payload),
            rows=tuple(rows),
        )
        if not record.source_snapshot_refs:
            raise EvidenceIntegrityError(f"snapshot_refs_missing:{query_ref}")
        snapshot_records = tuple(
            resolver.resolve_snapshot(ref)
            for ref in record.source_snapshot_refs
        )
        if any(item is None for item in snapshot_records):
            raise EvidenceIntegrityError(f"snapshot_record_missing:{query_ref}")
        if any(runtime_evidence_record_integrity_errors(item) for item in snapshot_records):
            raise EvidenceIntegrityError(f"snapshot_record_integrity:{query_ref}")
        if (
            tuple(item.record_ref for item in snapshot_records)
            != record.source_snapshot_record_refs
            or tuple(item.record_digest for item in snapshot_records)
            != record.source_snapshot_record_digests
        ):
            raise EvidenceIntegrityError(f"snapshot_record_binding:{query_ref}")
        if release_resolver is None:
            release_required = next(
                (
                    item.snapshot.dataset_id
                    for item in snapshot_records
                    if item is not None
                    and canonical_dataset_requires_release(item.snapshot.dataset_id)
                ),
                "",
            )
            if release_required:
                raise EvidenceIntegrityError(
                    f"dataset_release_resolver_required:{release_required}"
                )
        if report_selector.report_ref != record.completeness_report_ref:
            raise EvidenceIntegrityError(f"completeness_ref_mismatch:{query_ref}")
        selected.append(
            (
                query_ref,
                record.contract,
                result,
                {item.snapshot_ref: item.snapshot for item in snapshot_records if item},
                record,
                rows_record,
            )
        )
    contracts = tuple(item[1] for item in selected)
    results = tuple(item[2] for item in selected)
    base_reports = tuple(
        validate_query_result(
            item[1],
            item[2],
            item[3],
            release_resolver=release_resolver,
        )
        for item in selected
    )
    final_reports = (
        validate_query_set(
            contracts,
            results,
            base_reports,
        )
        if selected
        else ()
    )
    refs = tuple(item[0] for item in selected)
    records = tuple(writer.record_completeness(report) for report in final_reports)
    if any(
        runtime_evidence_record_integrity_errors(record)
        or canonical_digest(record.report_payload)
        != canonical_digest(report.to_dict())
        for report, record in zip(final_reports, records)
    ):
        raise EvidenceIntegrityError("runtime_evidence_writer_record_invalid")
    return (
        dict(zip(refs, results)),
        dict(zip(refs, final_reports)),
        {report.report_ref: record for report, record in zip(final_reports, records)},
        {
            item[0]: {"query": item[4], "rows": item[5]}
            for item in selected
        },
    )


def _rows_hashes(
    resolver: RuntimeEvidenceResolver | None,
    results: list[QueryResultEnvelope],
) -> tuple[str, ...]:
    if resolver is None:
        return ()
    hashes = []
    seen_rows_refs = set()
    for result in results:
        if result.rows_ref in seen_rows_refs:
            continue
        seen_rows_refs.add(result.rows_ref)
        record = resolver.resolve_rows(result.rows_ref)
        if record is None or runtime_evidence_record_integrity_errors(record):
            raise EvidenceIntegrityError(
                f"rows_record_missing:{result.rows_ref}"
            )
        hashes.append(record.rows_content_hash)
    return tuple(hashes)


def _authority_record_values(
    results: list[QueryResultEnvelope],
    records: Mapping[str, Mapping[str, Any]],
    kind: str,
    field: str,
) -> tuple[str, ...]:
    values = []
    for result in results:
        record = records.get(result.query_contract_ref, {}).get(kind)
        if record is None:
            continue
        value = str(getattr(record, field))
        if value and value not in values:
            values.append(value)
    return tuple(values)


def _completeness_record_values(
    reports: list[CompletenessReport],
    records: Mapping[str, Any],
    field: str,
) -> tuple[str, ...]:
    values = []
    for report in reports:
        record = records.get(report.report_ref)
        if record is None:
            continue
        values.append(str(getattr(record, field)))
    return _dedupe(values)


def _match_exact_slot_with_validation_dependencies(
    slot: CapabilityInputSlot,
    *,
    results: Mapping[str, QueryResultEnvelope],
    reports: Mapping[str, CompletenessReport],
) -> tuple[_SlotMatch | None, str]:
    if len(slot.query_contract_refs) != 1:
        return None, f"primary_query_ref_cardinality_invalid:{slot.slot_id}"
    query_ref = slot.query_contract_refs[0]
    result = results.get(query_ref)
    report = reports.get(query_ref)
    if result is None or report is None:
        return None, ""
    if result.query_contract_ref != query_ref or report.query_contract_ref != query_ref:
        return None, f"primary_provenance_mismatch:{slot.slot_id}"
    if (
        result.execution_status != "succeeded"
        or report.result_ref != result.result_ref
        or report.report_ref != result.completeness_report_ref
        or not result.result_ref
        or not result.completeness_report_ref
        or not result.source_snapshot_refs
    ):
        return None, f"primary_provenance_mismatch:{slot.slot_id}"
    if report.completeness_status not in slot.accepted_completeness:
        return None, f"completeness_not_accepted:{slot.slot_id}"
    if not _primary_report_accepted(report):
        return None, f"primary_report_not_ready:{slot.slot_id}"
    if not _report_snapshot_matches(report, result):
        return None, f"primary_snapshot_provenance_mismatch:{slot.slot_id}"
    if result.row_count <= 0 or not result.rows:
        return None, f"empty_primary_result:{slot.slot_id}"
    if (
        result.row_count != len(result.rows)
        or report.coverage_summary.get("row_count") != result.row_count
    ):
        return None, f"primary_row_count_mismatch:{slot.slot_id}"
    missing_fields = tuple(
        field
        for field in slot.required_fields
        if any(field not in row for row in result.rows)
    )
    if missing_fields:
        return (
            None,
            f"required_fields_missing:{slot.slot_id}:{','.join(missing_fields)}",
        )
    missing_windows = tuple(
        window_id
        for window_id in slot.required_window_ids
        if window_id not in result.observed_windows
    )
    if missing_windows:
        return (
            None,
            f"required_windows_missing:{slot.slot_id}:{','.join(missing_windows)}",
        )
    coverage_required = tuple(
        report.coverage_summary.get("required_windows") or ()
    )
    coverage_observed = tuple(
        report.coverage_summary.get("observed_windows") or ()
    )
    if any(
        window_id not in coverage_required or window_id not in coverage_observed
        for window_id in slot.required_window_ids
    ):
        return None, f"required_window_provenance_mismatch:{slot.slot_id}"
    row_window_ids = {
        str(row["window_id"])
        for row in result.rows
        if row.get("window_id") not in (None, "")
    }
    if row_window_ids and any(
        window_id not in row_window_ids for window_id in slot.required_window_ids
    ):
        return None, f"required_window_rows_mismatch:{slot.slot_id}"

    validation_dependencies = []
    for validation_ref in slot.validation_query_contract_refs:
        validation_result = results.get(validation_ref)
        if validation_result is None:
            return None, f"missing_validation_query:{slot.slot_id}"
        validation_report = reports.get(validation_ref)
        if validation_report is None:
            return None, f"missing_validation_report:{slot.slot_id}"
        if not _validation_dependency_ready(
            validation_ref,
            validation_result,
            validation_report,
        ):
            return None, f"validation_report_not_ready:{slot.slot_id}"
        validation_dependencies.append((validation_result, validation_report))

    if validation_dependencies and not _primary_reconciliation_provenance_matches(
        query_ref,
        result,
        report,
        tuple(validation_dependencies),
    ):
        return None, f"validation_provenance_mismatch:{slot.slot_id}"

    return (
        _SlotMatch(
            result=result,
            report=report,
            validation_dependencies=tuple(validation_dependencies),
        ),
        "",
    )


def _validation_dependency_ready(
    expected_query_ref: str,
    result: QueryResultEnvelope,
    report: CompletenessReport,
) -> bool:
    return bool(
        result.query_contract_ref == expected_query_ref
        and report.query_contract_ref == expected_query_ref
        and result.execution_status == "succeeded"
        and report.result_ref == result.result_ref
        and report.report_ref == result.completeness_report_ref
        and report.completeness_status == "complete"
        and report.analysis_readiness == "ready"
        and result.result_ref
        and result.completeness_report_ref
        and result.source_snapshot_refs
        and _report_assertions_ready(report)
        and result.row_count > 0
        and result.row_count == len(result.rows)
        and report.coverage_summary.get("row_count") == result.row_count
        and _report_snapshot_matches(report, result)
    )


def _primary_reconciliation_provenance_matches(
    query_ref: str,
    result: QueryResultEnvelope,
    report: CompletenessReport,
    dependencies: tuple[tuple[QueryResultEnvelope, CompletenessReport], ...],
) -> bool:
    assertions = tuple(
        assertion
        for assertion in report.assertion_results
        if assertion.get("assertion") == "dimension_total_reconciliation"
        and assertion.get("passed") is True
    )
    if len(assertions) != 1:
        return False
    coverage = report.coverage_summary.get("reconciliation_validation")
    if not isinstance(coverage, Mapping):
        return False
    details = assertions[0].get("details")
    if not isinstance(details, Mapping) or details.get("status") != "passed":
        return False
    primary_expected = {
        "primary_query_contract_ref": query_ref,
        "primary_result_ref": result.result_ref,
        "primary_report_ref": report.report_ref,
        "primary_snapshot_refs": tuple(result.source_snapshot_refs),
    }
    if not _provenance_fields_match(coverage, primary_expected):
        return False
    if not _provenance_fields_match(details, primary_expected):
        return False
    expected_dependencies = tuple(
        {
            "validation_query_contract_ref": validation_result.query_contract_ref,
            "validation_result_ref": validation_result.result_ref,
            "validation_report_ref": validation_report.report_ref,
            "validation_snapshot_refs": tuple(validation_result.source_snapshot_refs),
        }
        for validation_result, validation_report in dependencies
    )
    if len(expected_dependencies) == 1 and "validation_dependencies" not in coverage:
        return _provenance_fields_match(
            coverage,
            expected_dependencies[0],
        ) and _provenance_fields_match(details, expected_dependencies[0])
    return _validation_dependencies_match(
        coverage.get("validation_dependencies"),
        expected_dependencies,
    ) and _validation_dependencies_match(
        details.get("validation_dependencies"),
        expected_dependencies,
    )


def _validation_dependencies_match(
    actual: Any,
    expected: tuple[Mapping[str, Any], ...],
) -> bool:
    if not isinstance(actual, (list, tuple)) or len(actual) != len(expected):
        return False
    return all(
        isinstance(item, Mapping) and _provenance_fields_match(item, expected_item)
        for item, expected_item in zip(actual, expected)
    )


def _report_assertions_ready(report: CompletenessReport) -> bool:
    return bool(
        report.assertion_results
        and not report.failure_reasons
        and all(assertion.get("passed") is True for assertion in report.assertion_results)
    )


def _primary_report_accepted(report: CompletenessReport) -> bool:
    if report.completeness_status == "complete":
        return report.analysis_readiness == "ready" and _report_assertions_ready(
            report
        )
    execution_assertions = tuple(
        assertion
        for assertion in report.assertion_results
        if assertion.get("assertion") == "execution_succeeded"
    )
    return bool(
        report.analysis_readiness == "degraded"
        and len(execution_assertions) == 1
        and execution_assertions[0].get("passed") is True
    )


def _report_snapshot_matches(
    report: CompletenessReport,
    result: QueryResultEnvelope,
) -> bool:
    if "snapshot_refs" in report.coverage_summary:
        snapshot_refs = tuple(report.coverage_summary.get("snapshot_refs") or ())
        return snapshot_refs == tuple(result.source_snapshot_refs)
    snapshot_ref = str(report.coverage_summary.get("snapshot_ref") or "")
    return bool(
        len(result.source_snapshot_refs) == 1
        and snapshot_ref == result.source_snapshot_refs[0]
    )


def _provenance_fields_match(
    actual: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> bool:
    return all(
        tuple(actual.get(key) or ()) == value
        if key.endswith("_refs")
        else actual.get(key) == value
        for key, value in expected.items()
    )


def _dedupe(values: Any) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in values if value))


def _optional_failure_blocks(
    plan: CapabilityExecutionPlan,
    reason: str,
) -> bool:
    policy_key = (
        "missing_optional_input"
        if not reason or reason.startswith("missing_")
        else "incomplete_input"
    )
    action = str(plan.degradation_policy.get(policy_key) or "")
    return action not in _NON_BLOCKING_DEGRADATION_ACTIONS


_NON_BLOCKING_DEGRADATION_ACTIONS = frozenset(
    {
        "context_only",
        "degrade_claim",
        "omit_optional_component",
        "omit_path",
        "report_contract_gap",
        "report_limitation",
        "sensitivity_only",
    }
)
_BLOCKING_DEGRADATION_ACTIONS = frozenset(
    {
        "block_candidate_impact",
        "block_claim",
        "block_reduced_claim",
        "block_unverified_claim",
    }
)
_KNOWN_DEGRADATION_ACTIONS = (
    _NON_BLOCKING_DEGRADATION_ACTIONS | _BLOCKING_DEGRADATION_ACTIONS
)


def _plan_schema_reasons(plan: CapabilityExecutionPlan) -> tuple[str, ...]:
    reasons = []
    required_mode = str(plan.minimum_readiness.get("required_slots") or "")
    if required_mode == "all":
        if not plan.required_input_slots:
            reasons.append("required_slot_mode_mismatch:all")
    elif required_mode == "none":
        if plan.required_input_slots:
            reasons.append("required_slot_mode_mismatch:none")
    else:
        reasons.append(f"required_slot_mode_invalid:{required_mode or 'missing'}")
    accepted = tuple(
        str(item)
        for item in plan.minimum_readiness.get("accepted_completeness", ())
        if item
    )
    slots = (*plan.required_input_slots, *plan.optional_input_slots)
    if slots and not accepted:
        reasons.append("accepted_completeness_missing")
    if not slots and accepted:
        reasons.append("accepted_completeness_without_slots")
    for key, action in plan.degradation_policy.items():
        if str(action) not in _KNOWN_DEGRADATION_ACTIONS:
            reasons.append(f"degradation_action_unknown:{key}:{action}")
    return tuple(reasons)


def _reference_collision_reasons(
    primary_results: list[QueryResultEnvelope],
    primary_reports: list[CompletenessReport],
    validation_results: list[QueryResultEnvelope],
    validation_reports: list[CompletenessReport],
) -> tuple[str, ...]:
    categories = {
        "primary_query_ref": tuple(
            (item.query_contract_ref, item) for item in primary_results
        ),
        "primary_result_ref": tuple((item.result_ref, item) for item in primary_results),
        "primary_report_ref": tuple((item.report_ref, item) for item in primary_reports),
        "validation_query_ref": tuple(
            (item.query_contract_ref, item) for item in validation_results
        ),
        "validation_result_ref": tuple(
            (item.result_ref, item) for item in validation_results
        ),
        "validation_report_ref": tuple(
            (item.report_ref, item) for item in validation_reports
        ),
    }
    reasons = []
    for category, entries in categories.items():
        refs = tuple(ref for ref, _ in entries)
        for ref in dict.fromkeys(refs):
            matches = tuple(item for item_ref, item in entries if item_ref == ref)
            if len(matches) <= 1:
                continue
            if category.startswith("primary_"):
                reasons.append(f"duplicate_{category}:{ref}")
            elif any(item != matches[0] for item in matches[1:]):
                reasons.append(f"conflicting_{category}:{ref}")
    for kind in ("query_ref", "result_ref", "report_ref"):
        primary = {ref for ref, _ in categories[f"primary_{kind}"]}
        validation = {ref for ref, _ in categories[f"validation_{kind}"]}
        reasons.extend(
            f"primary_validation_{kind}_collision:{ref}"
            for ref in sorted(primary.intersection(validation))
        )
    return tuple(reasons)


def _create_bound_capability_input(
    plan: CapabilityExecutionPlan,
    values: Mapping[str, Any],
    *,
    evidence_writer: RuntimeEvidenceWriter | None = None,
    evidence_resolver: RuntimeEvidenceResolver | None = None,
    rows_loader: RowsPayloadLoader | None = None,
    runtime_registry: RuntimeContractRegistry | None = None,
    release_resolver: DatasetReleaseResolver | None = None,
) -> BoundCapabilityInput:
    frozen_values = {
        key: _deep_freeze(value)
        for key, value in values.items()
    }
    manifest_payload = {
        "plan": _capability_plan_manifest(plan),
        "binding": _canonical_value(frozen_values),
    }
    digest = _canonical_digest(manifest_payload)
    binding_ref = ""
    if evidence_writer is not None:
        authority_record = _record_capability_binding(
            evidence_writer,
            plan,
            _canonical_value(frozen_values),
        )
        if (
            runtime_evidence_record_integrity_errors(authority_record)
            or _canonical_value(authority_record.plan_payload)
            != _canonical_value(manifest_payload["plan"])
            or _canonical_value(authority_record.binding_payload)
            != _canonical_value(manifest_payload["binding"])
            or authority_record.binding_digest != digest
        ):
            raise EvidenceIntegrityError("runtime_evidence_writer_record_invalid")
        digest = authority_record.binding_digest
        binding_ref = authority_record.record_ref
        if evidence_resolver is None or rows_loader is None or runtime_registry is None:
            raise EvidenceIntegrityError("authoritative_query_chain_dependencies_missing")
        validate_authoritative_query_chain(
            authority_record,
            resolver=evidence_resolver,
            rows_loader=rows_loader,
            runtime_registry=runtime_registry,
            release_resolver=release_resolver,
        )
    instance = object.__new__(BoundCapabilityInput)
    for field_name, value in frozen_values.items():
        object.__setattr__(instance, field_name, value)
    object.__setattr__(instance, "binding_manifest", _deep_freeze(manifest_payload))
    object.__setattr__(instance, "binding_manifest_ref", binding_ref)
    object.__setattr__(instance, "binding_manifest_digest", digest)
    object.__setattr__(instance, "_construction_token", _BOUND_CONSTRUCTION_TOKEN)
    return instance


def validate_bound_capability_input(
    bound: Any,
    resolver: RuntimeEvidenceResolver | None = None,
    *,
    allow_fixture: bool = False,
) -> str:
    if type(bound) is not BoundCapabilityInput:
        return "bound_capability_input_type_invalid"
    if getattr(bound, "_construction_token", None) is not _BOUND_CONSTRUCTION_TOKEN:
        return "bound_capability_input_factory_required"
    try:
        manifest = _canonical_value(bound.binding_manifest)
        stored_digest = str(bound.binding_manifest_digest)
    except (AttributeError, TypeError, ValueError):
        return "binding_manifest_missing"
    if _canonical_digest(manifest) != stored_digest:
        return "binding_manifest_digest_mismatch"
    binding = manifest.get("binding") if isinstance(manifest, Mapping) else None
    plan_manifest = manifest.get("plan") if isinstance(manifest, Mapping) else None
    if not isinstance(binding, Mapping) or not isinstance(plan_manifest, Mapping):
        return "binding_manifest_schema_invalid"
    current = _canonical_value(
        {
            field_name: getattr(bound, field_name)
            for field_name in _BOUND_VALUE_FIELDS
        }
    )
    if current != binding:
        return "binding_manifest_payload_mismatch"
    if not bound.binding_manifest_ref and not allow_fixture:
        return "capability_binding_record_missing"
    if bound.binding_manifest_ref:
        if resolver is None:
            return "runtime_evidence_resolver_missing"
        try:
            authority_record = resolver.resolve_capability_binding(
                bound.binding_manifest_ref
            )
        except Exception:
            return "runtime_evidence_resolution_failed"
        if authority_record is None:
            return "capability_binding_record_missing"
        if runtime_evidence_record_integrity_errors(authority_record):
            return "capability_binding_record_integrity"
        if (
            authority_record.binding_digest != stored_digest
            or _canonical_value(authority_record.binding_payload) != current
        ):
            return "capability_binding_record_mismatch"
    if not bound.capability_id or not bound.capability_contract_ref:
        return "binding_manifest_schema_invalid"
    return ""


def _blocked_bound(
    plan: CapabilityExecutionPlan,
    reason: str,
) -> BoundCapabilityInput:
    return _create_bound_capability_input(
        plan,
        {
            "capability_id": plan.capability_id,
            "capability_contract_ref": plan.capability_contract_ref,
            "capability_contract_version": plan.capability_contract_version,
            "capability_contract_signature": plan.capability_contract_signature,
            "analysis_contract_ref": plan.analysis_contract_ref,
            "status": "blocked",
            "rows_by_slot": {},
            "reasons": (reason,),
            "query_contract_refs": (),
            "result_refs": (),
            "query_execution_record_refs": (),
            "query_execution_record_digests": (),
            "rows_refs": (),
            "rows_metadata_record_refs": (),
            "rows_metadata_record_digests": (),
            "rows_content_hashes": (),
            "completeness_report_refs": (),
            "completeness_record_refs": (),
            "completeness_record_digests": (),
            "source_snapshot_refs": (),
            "validation_query_contract_refs": (),
            "validation_result_refs": (),
            "validation_query_execution_record_refs": (),
            "validation_query_execution_record_digests": (),
            "validation_rows_refs": (),
            "validation_rows_metadata_record_refs": (),
            "validation_rows_metadata_record_digests": (),
            "validation_rows_content_hashes": (),
            "validation_completeness_report_refs": (),
            "validation_completeness_record_refs": (),
            "validation_completeness_record_digests": (),
            "validation_source_snapshot_refs": (),
            "supported_evidence_types": tuple(plan.supported_evidence_types),
            "supported_claim_types": tuple(plan.supported_claim_types),
            "maximum_claim_strength": plan.maximum_claim_strength,
            "maximum_claim_strength_rank": plan.maximum_claim_strength_rank,
            "claim_strength_taxonomy_version": plan.claim_strength_taxonomy_version,
            "input_completeness_statuses": (),
        },
    )


def bound_capability_manifest(bound: Any) -> dict[str, Any]:
    if validate_bound_capability_input(bound, allow_fixture=True):
        return {}
    return _canonical_value(bound.binding_manifest)


_BOUND_VALUE_FIELDS = (
    "capability_id",
    "capability_contract_ref",
    "capability_contract_version",
    "capability_contract_signature",
    "analysis_contract_ref",
    "status",
    "rows_by_slot",
    "reasons",
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
    "source_snapshot_refs",
    "validation_query_contract_refs",
    "validation_result_refs",
    "validation_query_execution_record_refs",
    "validation_query_execution_record_digests",
    "validation_rows_refs",
    "validation_rows_metadata_record_refs",
    "validation_rows_metadata_record_digests",
    "validation_rows_content_hashes",
    "validation_completeness_report_refs",
    "validation_completeness_record_refs",
    "validation_completeness_record_digests",
    "validation_source_snapshot_refs",
    "supported_evidence_types",
    "supported_claim_types",
    "maximum_claim_strength",
    "maximum_claim_strength_rank",
    "claim_strength_taxonomy_version",
    "input_completeness_statuses",
)


def _capability_plan_manifest(plan: CapabilityExecutionPlan) -> dict[str, Any]:
    def slot_payload(slot: CapabilityInputSlot) -> dict[str, Any]:
        return {
            "slot_id": slot.slot_id,
            "query_contract_refs": tuple(slot.query_contract_refs),
            "required": slot.required,
            "accepted_completeness": tuple(slot.accepted_completeness),
            "required_fields": tuple(slot.required_fields),
            "required_window_ids": tuple(slot.required_window_ids),
            "validation_query_contract_refs": tuple(
                slot.validation_query_contract_refs
            ),
        }

    return {
        "capability_id": plan.capability_id,
        "capability_contract_ref": plan.capability_contract_ref,
        "capability_contract_version": plan.capability_contract_version,
        "capability_contract_signature": plan.capability_contract_signature,
        "analysis_contract_ref": plan.analysis_contract_ref,
        "required_input_slots": tuple(
            slot_payload(slot) for slot in plan.required_input_slots
        ),
        "optional_input_slots": tuple(
            slot_payload(slot) for slot in plan.optional_input_slots
        ),
        "merge_strategy": plan.merge_strategy,
        "minimum_readiness": _canonical_value(plan.minimum_readiness),
        "degradation_policy": _canonical_value(plan.degradation_policy),
        "supported_evidence_types": tuple(plan.supported_evidence_types),
        "supported_claim_types": tuple(plan.supported_claim_types),
        "maximum_claim_strength": plan.maximum_claim_strength,
        "maximum_claim_strength_rank": plan.maximum_claim_strength_rank,
        "claim_strength_taxonomy_version": plan.claim_strength_taxonomy_version,
    }


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                str(key): _deep_freeze(item)
                for key, item in value.items()
            }
        )
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_deep_freeze(item) for item in value)
    return value


def _canonical_value(value: Any) -> Any:
    return canonical_value(value)


def _canonical_digest(value: Any) -> str:
    return canonical_digest(_canonical_value(value))
