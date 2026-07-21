from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
import math
from typing import Any, Iterable, Mapping, Sequence

from bi_agent.runtime.analysis_contracts import (
    DIMENSION_PRESENCE_POLICIES,
    CompletenessFailureClass,
    CompletenessReport,
    MetricBinding,
    QueryContract,
    QueryResultEnvelope,
    canonical_exact_additive_count,
    completeness_state_from_assertions,
)
from bi_agent.runtime.dataset_catalog import (
    DatasetReleaseResolver,
    DatasetSnapshot,
    canonical_dataset_requires_release,
    dataset_release_authority_integrity_errors,
    snapshot_matches_release_authority,
)
from bi_agent.runtime.evidence_authority import (
    EvidenceIntegrityError,
    RuntimeEvidenceAuthority,
    RuntimeEvidenceWriter,
    _record_completeness,
    canonical_digest,
    canonical_result_rows_hash,
    runtime_evidence_record_integrity_errors,
)
from bi_agent.runtime.query_audit import query_audit_refs


ASSERTIONS = (
    "execution_succeeded",
    "snapshot_watermark",
    "required_fields",
    "required_windows",
    "complete_window_days",
    "unique_key",
    "valid_denominators",
    "provider_not_truncated",
    "aggregate_only",
)

CURRENT_DATA_ASSERTIONS = (
    *ASSERTIONS,
    "overall_channel_reconciliation",
)

_JOIN_AUDIT_FIELDS = (
    "__join_input_rows",
    "__join_output_rows",
    "__join_duplicate_keys",
    "__join_unmatched_rows",
)

_CONTEXT_ONLY_QUERY_INTENTS = frozenset(
    {
        "data_quality_probe",
        "event_context_probe",
        "association_outcome_timeseries",
        "association_candidate_timeseries",
        "channel_context_probe",
        "channel_context_total_probe",
        "source_reconciliation_probe",
    }
)

_UNRECONCILED_DIMENSION_QUERY_INTENTS = frozenset(
    {
        "data_quality_probe",
        "association_outcome_timeseries",
        "association_candidate_timeseries",
        "channel_context_probe",
        "source_reconciliation_probe",
    }
)


def validate_query_result(
    contract: QueryContract,
    result: QueryResultEnvelope,
    snapshot: DatasetSnapshot
    | Mapping[str, DatasetSnapshot]
    | Sequence[DatasetSnapshot],
    *,
    evidence_authority: RuntimeEvidenceAuthority | None = None,
    evidence_writer: RuntimeEvidenceWriter | None = None,
    release_resolver: DatasetReleaseResolver | None = None,
) -> CompletenessReport:
    snapshots = _normalize_snapshots(snapshot)
    rows = tuple(result.rows)
    execution_succeeded = result.execution_status == "succeeded"
    core_assertions = (
        _execution_assertion(contract, result, rows),
        _watermark_assertion(
            contract,
            snapshots,
            release_resolver=release_resolver,
        ),
        _required_fields_assertion(contract, result, rows),
        (
            _required_windows_assertion(contract, rows)
            if execution_succeeded
            else _not_evaluated_assertion("required_windows")
        ),
        (
            _complete_days_assertion(contract, rows)
            if execution_succeeded
            else _not_evaluated_assertion("complete_window_days")
        ),
        _unique_key_assertion(contract, rows),
        _denominator_assertion(contract, rows),
        _provider_bound_assertion(result),
        _aggregate_only_assertion(contract, rows),
    )
    pending_reconciliation = _pending_reconciliation_assertion(contract, result)
    assertions = core_assertions + (
        (pending_reconciliation,) if pending_reconciliation is not None else ()
    )
    failure_reasons = _failure_reasons(assertions)
    completeness_status, analysis_readiness = completeness_state_from_assertions(
        assertions
    )
    window_day_counts, _ = _window_membership(contract, rows)
    coverage_summary = {
        "row_count": result.row_count,
        "required_windows": tuple(contract.window_refs),
        "observed_windows": tuple(window_day_counts),
        "window_day_counts": dict(window_day_counts),
        "expected_grain": tuple(contract.result_shape.grain),
        "observed_grain": tuple(result.observed_grain),
        "snapshot_ref": snapshots[0].snapshot_ref if len(snapshots) == 1 else "",
        "snapshot_refs": tuple(result.source_snapshot_refs),
        "snapshot_watermark": snapshots[0].watermark if len(snapshots) == 1 else "",
        "snapshot_watermarks": {
            item.snapshot_ref: item.watermark for item in snapshots
        },
        "rows_ref": result.rows_ref,
    }
    if pending_reconciliation is not None:
        coverage_summary["reconciliation_validation"] = dict(
            pending_reconciliation["details"]
        )
    report = CompletenessReport(
        report_ref=result.completeness_report_ref,
        result_ref=result.result_ref,
        query_contract_ref=contract.query_contract_id,
        completeness_status=completeness_status,
        analysis_readiness=analysis_readiness,
        assertion_results=assertions,
        failure_reasons=failure_reasons,
        coverage_summary=coverage_summary,
    )
    writer = evidence_writer or (
        evidence_authority._runtime_writer() if evidence_authority is not None else None
    )
    if writer is not None:
        report = _recorded_report(report, writer)
    return report


def validate_query_set(
    contracts: Sequence[QueryContract],
    results: Sequence[QueryResultEnvelope],
    reports: Sequence[CompletenessReport],
    *,
    evidence_authority: RuntimeEvidenceAuthority | None = None,
    evidence_writer: RuntimeEvidenceWriter | None = None,
) -> tuple[CompletenessReport, ...]:
    if not (len(contracts) == len(results) == len(reports)):
        raise ValueError("query_set_length_mismatch")

    query_set_contract_refs = tuple(
        sorted(contract.query_contract_id for contract in contracts)
    )

    validated = []
    for index, (contract, result, report) in enumerate(
        zip(contracts, results, reports)
    ):
        if report.query_contract_ref != contract.query_contract_id:
            raise ValueError(f"query_set_report_contract_mismatch:{index}")
        if report.result_ref != result.result_ref:
            raise ValueError(f"query_set_report_result_mismatch:{index}")
        if report.report_ref != result.completeness_report_ref:
            raise ValueError(f"query_set_report_ref_mismatch:{index}")

        dimension_assertion = _dimension_total_assertion(
            contract,
            result,
            report,
            contracts,
            results,
            reports,
        )
        set_assertions = (
            dimension_assertion,
            _join_cardinality_assertion(contract, result),
            _paired_windows_assertion(contract, result),
        )
        base_assertions = tuple(
            assertion
            for assertion in report.assertion_results
            if assertion["assertion"]
            not in {
                "dimension_total_reconciliation",
                "overall_channel_reconciliation",
            }
        )
        assertion_results = (*base_assertions, *set_assertions)
        failure_reasons = _failure_reasons(assertion_results)
        status, readiness = completeness_state_from_assertions(assertion_results)
        coverage_summary = {
            **dict(report.coverage_summary),
            "query_set_size": len(contracts),
            "query_set_contract_refs": query_set_contract_refs,
        }
        if contract.reconciliation_binding is not None:
            coverage_summary["reconciliation_validation"] = dict(
                dimension_assertion["details"]
            )
        validated.append(
            replace(
                report,
                completeness_status=status,
                analysis_readiness=readiness,
                assertion_results=assertion_results,
                failure_reasons=failure_reasons,
                coverage_summary=coverage_summary,
            )
        )
    output = tuple(validated)
    writer = evidence_writer or (
        evidence_authority._runtime_writer() if evidence_authority is not None else None
    )
    if writer is not None:
        output = tuple(_recorded_report(report, writer) for report in output)
    return output


def _recorded_report(
    report: CompletenessReport,
    writer: RuntimeEvidenceWriter,
) -> CompletenessReport:
    try:
        record = _record_completeness(writer, report)
    except EvidenceIntegrityError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvidenceIntegrityError("runtime_evidence_writer_record_invalid") from exc
    if (
        runtime_evidence_record_integrity_errors(record)
        or record.report_ref != report.report_ref
        or record.query_contract_ref != report.query_contract_ref
        or record.result_ref != report.result_ref
        or canonical_digest(record.report_payload) != canonical_digest(report.to_dict())
    ):
        raise EvidenceIntegrityError("runtime_evidence_writer_record_invalid")
    return report


def _execution_assertion(
    contract: QueryContract,
    result: QueryResultEnvelope,
    rows: tuple[Mapping[str, Any], ...],
) -> Mapping[str, Any]:
    reasons = []
    failure_classes = []
    if result.execution_status == "failed":
        reasons.append(f"execution_status:{result.execution_status}")
        reasons.append(result.failure_reason)
        failure_classes.append(CompletenessFailureClass.EXECUTION_TECHNICAL)
    elif result.execution_status == "blocked":
        reasons.append(f"execution_status:{result.execution_status}")
        reasons.append(result.failure_reason)
        failure_classes.append(CompletenessFailureClass.AUTHORITY_INTEGRITY)
    if result.query_contract_ref != contract.query_contract_id:
        reasons.append(
            "query_contract_ref_mismatch:"
            f"{result.query_contract_ref}:{contract.query_contract_id}"
        )
        failure_classes.append(CompletenessFailureClass.AUTHORITY_INTEGRITY)
    if tuple(result.source_snapshot_refs) != tuple(contract.dataset_snapshot_refs):
        reasons.append(
            "source_snapshot_refs_mismatch:"
            f"{','.join(result.source_snapshot_refs)}:"
            f"{','.join(contract.dataset_snapshot_refs)}"
        )
        failure_classes.append(CompletenessFailureClass.AUTHORITY_INTEGRITY)
    if result.row_count != len(rows):
        reasons.append(f"row_count_mismatch:{result.row_count}:{len(rows)}")
        failure_classes.append(CompletenessFailureClass.AUTHORITY_INTEGRITY)
    contract_reasons = (
        *_metric_reconciliation_contract_reasons(contract),
        *_join_expectation_contract_reasons(contract),
    )
    reasons.extend(contract_reasons)
    if contract_reasons:
        failure_classes.append(CompletenessFailureClass.AUTHORITY_INTEGRITY)
    try:
        rows_content_hash = canonical_result_rows_hash(
            rows,
            contract.result_shape.unique_key,
        )
    except EvidenceIntegrityError:
        rows_content_hash = ""
    expected_refs = query_audit_refs(
        result.query_hash,
        contract.contract_signature,
        contract.dataset_snapshot_refs,
        query_contract_ref=contract.query_contract_id,
        execution_attempt_ref=result.execution_attempt_ref,
        rows_content_hash=(
            rows_content_hash if result.execution_status == "succeeded" else ""
        ),
    )
    if result.result_ref != expected_refs.result_ref:
        reasons.append("result_ref_mismatch")
        failure_classes.append(CompletenessFailureClass.AUTHORITY_INTEGRITY)
    if result.rows_ref != expected_refs.rows_ref:
        reasons.append("rows_ref_mismatch")
        failure_classes.append(CompletenessFailureClass.AUTHORITY_INTEGRITY)
    if result.completeness_report_ref != expected_refs.completeness_report_ref:
        reasons.append("completeness_report_ref_mismatch")
        failure_classes.append(CompletenessFailureClass.AUTHORITY_INTEGRITY)
    if not result.execution_attempt_ref:
        reasons.append("missing_execution_attempt_ref")
        failure_classes.append(CompletenessFailureClass.AUTHORITY_INTEGRITY)
    if result.execution_status == "succeeded":
        if not result.query_id:
            reasons.append("missing_query_id")
            failure_classes.append(CompletenessFailureClass.AUTHORITY_INTEGRITY)
        if not result.query_hash:
            reasons.append("missing_query_hash")
            failure_classes.append(CompletenessFailureClass.AUTHORITY_INTEGRITY)
        if not result.result_ref:
            reasons.append("missing_result_ref")
            failure_classes.append(CompletenessFailureClass.AUTHORITY_INTEGRITY)
        if not result.rows_ref:
            reasons.append("missing_rows_ref")
            failure_classes.append(CompletenessFailureClass.AUTHORITY_INTEGRITY)
        if not result.completeness_report_ref:
            reasons.append("missing_completeness_report_ref")
            failure_classes.append(CompletenessFailureClass.AUTHORITY_INTEGRITY)
    return _assertion(
        "execution_succeeded",
        reasons,
        failure_classes=failure_classes,
    )


def _watermark_assertion(
    contract: QueryContract,
    snapshots: Sequence[DatasetSnapshot],
    *,
    release_resolver: DatasetReleaseResolver | None,
) -> Mapping[str, Any]:
    reasons = []
    failure_classes = []
    actual_refs = tuple(item.snapshot_ref for item in snapshots)
    if set(actual_refs) != set(contract.dataset_snapshot_refs) or len(
        actual_refs
    ) != len(contract.dataset_snapshot_refs):
        reasons.append(
            "snapshot_refs_mismatch:"
            f"{','.join(actual_refs)}:{','.join(contract.dataset_snapshot_refs)}"
        )
        failure_classes.append(CompletenessFailureClass.AUTHORITY_INTEGRITY)
    required_values = []
    for window in contract.resolved_windows:
        try:
            required_values.append(
                date.fromisoformat(window.source_watermark_requirement)
            )
        except (TypeError, ValueError):
            reasons.append(
                "source_watermark_requirement_invalid:"
                f"{window.window_id}:{window.source_watermark_requirement}"
            )
            failure_classes.append(CompletenessFailureClass.AUTHORITY_INTEGRITY)
    required = max(required_values) if required_values else None
    observed_by_ref = {}
    for snapshot in snapshots:
        if snapshot.status != "active":
            reasons.append(
                f"snapshot_status_invalid:{snapshot.snapshot_ref}:{snapshot.status}"
            )
            failure_classes.append(CompletenessFailureClass.AUTHORITY_INTEGRITY)
        if (
            snapshot.evidence_state != "claim_ready"
            and contract.query_intent not in _CONTEXT_ONLY_QUERY_INTENTS
        ):
            reasons.append(
                "snapshot_evidence_state_invalid:"
                f"{snapshot.snapshot_ref}:{snapshot.evidence_state}"
            )
            failure_classes.append(CompletenessFailureClass.AUTHORITY_INTEGRITY)
        if (
            contract.dimension_bindings
            and contract.query_intent not in _UNRECONCILED_DIMENSION_QUERY_INTENTS
            and snapshot.reconciliation_ref
            and snapshot.reconciliation_status != "matched"
        ):
            reasons.append(
                "snapshot_reconciliation_status_invalid:"
                f"{snapshot.snapshot_ref}:{snapshot.reconciliation_status}"
            )
            failure_classes.append(CompletenessFailureClass.AUTHORITY_INTEGRITY)
        if (
            canonical_dataset_requires_release(snapshot.dataset_id)
            or snapshot.release_ref
        ):
            authority_valid = False
            if (
                release_resolver is not None
                and snapshot.release_ref
                and snapshot.authority_record_ref
                and snapshot.rows_content_hash
            ):
                try:
                    authority = release_resolver.resolve_dataset_release(
                        snapshot.release_ref
                    )
                    authority_valid = not dataset_release_authority_integrity_errors(
                        authority
                    ) and snapshot_matches_release_authority(snapshot, authority)
                except (KeyError, TypeError, ValueError):
                    authority_valid = False
            if not authority_valid:
                reasons.append(f"snapshot_release_unverified:{snapshot.snapshot_ref}")
                failure_classes.append(CompletenessFailureClass.AUTHORITY_INTEGRITY)
        if not snapshot.schema_fingerprint:
            reasons.append(f"snapshot_schema_missing:{snapshot.snapshot_ref}")
            failure_classes.append(CompletenessFailureClass.SCHEMA_INTEGRITY)
        required_source_fields = {
            field
            for binding in contract.metric_bindings
            if binding.dataset_id == snapshot.dataset_id
            for field in binding.required_fields
        } | {
            binding.source_field
            for binding in contract.dimension_bindings
            if binding.dataset_id == snapshot.dataset_id
        }
        missing_fields = sorted(required_source_fields - set(snapshot.schema_fields))
        if missing_fields:
            reasons.append(
                f"snapshot_schema_fields_missing:{snapshot.snapshot_ref}:"
                + ",".join(missing_fields)
            )
            failure_classes.append(CompletenessFailureClass.SCHEMA_INTEGRITY)
        try:
            observed = date.fromisoformat(snapshot.watermark)
        except (TypeError, ValueError):
            observed = None
            reasons.append(
                f"snapshot_watermark_invalid:{snapshot.snapshot_ref}:{snapshot.watermark}"
            )
            failure_classes.append(CompletenessFailureClass.SCHEMA_INTEGRITY)
        observed_by_ref[snapshot.snapshot_ref] = snapshot.watermark
        if observed is not None and required is not None and observed < required:
            reasons.append(
                "snapshot_stale:"
                + (f"{snapshot.snapshot_ref}:" if len(snapshots) > 1 else "")
                + f"{observed.isoformat()}:{required.isoformat()}"
            )
            failure_classes.append(CompletenessFailureClass.FRESHNESS)
    return _assertion(
        "snapshot_watermark",
        reasons,
        failure_classes=failure_classes,
        details={
            "observed": observed_by_ref,
            "required": required.isoformat() if required is not None else "",
        },
    )


def _normalize_snapshots(
    snapshots: DatasetSnapshot
    | Mapping[str, DatasetSnapshot]
    | Sequence[DatasetSnapshot],
) -> tuple[DatasetSnapshot, ...]:
    if isinstance(snapshots, DatasetSnapshot):
        return (snapshots,)
    if isinstance(snapshots, Mapping):
        values = tuple(snapshots.values())
        if any(
            not isinstance(item, DatasetSnapshot) or str(key) != item.snapshot_ref
            for key, item in snapshots.items()
        ):
            return ()
        return values
    if isinstance(snapshots, Sequence) and not isinstance(snapshots, (str, bytes)):
        values = tuple(snapshots)
        return (
            values if all(isinstance(item, DatasetSnapshot) for item in values) else ()
        )
    return ()


def _required_fields_assertion(
    contract: QueryContract,
    result: QueryResultEnvelope,
    rows: tuple[Mapping[str, Any], ...],
) -> Mapping[str, Any]:
    reasons = []
    failure_classes = []
    mean_normalized_window_ids = {
        window.window_id
        for window in contract.resolved_windows
        if contract.result_shape.result_semantics == "complete_window_aggregate"
        and window.aggregation == "mean_of_complete_days"
    }
    if rows:
        for field in contract.result_shape.required_fields:
            if any(field not in row for row in rows):
                reasons.append(f"missing_field:{field}")
                failure_classes.append(CompletenessFailureClass.AVAILABILITY)
        for binding in contract.metric_bindings:
            for row in rows:
                if binding.metric_id not in row:
                    continue
                value = row[binding.metric_id]
                if value is None:
                    reasons.append(f"null_required_metric:{binding.metric_id}")
                    failure_classes.append(CompletenessFailureClass.AVAILABILITY)
                    break
                if not _finite_number(value):
                    reasons.append(f"invalid_type:{binding.metric_id}")
                    failure_classes.append(CompletenessFailureClass.SCHEMA_INTEGRITY)
                    break
                if (
                    binding.reconciliation_strategy == "exact_additive_count"
                    and str(row.get("window_id") or "")
                    not in mean_normalized_window_ids
                    and canonical_exact_additive_count(value) is None
                ):
                    reasons.append(
                        f"invalid_type:{binding.metric_id}:exact_additive_count"
                    )
                    failure_classes.append(CompletenessFailureClass.SCHEMA_INTEGRITY)
                    break
        expected_grain = tuple(contract.result_shape.grain)
        observed_grain = tuple(result.observed_grain)
        if observed_grain != expected_grain:
            reasons.append(
                "observed_grain_mismatch:"
                f"{','.join(expected_grain)}:{','.join(observed_grain)}"
            )
            failure_classes.append(CompletenessFailureClass.RESULT_CONSISTENCY)
    return _assertion(
        "required_fields",
        _dedupe(reasons),
        failure_classes=failure_classes,
        details={
            "required": tuple(contract.result_shape.required_fields),
            "observed_grain": tuple(result.observed_grain),
        },
    )


def _required_windows_assertion(
    contract: QueryContract,
    rows: tuple[Mapping[str, Any], ...],
) -> Mapping[str, Any]:
    observed = {
        str(row["window_id"]) for row in rows if row.get("window_id") not in (None, "")
    }
    reasons = ["empty_result"] if not rows else []
    missing_windows = tuple(
        f"missing_required_window:{window_id}"
        for window_id in contract.result_shape.required_window_ids
        if window_id not in observed
    )
    reasons.extend(missing_windows)
    failure_classes = (
        *((CompletenessFailureClass.EMPTY_RESULT,) if not rows else ()),
        *((CompletenessFailureClass.AVAILABILITY,) if missing_windows else ()),
    )
    return _assertion(
        "required_windows",
        reasons,
        failure_classes=failure_classes,
        details={
            "required": tuple(contract.result_shape.required_window_ids),
            "observed": tuple(sorted(observed)),
        },
    )


def _complete_days_assertion(
    contract: QueryContract,
    rows: tuple[Mapping[str, Any], ...],
) -> Mapping[str, Any]:
    counts, membership_reasons = _window_membership(contract, rows)
    reasons = list(membership_reasons)
    for window in contract.resolved_windows:
        actual = counts.get(window.window_id, 0)
        if actual < window.required_complete_days:
            reasons.append(
                f"incomplete_window:{window.window_id}:"
                f"{actual}/{window.required_complete_days}"
            )
    return _assertion(
        "complete_window_days",
        reasons,
        failure_classes=((CompletenessFailureClass.AVAILABILITY,) if reasons else ()),
        details=dict(counts),
    )


def _unique_key_assertion(
    contract: QueryContract,
    rows: tuple[Mapping[str, Any], ...],
) -> Mapping[str, Any]:
    key_fields = tuple(contract.result_shape.unique_key)
    seen = set()
    reasons = []
    for row in rows:
        if any(field not in row for field in key_fields):
            continue
        key = tuple(row[field] for field in key_fields)
        typed_key = canonical_digest(key)
        if typed_key in seen:
            reasons.append(
                "duplicate_key:"
                + ",".join(f"{field}={value}" for field, value in zip(key_fields, key))
            )
        seen.add(typed_key)
    return _assertion(
        "unique_key",
        _dedupe(reasons),
        failure_classes=(
            (CompletenessFailureClass.RESULT_CONSISTENCY,) if reasons else ()
        ),
        details={"fields": key_fields, "unique_count": len(seen)},
    )


def _denominator_assertion(
    contract: QueryContract,
    rows: tuple[Mapping[str, Any], ...],
) -> Mapping[str, Any]:
    reasons = []
    ratio_bindings = tuple(
        binding
        for binding in contract.metric_bindings
        if binding.aggregation == "ratio"
    )
    for binding in ratio_bindings:
        for row in rows:
            value = row.get(binding.metric_id)
            denominator = (
                row.get(binding.denominator_metric)
                if binding.denominator_metric
                else None
            )
            if denominator is not None and (
                not _finite_number(denominator) or float(denominator) < 0
            ):
                reasons.append(f"invalid_denominator:{binding.metric_id}")
            if denominator == 0 and value is not None:
                reasons.append(f"zero_denominator_non_null:{binding.metric_id}")
            if value is not None and not _finite_number(value):
                reasons.append(f"invalid_ratio:{binding.metric_id}")
    return _assertion(
        "valid_denominators",
        _dedupe(reasons),
        failure_classes=(
            (CompletenessFailureClass.ANALYTICAL_QUALITY,) if reasons else ()
        ),
    )


def _provider_bound_assertion(result: QueryResultEnvelope) -> Mapping[str, Any]:
    stats = result.provider_stats
    reasons = []
    for key in ("result_overflow_mode", "overflow_mode"):
        if str(stats.get(key) or "").casefold() == "break":
            reasons.append(f"provider_truncated:{key}=break")
    for key in ("truncated", "result_truncated", "limit_reached"):
        if stats.get(key) is True:
            reasons.append(f"provider_truncated:{key}=true")
    rows_before_limit = stats.get("rows_before_limit_at_least")
    if (
        isinstance(rows_before_limit, int)
        and not isinstance(rows_before_limit, bool)
        and rows_before_limit > result.row_count
    ):
        reasons.append(
            f"provider_truncated:rows_before_limit_at_least={rows_before_limit}"
        )
    return _assertion(
        "provider_not_truncated",
        _dedupe(reasons),
        failure_classes=(
            (CompletenessFailureClass.PROVIDER_TRUNCATION,) if reasons else ()
        ),
        details=dict(stats),
    )


def _aggregate_only_assertion(
    contract: QueryContract,
    rows: tuple[Mapping[str, Any], ...],
) -> Mapping[str, Any]:
    allowed = frozenset(contract.result_shape.required_fields)
    unreviewed = sorted(
        {str(field) for row in rows for field in row if str(field) not in allowed}
    )
    return _assertion(
        "aggregate_only",
        tuple(f"unreviewed_output_field:{field}" for field in unreviewed),
        failure_classes=(
            (CompletenessFailureClass.SCHEMA_INTEGRITY,) if unreviewed else ()
        ),
        details={
            "reviewed_output_fields": tuple(contract.result_shape.required_fields),
            "unreviewed_output_fields": tuple(unreviewed),
        },
    )


def _pending_reconciliation_assertion(
    contract: QueryContract,
    result: QueryResultEnvelope,
) -> Mapping[str, Any] | None:
    if not contract.dimension_bindings or contract.reconciliation_binding is None:
        return None
    binding = contract.reconciliation_binding
    assertion_name = _reconciliation_assertion_name(contract)
    return _assertion(
        assertion_name,
        (f"dimension_total_reconciliation_pending:{binding.reference_query_role_ref}",),
        failure_classes=(CompletenessFailureClass.RECONCILIATION_PENDING,),
        details=_reconciliation_validation_details(
            contract,
            result,
            status="pending",
        ),
    )


def _report_pending_reconciliation_only(
    report: CompletenessReport,
) -> bool:
    reconciliation_names = {
        "dimension_total_reconciliation",
        "overall_channel_reconciliation",
    }
    pending_assertions = tuple(
        assertion
        for assertion in report.assertion_results
        if assertion["assertion"] in reconciliation_names
    )
    if len(pending_assertions) != 1 or pending_assertions[0]["passed"]:
        return False
    if any(
        not assertion["passed"]
        for assertion in report.assertion_results
        if assertion["assertion"] not in reconciliation_names
    ):
        return False
    return set(pending_assertions[0]["failure_classes"]) == {
        CompletenessFailureClass.RECONCILIATION_PENDING.value
    }


def _reconciliation_validation_details(
    contract: QueryContract,
    result: QueryResultEnvelope,
    *,
    status: str,
    reference: tuple[
        QueryContract,
        QueryResultEnvelope,
        CompletenessReport,
    ]
    | None = None,
) -> dict[str, Any]:
    binding = contract.reconciliation_binding
    details: dict[str, Any] = {
        "applicable": True,
        "status": status,
        "primary_query_contract_ref": contract.query_contract_id,
        "primary_result_ref": result.result_ref,
        "primary_report_ref": result.completeness_report_ref,
        "primary_snapshot_refs": tuple(result.source_snapshot_refs),
        "required_query_role_ref": (
            binding.reference_query_role_ref if binding is not None else ""
        ),
        "required_contract_signature": (
            binding.reference_contract_signature if binding is not None else ""
        ),
        "validation_query_contract_ref": "",
        "validation_result_ref": "",
        "validation_report_ref": "",
        "validation_snapshot_refs": (),
    }
    if reference is not None:
        total_contract, total_result, total_report = reference
        details.update(
            {
                "validation_query_contract_ref": (total_contract.query_contract_id),
                "validation_result_ref": total_result.result_ref,
                "validation_report_ref": total_report.report_ref,
                "validation_snapshot_refs": tuple(total_result.source_snapshot_refs),
            }
        )
    return details


def _dimension_total_assertion(
    contract: QueryContract,
    result: QueryResultEnvelope,
    report: CompletenessReport,
    contracts: Sequence[QueryContract],
    results: Sequence[QueryResultEnvelope],
    reports: Sequence[CompletenessReport],
) -> Mapping[str, Any]:
    assertion_name = _reconciliation_assertion_name(contract)
    if not contract.dimension_bindings or contract.reconciliation_binding is None:
        return _assertion(assertion_name, (), details={"applicable": False})

    standalone_ready = (
        report.completeness_status == "complete"
        and report.analysis_readiness == "ready"
    ) or _report_pending_reconciliation_only(report)
    if not standalone_ready:
        return _assertion(
            assertion_name,
            (
                "dimension_result_incomplete:"
                f"{contract.query_contract_id}:"
                f"{report.completeness_status}:{report.analysis_readiness}",
            ),
            failure_classes=(CompletenessFailureClass.RECONCILIATION,),
            details=_reconciliation_validation_details(
                contract,
                result,
                status="failed",
            ),
        )

    reference, reference_reasons = _total_reference(
        contract,
        contracts,
        results,
        reports,
    )
    validation_details = _reconciliation_validation_details(
        contract,
        result,
        status="failed",
        reference=reference,
    )
    if reference_reasons:
        return _assertion(
            assertion_name,
            reference_reasons,
            failure_classes=(CompletenessFailureClass.RECONCILIATION,),
            details=validation_details,
        )
    if reference is None:
        raise AssertionError("validated reconciliation reference missing")
    total_contract, total_result, total_report = reference
    if (
        total_report.completeness_status != "complete"
        or total_report.analysis_readiness != "ready"
    ):
        return _assertion(
            assertion_name,
            (
                "dimension_total_reference_incomplete:"
                f"{total_contract.query_contract_id}:"
                f"{total_report.completeness_status}:"
                f"{total_report.analysis_readiness}",
            ),
            failure_classes=(CompletenessFailureClass.RECONCILIATION,),
            details={
                **validation_details,
                "validation_query_contract_ref": total_contract.query_contract_id,
            },
        )
    reasons = []
    tolerance_details = {}
    reconciled_metrics = []
    non_additive_metrics = []
    for binding in contract.metric_bindings:
        total_binding = next(
            (
                candidate
                for candidate in total_contract.metric_bindings
                if candidate.metric_id == binding.metric_id
            ),
            None,
        )
        if total_binding is None:
            reasons.append(f"dimension_total_metric_missing:{binding.metric_id}")
            continue
        tolerance = binding.reconciliation_tolerance
        tolerance_details[binding.metric_id] = tolerance
        if binding.reconciliation_strategy == "unsupported_non_additive":
            non_additive_metrics.append(binding.metric_id)
            continue
        reconciled_metrics.append(binding.metric_id)
        if binding.reconciliation_strategy == "ratio_from_components":
            reasons.extend(
                _ratio_reconciliation_reasons(
                    binding,
                    total_result.rows,
                    result.rows,
                )
            )
            continue
        total_values = _metric_values(total_result.rows, binding.metric_id)
        dimension_values = _metric_values(result.rows, binding.metric_id)
        for key in sorted(set(total_values) | set(dimension_values)):
            total_value = total_values.get(key)
            dimension_value = dimension_values.get(key)
            if total_value is None or dimension_value is None:
                reasons.append(
                    f"dimension_total_pair_missing:{binding.metric_id}:{key[0]}"
                )
                continue
            if abs(float(dimension_value) - float(total_value)) > tolerance:
                reasons.append(
                    "dimension_total_mismatch:"
                    f"{binding.metric_id}:{key[0]}:"
                    f"{_format_number(dimension_value)}:"
                    f"{_format_number(total_value)}:{tolerance}"
                )
    if non_additive_metrics and not reconciled_metrics:
        reasons.append(
            "dimension_total_reconciliation_unavailable:"
            f"{','.join(non_additive_metrics)}"
        )
    selected_tolerance = (
        next(iter(tolerance_details.values())) if len(tolerance_details) == 1 else None
    )
    validation_details.update(
        {
            "status": "passed" if not reasons else "failed",
            "reference_query_contract_ref": total_contract.query_contract_id,
            "tolerance": selected_tolerance,
            "metric_tolerances": tolerance_details,
            "reconciled_metrics": tuple(reconciled_metrics),
            "non_additive_metrics": tuple(non_additive_metrics),
        }
    )
    return _assertion(
        assertion_name,
        reasons,
        failure_classes=((CompletenessFailureClass.RECONCILIATION,) if reasons else ()),
        details=validation_details,
    )


def _ratio_reconciliation_reasons(
    binding: MetricBinding,
    total_rows: Iterable[Mapping[str, Any]],
    dimension_rows: Iterable[Mapping[str, Any]],
) -> tuple[str, ...]:
    total_ratio = _metric_values(total_rows, binding.metric_id)
    total_numerator = _metric_values(total_rows, binding.numerator_metric)
    total_denominator = _metric_values(total_rows, binding.denominator_metric)
    reasons = []
    keys = set(total_ratio) | set(total_numerator) | set(total_denominator)
    for key in sorted(keys):
        ratio = total_ratio.get(key)
        numerator = total_numerator.get(key)
        denominator = total_denominator.get(key)
        if None in (ratio, numerator, denominator):
            reasons.append(f"ratio_component_missing:{binding.metric_id}:{key[0]}")
            continue
        expected = None if denominator == 0 else float(numerator) / float(denominator)
        if expected is None:
            if ratio is not None:
                reasons.append(
                    f"ratio_zero_denominator_non_null:{binding.metric_id}:{key[0]}"
                )
            continue
        if abs(float(ratio) - expected) > binding.reconciliation_tolerance:
            reasons.append(
                f"ratio_component_mismatch:{binding.metric_id}:{key[0]}:"
                f"{_format_number(expected)}:{_format_number(ratio)}"
            )
    for row in dimension_rows:
        ratio = row.get(binding.metric_id)
        numerator = row.get(binding.numerator_metric)
        denominator = row.get(binding.denominator_metric)
        window_id = str(row.get("window_id") or "")
        if None in (ratio, numerator, denominator):
            reasons.append(f"ratio_component_missing:{binding.metric_id}:{window_id}")
            continue
        if not all(_finite_number(value) for value in (ratio, numerator, denominator)):
            reasons.append(f"ratio_component_invalid:{binding.metric_id}:{window_id}")
            continue
        if denominator == 0:
            reasons.append(
                f"ratio_zero_denominator_non_null:{binding.metric_id}:{window_id}"
            )
            continue
        expected = float(numerator) / float(denominator)
        if abs(float(ratio) - expected) > binding.reconciliation_tolerance:
            reasons.append(
                f"ratio_row_component_mismatch:{binding.metric_id}:{window_id}:"
                f"{_format_number(expected)}:{_format_number(ratio)}"
            )
    return _dedupe(reasons)


def _metric_reconciliation_contract_reasons(
    contract: QueryContract,
) -> tuple[str, ...]:
    strategies = {
        "additive_sum",
        "exact_additive_count",
        "ratio_from_components",
        "unsupported_non_additive",
    }
    metric_ids = {binding.metric_id for binding in contract.metric_bindings}
    reasons = []
    for binding in contract.metric_bindings:
        tolerance = binding.reconciliation_tolerance
        if (
            isinstance(tolerance, bool)
            or not isinstance(tolerance, (int, float))
            or not math.isfinite(float(tolerance))
            or tolerance < 0
        ):
            reasons.append(f"invalid_reconciliation_tolerance:{binding.metric_id}")
        if binding.reconciliation_strategy not in strategies:
            reasons.append(f"invalid_reconciliation_strategy:{binding.metric_id}")
        if binding.reconciliation_strategy == "exact_additive_count" and tolerance != 0:
            reasons.append(f"exact_count_tolerance_must_be_zero:{binding.metric_id}")
        if binding.reconciliation_strategy == "ratio_from_components":
            if (
                not binding.numerator_metric
                or not binding.denominator_metric
                or binding.numerator_metric not in metric_ids
                or binding.denominator_metric not in metric_ids
            ):
                reasons.append(
                    f"ratio_reconciliation_components_missing:{binding.metric_id}"
                )
    return _dedupe(reasons)


def _join_expectation_contract_reasons(
    contract: QueryContract,
) -> tuple[str, ...]:
    expectation = contract.join_expectation
    if expectation is None:
        return ()
    reasons = []
    if expectation.cardinality not in {"one_to_one", "many_to_one"}:
        reasons.append("invalid_join_expectation_cardinality")
    if tuple(expectation.audit_fields) != _JOIN_AUDIT_FIELDS:
        reasons.append("invalid_join_expectation_audit_fields")
    for field_name in ("max_duplicate_keys", "max_unmatched_rows"):
        value = getattr(expectation, field_name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            reasons.append(f"invalid_join_expectation:{field_name}")
    return tuple(reasons)


def _join_cardinality_assertion(
    contract: QueryContract,
    result: QueryResultEnvelope,
) -> Mapping[str, Any]:
    expectation = contract.join_expectation
    if expectation is None:
        return _assertion("join_cardinality", (), details={"applicable": False})
    expected = expectation.cardinality
    reasons = []
    provider_fields = tuple(
        field.removeprefix("__") for field in expectation.audit_fields
    )
    for field in provider_fields:
        if field not in result.provider_stats:
            reasons.append(f"join_audit_missing:{field}")
        elif not _non_negative_int(result.provider_stats[field]):
            reasons.append(f"join_audit_invalid:{field}")
    input_rows = result.provider_stats.get("join_input_rows")
    output_rows = result.provider_stats.get("join_output_rows")
    duplicate_keys = result.provider_stats.get("join_duplicate_keys")
    unmatched_rows = result.provider_stats.get("join_unmatched_rows")
    if (
        expected in {"one_to_one", "many_to_one"}
        and _non_negative_int(input_rows)
        and _non_negative_int(output_rows)
        and output_rows > input_rows
    ):
        reasons.append(f"join_row_expansion:{input_rows}:{output_rows}")
    if (
        _non_negative_int(duplicate_keys)
        and duplicate_keys > expectation.max_duplicate_keys
    ):
        reasons.append(
            "join_duplicate_keys_exceeded:"
            f"{duplicate_keys}:{expectation.max_duplicate_keys}"
        )
    if (
        _non_negative_int(unmatched_rows)
        and unmatched_rows > expectation.max_unmatched_rows
    ):
        reasons.append(
            "join_unmatched_rows_exceeded:"
            f"{unmatched_rows}:{expectation.max_unmatched_rows}"
        )
    return _assertion(
        "join_cardinality",
        reasons,
        failure_classes=(
            (CompletenessFailureClass.RESULT_CONSISTENCY,) if reasons else ()
        ),
        details={
            "applicable": True,
            "expected": expected,
            "observed_input_rows": input_rows,
            "observed_output_rows": output_rows,
            "audit_fields": provider_fields,
        },
    )


def _paired_windows_assertion(
    contract: QueryContract,
    result: QueryResultEnvelope,
) -> Mapping[str, Any]:
    policy = contract.result_shape.dimension_presence_policy
    if policy not in DIMENSION_PRESENCE_POLICIES:
        return _assertion(
            "paired_target_baseline",
            (f"dimension_presence_policy_invalid:{policy or 'missing'}",),
            failure_classes=(CompletenessFailureClass.AUTHORITY_INTEGRITY,),
            details={"applicable": False, "dimension_presence_policy": policy},
        )
    if not contract.dimension_bindings:
        return _assertion(
            "paired_target_baseline",
            (),
            details={"applicable": False, "dimension_presence_policy": policy},
        )
    if policy != "paired_required":
        return _assertion(
            "paired_target_baseline",
            (),
            details={"applicable": False, "dimension_presence_policy": policy},
        )
    contract_roles = {window.role for window in contract.resolved_windows}
    if not {"target", "baseline"}.issubset(contract_roles):
        return _assertion(
            "paired_target_baseline",
            (),
            details={"applicable": False, "dimension_presence_policy": policy},
        )
    dimension_ids = tuple(
        binding.dimension_id for binding in contract.dimension_bindings
    )
    roles_by_dimension: dict[tuple[Any, ...], set[str]] = {}
    for row in result.rows:
        if any(dimension_id not in row for dimension_id in dimension_ids):
            continue
        key = tuple(row[dimension_id] for dimension_id in dimension_ids)
        role = str(row.get("window_role") or "")
        if role:
            roles_by_dimension.setdefault(key, set()).add(role)
    reasons = []
    for key, roles in sorted(
        roles_by_dimension.items(), key=lambda item: repr(item[0])
    ):
        label = ",".join(
            f"{dimension_id}:{value}" for dimension_id, value in zip(dimension_ids, key)
        )
        if "target" not in roles:
            reasons.append(f"unpaired_dimension:{label}:missing_target")
        if "baseline" not in roles:
            reasons.append(f"unpaired_dimension:{label}:missing_baseline")
    return _assertion(
        "paired_target_baseline",
        reasons,
        failure_classes=(
            (CompletenessFailureClass.RESULT_CONSISTENCY,) if reasons else ()
        ),
        details={
            "applicable": True,
            "dimension_ids": dimension_ids,
            "dimension_presence_policy": policy,
        },
    )


def _total_reference(
    contract: QueryContract,
    contracts: Sequence[QueryContract],
    results: Sequence[QueryResultEnvelope],
    reports: Sequence[CompletenessReport],
) -> tuple[
    tuple[QueryContract, QueryResultEnvelope, CompletenessReport] | None,
    tuple[str, ...],
]:
    binding = contract.reconciliation_binding
    if binding is None:
        return None, ("dimension_total_reference_missing",)
    matches = []
    for candidate, result, report in zip(contracts, results, reports):
        if candidate.query_role_ref == binding.reference_query_role_ref:
            matches.append((candidate, result, report))
    if not matches:
        return None, (
            f"dimension_total_reference_missing:{binding.reference_query_role_ref}",
        )
    if len(matches) != 1:
        return None, (
            "dimension_total_reference_ambiguous:"
            f"{binding.reference_query_role_ref}:{len(matches)}",
        )
    reference = matches[0]
    candidate = reference[0]
    if candidate.contract_signature != binding.reference_contract_signature:
        return reference, ("dimension_total_reference_signature_mismatch",)
    overall_channel = (
        _reconciliation_assertion_name(contract) == "overall_channel_reconciliation"
    )
    scope_fields = (
        "analysis_contract_ref",
        "filters",
        "window_refs",
        "resolved_windows",
        "workload_class",
    )
    if not overall_channel:
        scope_fields = (*scope_fields, "dataset_snapshot_refs", "metric_bindings")
    mismatches = tuple(
        f"dimension_total_scope_mismatch:{field_name}"
        for field_name in scope_fields
        if getattr(candidate, field_name) != getattr(contract, field_name)
    )
    if candidate.dimension_bindings:
        mismatches = (*mismatches, "dimension_total_scope_mismatch:dimensions")
    expected_reference_intent = (
        "channel_context_total_probe"
        if overall_channel and contract.query_intent == "channel_context_probe"
        else (
            contract.query_intent
            if contract.result_shape.result_semantics == "complete_window_aggregate"
            and not overall_channel
            else "daily_metric_baselines"
        )
    )
    if candidate.query_intent != expected_reference_intent:
        mismatches = (*mismatches, "dimension_total_scope_mismatch:query_intent")
    if overall_channel and tuple(
        item.metric_id for item in candidate.metric_bindings
    ) != tuple(item.metric_id for item in contract.metric_bindings):
        mismatches = (*mismatches, "dimension_total_scope_mismatch:metric_ids")
    return (reference, _dedupe(mismatches)) if mismatches else (reference, ())


def _reconciliation_assertion_name(contract: QueryContract) -> str:
    if "overall_channel_reconciliation" in contract.completeness_assertions:
        return "overall_channel_reconciliation"
    return "dimension_total_reconciliation"


def _metric_values(
    rows: Iterable[Mapping[str, Any]],
    metric_id: str,
) -> dict[tuple[str, str], float | int]:
    values: dict[tuple[str, str], float | int] = {}
    for row in rows:
        value = row.get(metric_id)
        if not _finite_number(value):
            continue
        key = (
            str(row.get("window_id") or ""),
            str(row.get("observation_key") or ""),
        )
        values[key] = values.get(key, 0) + value
    return values


def _assertion(
    name: str,
    reasons: Iterable[str],
    *,
    failure_classes: Iterable[CompletenessFailureClass | str] = (),
    details: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    typed_reasons = tuple(str(reason) for reason in reasons)
    typed_failure_classes = _dedupe(
        failure_class.value
        if isinstance(failure_class, CompletenessFailureClass)
        else str(failure_class)
        for failure_class in failure_classes
    )
    return {
        "assertion": name,
        "passed": not typed_reasons,
        "failure_reasons": typed_reasons,
        "failure_classes": typed_failure_classes,
        "details": dict(details or {}),
    }


def _not_evaluated_assertion(name: str) -> Mapping[str, Any]:
    return {
        "assertion": name,
        "passed": True,
        "failure_reasons": (),
        "failure_classes": (),
        "details": {"evaluated": False, "reason": "execution_not_succeeded"},
    }


def _failure_reasons(
    assertions: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    return _dedupe(
        reason
        for assertion in assertions
        for reason in assertion.get("failure_reasons", ())
    )


def _window_membership(
    contract: QueryContract,
    rows: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, int], tuple[str, ...]]:
    if contract.result_shape.result_semantics == "complete_context_rows":
        return _context_window_membership(contract, tuple(rows))
    if contract.result_shape.result_semantics == "complete_window_aggregate":
        return _aggregate_window_membership(contract, tuple(rows))
    windows = {window.window_id: window for window in contract.resolved_windows}
    observations: dict[str, set[str]] = {}
    reasons = []
    for row in rows:
        window_id = row.get("window_id")
        observation_key = row.get("observation_key")
        if window_id in (None, "") or observation_key in (None, ""):
            continue
        window_id = str(window_id)
        observation_key = str(observation_key)
        window = windows.get(window_id)
        if window is None:
            reasons.append(f"unexpected_window:{window_id}")
            continue
        valid = True
        observed_role = str(row.get("window_role") or "")
        if observed_role != window.role:
            reasons.append(
                f"window_role_mismatch:{window_id}:"
                f"{observed_role or 'missing'}:{window.role}"
            )
            valid = False
        try:
            observed_date = date.fromisoformat(observation_key)
            start = date.fromisoformat(window.start_inclusive)
            end = date.fromisoformat(window.end_exclusive)
        except (TypeError, ValueError):
            reasons.append(f"invalid_window_observation:{window_id}:{observation_key}")
            valid = False
        else:
            if not start <= observed_date < end:
                reasons.append(
                    f"observation_outside_window:{window_id}:{observation_key}:"
                    f"{window.start_inclusive}:{window.end_exclusive}"
                )
                valid = False
        if valid:
            observations.setdefault(window_id, set()).add(observation_key)
    return (
        {
            window.window_id: len(observations[window.window_id])
            for window in contract.resolved_windows
            if window.window_id in observations
        },
        _canonical_membership_reasons(reasons),
    )


def _aggregate_window_membership(
    contract: QueryContract,
    rows: tuple[Mapping[str, Any], ...],
) -> tuple[dict[str, int], tuple[str, ...]]:
    windows = {window.window_id: window for window in contract.resolved_windows}
    observed_days: dict[str, int] = {}
    reasons: list[str] = []
    for row in rows:
        window_id = str(row.get("window_id") or "")
        window = windows.get(window_id)
        if window is None:
            reasons.append(f"unexpected_window:{window_id or 'missing'}")
            continue
        if str(row.get("window_role") or "") != window.role:
            reasons.append(f"window_role_mismatch:{window_id}")
        if str(row.get("observation_key") or "") != window_id:
            reasons.append(f"invalid_window_aggregate_identity:{window_id}")
        complete_days = row.get("source_complete_days")
        if (
            isinstance(complete_days, bool)
            or not isinstance(complete_days, int)
            or complete_days < 0
        ):
            reasons.append(f"invalid_source_complete_days:{window_id}")
            continue
        previous = observed_days.setdefault(window_id, complete_days)
        if previous != complete_days:
            reasons.append(f"inconsistent_source_complete_days:{window_id}")
    return observed_days, _canonical_membership_reasons(reasons)


def _context_window_membership(
    contract: QueryContract,
    rows: tuple[Mapping[str, Any], ...],
) -> tuple[dict[str, int], tuple[str, ...]]:
    rows_by_window: dict[str, list[Mapping[str, Any]]] = {}
    windows = {window.window_id: window for window in contract.resolved_windows}
    reasons: list[str] = []
    for row in rows:
        window_id = str(row.get("window_id") or "")
        if window_id not in windows:
            reasons.append(f"unexpected_window:{window_id or 'missing'}")
            continue
        rows_by_window.setdefault(window_id, []).append(row)

    counts: dict[str, int] = {}
    content_fields = (
        "source_family",
        "event_type",
        "affected_scope",
        "authority",
        "evidence_level",
        "wording_limit",
        "payload",
    )
    for window_id, window in windows.items():
        window_rows = rows_by_window.get(window_id, [])
        if not window_rows:
            continue
        sentinels = [
            row
            for row in window_rows
            if str(row.get("event_id") or "").startswith("__no_event__:")
        ]
        real_rows = [row for row in window_rows if row not in sentinels]
        valid = True
        for row in window_rows:
            if str(row.get("window_role") or "") != window.role:
                reasons.append(f"window_role_mismatch:{window_id}")
                valid = False
        if sentinels:
            expected_id = f"__no_event__:{window_id}"
            if len(sentinels) != 1 or real_rows:
                reasons.append(f"invalid_context_sentinel_multiplicity:{window_id}")
                valid = False
            sentinel = sentinels[0]
            if (
                sentinel.get("event_id") != expected_id
                or sentinel.get("observation_key") != expected_id
                or sentinel.get("event_count") != 0
            ):
                reasons.append(f"invalid_context_sentinel:{window_id}")
                valid = False
            if any(
                str(sentinel.get(field) or "")
                for field in (
                    "source_family",
                    "event_type",
                    "event_start_date",
                    "event_end_date",
                    "affected_scope",
                    "authority",
                    "evidence_level",
                )
            ):
                reasons.append(f"context_sentinel_contains_event_content:{window_id}")
                valid = False
        else:
            start = date.fromisoformat(window.start_inclusive)
            end = date.fromisoformat(window.end_exclusive)
            for row in real_rows:
                event_id = str(row.get("event_id") or "")
                if not event_id or str(row.get("observation_key") or "") != event_id:
                    reasons.append(f"invalid_context_event_identity:{window_id}")
                    valid = False
                if row.get("event_count") != 1:
                    reasons.append(
                        f"invalid_context_event_count:{window_id}:{event_id}"
                    )
                    valid = False
                if any(not str(row.get(field) or "") for field in content_fields):
                    reasons.append(f"incomplete_context_event:{window_id}:{event_id}")
                    valid = False
                try:
                    event_start = date.fromisoformat(
                        str(row.get("event_start_date") or "")
                    )
                    event_end = date.fromisoformat(str(row.get("event_end_date") or ""))
                except (TypeError, ValueError):
                    reasons.append(
                        f"invalid_context_event_interval:{window_id}:{event_id}"
                    )
                    valid = False
                    continue
                if event_start > event_end or not (
                    event_start < end and event_end >= start
                ):
                    reasons.append(
                        f"context_event_outside_window:{window_id}:{event_id}"
                    )
                    valid = False
                if not _event_recurrence_occurs_in_window(row, start, end):
                    reasons.append(
                        f"context_event_recurrence_outside_window:{window_id}:{event_id}"
                    )
                    valid = False
        if valid:
            counts[window_id] = window.required_complete_days
    return counts, _canonical_membership_reasons(reasons)


def _canonical_membership_reasons(
    reasons: Iterable[str],
) -> tuple[str, ...]:
    return tuple(sorted({str(reason) for reason in reasons}))


def _event_recurrence_occurs_in_window(
    row: Mapping[str, Any],
    start: date,
    end: date,
) -> bool:
    kind = str(row.get("recurrence_kind") or "")
    if not kind:
        return True
    values = tuple(
        row.get(field)
        for field in (
            "recurrence_month_start",
            "recurrence_day_start",
            "recurrence_month_end",
            "recurrence_day_end",
        )
    )
    if any(not isinstance(value, int) or isinstance(value, bool) for value in values):
        return False
    month_start, day_start, month_end, day_end = values
    if kind == "monthly_day_range":
        if month_start != 0 or month_end != 0 or not 1 <= day_start <= day_end <= 31:
            return False
        return any(
            day_start <= (start + timedelta(days=offset)).day <= day_end
            for offset in range((end - start).days)
        )
    if kind != "annual_month_day_range":
        return False
    try:
        date(2000, month_start, day_start)
        date(2000, month_end, day_end)
    except ValueError:
        return False
    start_code = month_start * 100 + day_start
    end_code = month_end * 100 + day_end
    for offset in range((end - start).days):
        current = start + timedelta(days=offset)
        code = current.month * 100 + current.day
        if (start_code <= end_code and start_code <= code <= end_code) or (
            start_code > end_code and (code >= start_code or code <= end_code)
        ):
            return True
    return False


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float, Decimal))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _format_number(value: float | int) -> str:
    return str(value)


def _dedupe(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in values))
