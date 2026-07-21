from __future__ import annotations

from dataclasses import dataclass, fields, replace
import json
from typing import Any, Callable, Mapping, Sequence

from bi_agent.runtime.canonical_values import canonical_thaw
from bi_agent.runtime.analysis_contracts import (
    AnalysisContract,
    QueryContract,
    analysis_contract_from_dict,
    analysis_contract_signature,
    query_contract_from_dict,
    query_contract_signature,
)
from bi_agent.runtime.dataset_catalog import (
    DatasetSnapshot,
)
from bi_agent.runtime.evidence_authority import (
    CapabilityBindingRecord,
    CompletenessRecord,
    EvidenceIntegrityError,
    QueryExecutionRecord,
    RowsRecord,
    RuntimeEvidenceResolver,
    SnapshotRecord,
    canonical_digest,
    canonical_value,
    runtime_evidence_record_integrity_errors,
    _deep_freeze,
)


def authority_record_payload(kind: str, record: Any) -> dict[str, Any]:
    expected = {
        "snapshot": SnapshotRecord,
        "rows": RowsRecord,
        "query_execution": QueryExecutionRecord,
        "completeness": CompletenessRecord,
        "capability_binding": CapabilityBindingRecord,
    }
    if type(record) is not expected.get(kind):
        raise EvidenceIntegrityError(f"runtime_authority_record_type_invalid:{kind}")
    errors = runtime_evidence_record_integrity_errors(record)
    if errors:
        raise EvidenceIntegrityError(errors[0])
    return canonical_value({"kind": kind, "record": record})


@dataclass(frozen=True)
class CapabilitySettlementAuthority:
    """The exact runtime-authority subgraph admitted with one capability outcome."""

    run_id: str
    analysis_contract: Mapping[str, Any]
    query_contracts: tuple[QueryContract, ...]
    query_execution_records: tuple[QueryExecutionRecord, ...]
    rows_records: tuple[RowsRecord, ...]
    snapshot_records: tuple[SnapshotRecord, ...]
    completeness_records: tuple[CompletenessRecord, ...]
    capability_binding_records: tuple[CapabilityBindingRecord, ...]
    content_digest: str

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        analysis_contract: Mapping[str, Any],
        query_contracts: Sequence[QueryContract],
        query_execution_records: Sequence[QueryExecutionRecord],
        rows_records: Sequence[RowsRecord],
        snapshot_records: Sequence[SnapshotRecord],
        completeness_records: Sequence[CompletenessRecord],
        capability_binding_records: Sequence[CapabilityBindingRecord],
    ) -> "CapabilitySettlementAuthority":
        ordered = {
            "query_contracts": tuple(
                sorted(query_contracts, key=lambda item: item.query_contract_id)
            ),
            "query_execution_records": tuple(
                sorted(query_execution_records, key=lambda item: item.result_ref)
            ),
            "rows_records": tuple(
                sorted(rows_records, key=lambda item: item.record_ref)
            ),
            "snapshot_records": tuple(
                sorted(snapshot_records, key=lambda item: item.snapshot_ref)
            ),
            "completeness_records": tuple(
                sorted(completeness_records, key=lambda item: item.record_ref)
            ),
            "capability_binding_records": tuple(
                sorted(capability_binding_records, key=lambda item: item.record_ref)
            ),
        }
        bundle = _validate_capability_settlement_records(
            run_id=run_id,
            analysis_contract=analysis_contract,
            **ordered,
        )
        digest_payload = {
            "run_id": run_id,
            "analysis_contract": bundle["analysis_contract"],
            **{
                key: bundle[key]
                for key in (
                    "query_contracts",
                    "query_execution_records",
                    "rows_records",
                    "snapshot_records",
                    "completeness_records",
                    "capability_binding_records",
                )
            },
        }
        return cls(
            run_id=run_id,
            analysis_contract=_deep_freeze(bundle["analysis_contract"]),
            query_contracts=bundle["query_contracts"],
            query_execution_records=bundle["query_execution_records"],
            rows_records=bundle["rows_records"],
            snapshot_records=bundle["snapshot_records"],
            completeness_records=bundle["completeness_records"],
            capability_binding_records=bundle["capability_binding_records"],
            content_digest=canonical_digest(digest_payload),
        )

    @classmethod
    def from_resolver(
        cls,
        *,
        run_id: str,
        analysis_contract: AnalysisContract,
        query_contracts: Sequence[QueryContract],
        binding_refs: Sequence[str],
        evidence_resolver: RuntimeEvidenceResolver,
    ) -> "CapabilitySettlementAuthority":
        if type(analysis_contract) is not AnalysisContract:
            raise EvidenceIntegrityError(
                "capability_settlement_analysis_contract_invalid"
            )
        if not isinstance(evidence_resolver, RuntimeEvidenceResolver):
            raise EvidenceIntegrityError(
                "capability_settlement_evidence_resolver_invalid"
            )
        normalized_refs = _capability_settlement_refs(binding_refs)
        query_by_ref = _capability_settlement_unique(
            query_contracts,
            key="query_contract_id",
            kind="query_contract",
        )
        bindings = tuple(
            _capability_settlement_resolve(
                evidence_resolver.resolve_capability_binding,
                ref,
                expected=CapabilityBindingRecord,
                identity="record_ref",
                kind="binding",
            )
            for ref in normalized_refs
        )
        selected_query_refs = _capability_settlement_binding_values(
            bindings,
            "query_contract_refs",
            "validation_query_contract_refs",
        )
        selected_queries = tuple(
            _capability_settlement_existing(
                query_by_ref,
                ref,
                kind="query_contract",
            )
            for ref in selected_query_refs
        )
        query_records = tuple(
            _capability_settlement_resolve(
                evidence_resolver.resolve_query_execution_record,
                ref,
                expected=QueryExecutionRecord,
                identity="record_ref",
                kind="query_execution",
            )
            for ref in _capability_settlement_binding_values(
                bindings,
                "query_execution_record_refs",
                "validation_query_execution_record_refs",
            )
        )
        rows_records = tuple(
            _capability_settlement_resolve(
                evidence_resolver.resolve_rows_record,
                ref,
                expected=RowsRecord,
                identity="record_ref",
                kind="rows",
            )
            for ref in _capability_settlement_binding_values(
                bindings,
                "rows_metadata_record_refs",
                "validation_rows_metadata_record_refs",
            )
        )
        completeness_records = tuple(
            _capability_settlement_resolve(
                evidence_resolver.resolve_completeness,
                ref,
                expected=CompletenessRecord,
                identity="record_ref",
                kind="completeness",
            )
            for ref in _capability_settlement_binding_values(
                bindings,
                "completeness_record_refs",
                "validation_completeness_record_refs",
            )
        )
        snapshot_refs = tuple(
            dict.fromkeys(
                (
                    *_capability_settlement_binding_values(
                        bindings,
                        "source_snapshot_refs",
                        "validation_source_snapshot_refs",
                    ),
                    *(
                        ref
                        for record in query_records
                        for ref in record.source_snapshot_refs
                    ),
                )
            )
        )
        snapshot_records = tuple(
            _capability_settlement_resolve(
                evidence_resolver.resolve_snapshot,
                ref,
                expected=SnapshotRecord,
                identity="snapshot_ref",
                kind="snapshot",
            )
            for ref in snapshot_refs
        )
        signed_analysis = {
            **analysis_contract.to_dict(),
            "contract_signature": analysis_contract_signature(analysis_contract),
        }
        return cls.create(
            run_id=run_id,
            analysis_contract=signed_analysis,
            query_contracts=selected_queries,
            query_execution_records=query_records,
            rows_records=rows_records,
            snapshot_records=snapshot_records,
            completeness_records=completeness_records,
            capability_binding_records=bindings,
        )

    def revalidated(self) -> "CapabilitySettlementAuthority":
        rebuilt = self.create(
            run_id=self.run_id,
            analysis_contract=self.analysis_contract,
            query_contracts=self.query_contracts,
            query_execution_records=self.query_execution_records,
            rows_records=self.rows_records,
            snapshot_records=self.snapshot_records,
            completeness_records=self.completeness_records,
            capability_binding_records=self.capability_binding_records,
        )
        if rebuilt.content_digest != self.content_digest:
            raise EvidenceIntegrityError("capability_settlement_digest_invalid")
        return rebuilt

    def for_binding_refs(
        self,
        binding_refs: Sequence[str],
    ) -> "CapabilitySettlementAuthority":
        source = self.revalidated()
        normalized_refs = _capability_settlement_refs(binding_refs)
        bindings_by_ref = {
            item.record_ref: item for item in source.capability_binding_records
        }
        bindings = tuple(
            _capability_settlement_existing(
                bindings_by_ref,
                ref,
                kind="binding_ref",
            )
            for ref in normalized_refs
        )
        query_refs = _capability_settlement_binding_values(
            bindings,
            "query_contract_refs",
            "validation_query_contract_refs",
        )
        query_record_refs = _capability_settlement_binding_values(
            bindings,
            "query_execution_record_refs",
            "validation_query_execution_record_refs",
        )
        rows_record_refs = _capability_settlement_binding_values(
            bindings,
            "rows_metadata_record_refs",
            "validation_rows_metadata_record_refs",
        )
        completeness_refs = _capability_settlement_binding_values(
            bindings,
            "completeness_record_refs",
            "validation_completeness_record_refs",
        )
        queries_by_ref = {
            item.query_contract_id: item for item in source.query_contracts
        }
        query_records_by_ref = {
            item.record_ref: item for item in source.query_execution_records
        }
        rows_by_ref = {item.record_ref: item for item in source.rows_records}
        completeness_by_ref = {
            item.record_ref: item for item in source.completeness_records
        }
        selected_query_records = tuple(
            _capability_settlement_existing(
                query_records_by_ref,
                ref,
                kind="query_execution",
            )
            for ref in query_record_refs
        )
        snapshot_refs = tuple(
            dict.fromkeys(
                (
                    *_capability_settlement_binding_values(
                        bindings,
                        "source_snapshot_refs",
                        "validation_source_snapshot_refs",
                    ),
                    *(
                        ref
                        for record in selected_query_records
                        for ref in record.source_snapshot_refs
                    ),
                )
            )
        )
        snapshots_by_ref = {item.snapshot_ref: item for item in source.snapshot_records}
        return self.create(
            run_id=source.run_id,
            analysis_contract=source.analysis_contract,
            query_contracts=tuple(
                _capability_settlement_existing(
                    queries_by_ref,
                    ref,
                    kind="query_contract",
                )
                for ref in query_refs
            ),
            query_execution_records=selected_query_records,
            rows_records=tuple(
                _capability_settlement_existing(
                    rows_by_ref,
                    ref,
                    kind="rows",
                )
                for ref in rows_record_refs
            ),
            snapshot_records=tuple(
                _capability_settlement_existing(
                    snapshots_by_ref,
                    ref,
                    kind="snapshot",
                )
                for ref in snapshot_refs
            ),
            completeness_records=tuple(
                _capability_settlement_existing(
                    completeness_by_ref,
                    ref,
                    kind="completeness",
                )
                for ref in completeness_refs
            ),
            capability_binding_records=bindings,
        )


def _capability_settlement_refs(refs: Sequence[str]) -> tuple[str, ...]:
    if isinstance(refs, (str, bytes)) or not isinstance(refs, Sequence):
        raise EvidenceIntegrityError("capability_settlement_binding_refs_invalid")
    normalized = tuple(refs)
    if any(type(ref) is not str or not ref or ref != ref.strip() for ref in normalized):
        raise EvidenceIntegrityError("capability_settlement_binding_refs_invalid")
    if len(normalized) != len(set(normalized)):
        raise EvidenceIntegrityError("capability_settlement_binding_refs_duplicate")
    return tuple(sorted(normalized))


def _capability_settlement_unique(
    records: Sequence[Any],
    *,
    key: str,
    kind: str,
) -> dict[str, Any]:
    indexed = {str(getattr(item, key, "")): item for item in records}
    if len(indexed) != len(records) or "" in indexed:
        raise EvidenceIntegrityError(f"capability_settlement_{kind}_duplicate")
    return indexed


def _capability_settlement_binding_values(
    bindings: Sequence[CapabilityBindingRecord],
    primary: str,
    validation: str,
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            ref
            for binding in bindings
            for ref in (*getattr(binding, primary), *getattr(binding, validation))
        )
    )


def _capability_settlement_resolve(
    resolver: Callable[[str], Any],
    ref: str,
    *,
    expected: type,
    identity: str,
    kind: str,
) -> Any:
    record = resolver(ref)
    if type(record) is not expected or getattr(record, identity, None) != ref:
        raise EvidenceIntegrityError(
            f"capability_settlement_{kind}_record_missing:{ref}"
        )
    return record


def _capability_settlement_existing(
    indexed: Mapping[str, Any],
    ref: str,
    *,
    kind: str,
) -> Any:
    record = indexed.get(ref)
    if record is None:
        error = (
            "capability_settlement_binding_ref_missing"
            if kind == "binding_ref"
            else f"capability_settlement_{kind}_record_missing:{ref}"
        )
        raise EvidenceIntegrityError(error)
    return record


def _validate_capability_settlement_records(
    *,
    run_id: str,
    analysis_contract: Mapping[str, Any],
    query_contracts: Sequence[QueryContract],
    query_execution_records: Sequence[QueryExecutionRecord],
    rows_records: Sequence[RowsRecord],
    snapshot_records: Sequence[SnapshotRecord],
    completeness_records: Sequence[CompletenessRecord],
    capability_binding_records: Sequence[CapabilityBindingRecord],
) -> dict[str, Any]:
    """Close the exact evidence subgraph admitted with one capability outcome."""
    if not run_id:
        raise EvidenceIntegrityError("runtime_persistence_run_id_missing")
    if not isinstance(analysis_contract, Mapping):
        raise EvidenceIntegrityError("runtime_persistence_analysis_contract_invalid")
    expected_analysis_keys = {
        *AnalysisContract.__dataclass_fields__,
        "contract_signature",
    }
    if set(analysis_contract) != expected_analysis_keys:
        raise EvidenceIntegrityError(
            "runtime_persistence_analysis_contract_shape_invalid"
        )
    try:
        typed_analysis = analysis_contract_from_dict(
            {
                key: value
                for key, value in analysis_contract.items()
                if key != "contract_signature"
            }
        )
    except (TypeError, ValueError) as exc:
        raise EvidenceIntegrityError(
            f"runtime_persistence_analysis_contract_shape_invalid:{exc}"
        ) from exc
    signature = str(analysis_contract.get("contract_signature") or "")
    analysis_ref = typed_analysis.analysis_contract_id
    if len(signature) != 64:
        raise EvidenceIntegrityError(
            "runtime_persistence_analysis_contract_identity_invalid"
        )
    if signature != analysis_contract_signature(typed_analysis):
        raise EvidenceIntegrityError(
            "runtime_persistence_analysis_contract_signature_invalid"
        )
    analysis_payload = canonical_value(
        {**typed_analysis.to_dict(), "contract_signature": signature}
    )
    if any(type(item) is not QueryContract for item in query_contracts):
        raise EvidenceIntegrityError("runtime_persistence_query_contract_type_invalid")
    query_by_ref = {item.query_contract_id: item for item in query_contracts}
    if len(query_by_ref) != len(query_contracts):
        raise EvidenceIntegrityError("runtime_persistence_query_contract_duplicate")
    for contract in query_contracts:
        if (
            contract.analysis_contract_ref != analysis_ref
            or query_contract_signature(contract) != contract.contract_signature
        ):
            raise EvidenceIntegrityError("runtime_persistence_query_contract_integrity")

    typed_groups = (
        ("query_execution", QueryExecutionRecord, query_execution_records),
        ("rows", RowsRecord, rows_records),
        ("snapshot", SnapshotRecord, snapshot_records),
        ("completeness", CompletenessRecord, completeness_records),
        ("capability_binding", CapabilityBindingRecord, capability_binding_records),
    )
    for kind, expected, records in typed_groups:
        for record in records:
            if type(record) is not expected:
                raise EvidenceIntegrityError(f"runtime_persistence_{kind}_type_invalid")
            errors = runtime_evidence_record_integrity_errors(record)
            if errors:
                raise EvidenceIntegrityError(errors[0])

    query_records = _unique_by(
        query_execution_records,
        "result_ref",
        "runtime_persistence_query_result_duplicate",
    )
    rows_by_ref = _unique_by(
        rows_records, "rows_ref", "runtime_persistence_rows_record_duplicate"
    )
    snapshots_by_ref = _unique_by(
        snapshot_records,
        "snapshot_ref",
        "runtime_persistence_snapshot_record_duplicate",
    )
    completeness_by_ref = _unique_by(
        completeness_records,
        "record_ref",
        "runtime_persistence_completeness_record_duplicate",
    )
    _unique_by(
        capability_binding_records,
        "record_ref",
        "runtime_persistence_capability_binding_duplicate",
    )
    _validate_query_contract_analysis_closure(
        typed_analysis,
        query_contracts,
        snapshots_by_ref,
    )
    for query in query_execution_records:
        contract = query_by_ref.get(query.query_contract_ref)
        if contract is None or canonical_value(contract) != canonical_value(
            query.contract
        ):
            raise EvidenceIntegrityError("runtime_persistence_query_contract_missing")
        rows = rows_by_ref.get(query.rows_ref)
        if rows is None:
            raise EvidenceIntegrityError("runtime_persistence_rows_record_missing")
        if (
            rows.row_count != query.row_count
            or rows.rows_content_hash != query.rows_content_hash
        ):
            raise EvidenceIntegrityError(
                "runtime_persistence_rows_record_link_mismatch"
            )
        for snapshot_ref, record_ref, record_digest in zip(
            query.source_snapshot_refs,
            query.source_snapshot_record_refs,
            query.source_snapshot_record_digests,
        ):
            snapshot = snapshots_by_ref.get(snapshot_ref)
            if snapshot is None:
                raise EvidenceIntegrityError(
                    "runtime_persistence_snapshot_record_missing"
                )
            if (
                snapshot.record_ref != record_ref
                or snapshot.record_digest != record_digest
            ):
                raise EvidenceIntegrityError(
                    "runtime_persistence_snapshot_record_link_mismatch"
                )
    reports_by_result = _group_by(completeness_records, "result_ref")
    for report in completeness_records:
        query = query_records.get(report.result_ref)
        if (
            query is None
            or report.query_contract_ref != query.query_contract_ref
            or report.report_ref != query.completeness_report_ref
        ):
            raise EvidenceIntegrityError(
                "runtime_persistence_completeness_link_mismatch"
            )
    if set(reports_by_result) != set(query_records):
        raise EvidenceIntegrityError(
            "runtime_persistence_completeness_chain_incomplete"
        )

    all_report_records = set(completeness_by_ref)
    bound_result_refs: set[str] = set()
    for binding in capability_binding_records:
        if binding.analysis_contract_ref != analysis_ref:
            raise EvidenceIntegrityError(
                "runtime_persistence_binding_analysis_contract_mismatch"
            )
        _validate_capability_binding_analysis_closure(
            typed_analysis,
            binding,
            query_by_ref,
        )
        groups = (
            (
                binding.query_contract_refs,
                binding.result_refs,
                binding.query_execution_record_refs,
                binding.query_execution_record_digests,
                binding.rows_refs,
                binding.rows_metadata_record_refs,
                binding.rows_metadata_record_digests,
                binding.rows_content_hashes,
                binding.completeness_report_refs,
                binding.completeness_record_refs,
                binding.completeness_record_digests,
            ),
            (
                binding.validation_query_contract_refs,
                binding.validation_result_refs,
                binding.validation_query_execution_record_refs,
                binding.validation_query_execution_record_digests,
                binding.validation_rows_refs,
                binding.validation_rows_metadata_record_refs,
                binding.validation_rows_metadata_record_digests,
                binding.validation_rows_content_hashes,
                binding.validation_completeness_report_refs,
                binding.validation_completeness_record_refs,
                binding.validation_completeness_record_digests,
            ),
        )
        for (
            query_refs,
            result_refs,
            query_record_refs,
            query_record_digests,
            rows_refs,
            rows_record_refs,
            rows_record_digests,
            rows_hashes,
            report_aliases,
            report_refs,
            report_digests,
        ) in groups:
            for index, result_ref in enumerate(result_refs):
                bound_result_refs.add(result_ref)
                query = query_records.get(result_ref)
                if (
                    query is None
                    or query.query_contract_ref != query_refs[index]
                    or query.record_ref != query_record_refs[index]
                    or query.record_digest != query_record_digests[index]
                    or query.rows_ref != rows_refs[index]
                ):
                    raise EvidenceIntegrityError(
                        "runtime_persistence_binding_query_link_mismatch"
                    )
                rows = rows_by_ref.get(rows_refs[index])
                if (
                    rows is None
                    or rows.record_ref != rows_record_refs[index]
                    or rows.record_digest != rows_record_digests[index]
                    or rows.rows_content_hash != rows_hashes[index]
                ):
                    raise EvidenceIntegrityError(
                        "runtime_persistence_binding_rows_link_mismatch"
                    )
                if report_refs[index] not in all_report_records:
                    raise EvidenceIntegrityError(
                        "runtime_persistence_binding_completeness_missing"
                    )
                report = completeness_by_ref[report_refs[index]]
                if (
                    report.result_ref != result_ref
                    or report.report_ref != report_aliases[index]
                    or report.report_digest != report_digests[index]
                ):
                    raise EvidenceIntegrityError(
                        "runtime_persistence_binding_completeness_link_mismatch"
                    )
    if set(query_records) != bound_result_refs:
        raise EvidenceIntegrityError("capability_settlement_binding_chain_incomplete")
    return {
        "analysis_contract": analysis_payload,
        "query_contracts": tuple(query_contracts),
        "query_execution_records": tuple(query_execution_records),
        "rows_records": tuple(rows_records),
        "snapshot_records": tuple(snapshot_records),
        "completeness_records": tuple(completeness_records),
        "capability_binding_records": tuple(capability_binding_records),
    }


def _unique_by(records: Sequence[Any], field: str, code: str) -> dict[str, Any]:
    result = {}
    for record in records:
        ref = str(getattr(record, field))
        if ref in result:
            raise EvidenceIntegrityError(code)
        result[ref] = record
    return result


def _group_by(records: Sequence[Any], field: str) -> dict[str, tuple[Any, ...]]:
    grouped: dict[str, list[Any]] = {}
    for record in records:
        grouped.setdefault(str(getattr(record, field)), []).append(record)
    return {key: tuple(values) for key, values in grouped.items()}


def _validate_query_contract_analysis_closure(
    analysis: AnalysisContract,
    contracts: Sequence[QueryContract],
    snapshots_by_ref: Mapping[str, SnapshotRecord],
) -> None:
    for contract in contracts:
        _validate_query_contract_analysis_semantics(analysis, contract)
        for snapshot_ref in contract.dataset_snapshot_refs:
            snapshot = snapshots_by_ref.get(snapshot_ref)
            if snapshot is None or snapshot.snapshot.dataset_id not in set(
                analysis.dataset_requirements
            ):
                raise EvidenceIntegrityError(
                    "runtime_persistence_query_dataset_requirement_mismatch"
                )


def _validate_query_contract_analysis_semantics(
    analysis: AnalysisContract,
    contract: QueryContract,
) -> None:
    metrics = {
        (item.metric_id, item.dataset_id): item for item in analysis.metric_bindings
    }
    dimensions = {
        (item.dimension_id, item.dataset_id): item
        for item in analysis.dimension_bindings
    }
    windows = {item.window_id: item for item in analysis.resolved_windows}
    datasets = set(analysis.dataset_requirements)
    if any(
        item.dataset_id not in datasets
        for item in (*analysis.metric_bindings, *analysis.dimension_bindings)
    ):
        raise EvidenceIntegrityError(
            "runtime_persistence_analysis_dataset_requirement_mismatch"
        )
    for metric in contract.metric_bindings:
        if canonical_value(
            metrics.get((metric.metric_id, metric.dataset_id))
        ) != canonical_value(metric):
            raise EvidenceIntegrityError(
                "runtime_persistence_query_metric_binding_mismatch"
            )
        if metric.dataset_id not in datasets:
            raise EvidenceIntegrityError(
                "runtime_persistence_query_dataset_requirement_mismatch"
            )
    for dimension in contract.dimension_bindings:
        if canonical_value(
            dimensions.get((dimension.dimension_id, dimension.dataset_id))
        ) != canonical_value(dimension):
            raise EvidenceIntegrityError(
                "runtime_persistence_query_dimension_binding_mismatch"
            )
        if dimension.dataset_id not in datasets:
            raise EvidenceIntegrityError(
                "runtime_persistence_query_dataset_requirement_mismatch"
            )
    if set(contract.window_refs) != {
        item.window_id for item in contract.resolved_windows
    }:
        raise EvidenceIntegrityError("runtime_persistence_query_window_ref_mismatch")
    for window in contract.resolved_windows:
        if canonical_value(windows.get(window.window_id)) != canonical_value(window):
            raise EvidenceIntegrityError(
                "runtime_persistence_query_window_binding_mismatch"
            )


def _analysis_contract_from_envelope(
    raw: Any,
    *,
    stored_signature: str,
    code: str,
) -> AnalysisContract:
    if not isinstance(raw, Mapping) or set(raw) != {
        *AnalysisContract.__dataclass_fields__,
        "contract_signature",
    }:
        raise EvidenceIntegrityError(code)
    try:
        analysis = analysis_contract_from_dict(
            {key: value for key, value in raw.items() if key != "contract_signature"}
        )
    except (TypeError, ValueError) as exc:
        raise EvidenceIntegrityError(code) from exc
    if (
        str(raw.get("contract_signature") or "") != stored_signature
        or analysis_contract_signature(analysis) != stored_signature
    ):
        raise EvidenceIntegrityError(code)
    return analysis


def _validate_capability_binding_analysis_closure(
    analysis: AnalysisContract,
    binding: CapabilityBindingRecord,
    query_by_ref: Mapping[str, QueryContract] | None,
) -> None:
    if binding.capability_id not in set(analysis.capability_requirements):
        raise EvidenceIntegrityError(
            "runtime_persistence_binding_capability_requirement_mismatch"
        )
    if query_by_ref is not None:
        binding_query_refs = tuple(
            dict.fromkeys(
                (
                    *binding.query_contract_refs,
                    *binding.validation_query_contract_refs,
                )
            )
        )
        missing = tuple(ref for ref in binding_query_refs if ref not in query_by_ref)
        if missing:
            raise EvidenceIntegrityError(
                "runtime_persistence_binding_query_contract_missing:"
                + ",".join(missing)
            )


class PostgresRuntimeEvidenceResolver(RuntimeEvidenceResolver):
    """Read-only adapter from normalized PostgreSQL authority rows to Task 6 records."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def resolve_query_execution(self, result_ref: str) -> QueryExecutionRecord | None:
        return self._resolve_query("q.result_ref = %(ref)s", result_ref)

    def resolve_query_execution_record(
        self, record_ref: str
    ) -> QueryExecutionRecord | None:
        return self._resolve_query("q.record_ref = %(ref)s", record_ref)

    def _resolve_query(self, where: str, ref: str) -> QueryExecutionRecord | None:
        row = self._one(
            f"""
            SELECT q.record_ref, q.record_digest, q.result_ref,
                   q.query_contract_ref, q.rows_ref, q.payload,
                   q.run_id AS authority_run_id,
                   qr.result_ref AS run_result_ref, qr.run_id,
                   qr.query_contract_id AS run_query_contract_id,
                   qr.execution_status, qr.query_hash AS run_query_hash,
                   qr.rows_ref AS run_rows_ref,
                   qr.completeness_report_ref AS run_completeness_report_ref,
                   qr.payload AS result_payload,
                   qc.query_contract_id AS contract_query_id,
                   qc.run_id AS contract_run_id,
                   qc.analysis_contract_id,
                   qc.contract_signature AS stored_contract_signature,
                   qc.payload AS contract_payload,
                   ac.run_id AS analysis_run_id,
                   ac.contract_signature AS stored_analysis_signature,
                   ac.payload AS analysis_payload,
                   ar.run_id AS analysis_run_id_actual,
                   ar.thread_id, ar.topic_id
            FROM waje_runtime.query_execution_authority q
            JOIN waje_runtime.query_runs qr ON qr.result_ref = q.result_ref
            JOIN waje_runtime.query_contracts qc
              ON qc.query_contract_id = qr.query_contract_id
            JOIN waje_runtime.analysis_contracts ac
              ON ac.analysis_contract_id = qc.analysis_contract_id
            JOIN waje_runtime.analysis_runs ar ON ar.run_id = qr.run_id
            WHERE {where}
            """,
            {"ref": str(ref)},
        )
        if row is None:
            return None
        row = _row_mapping(
            row,
            (
                "record_ref",
                "record_digest",
                "result_ref",
                "query_contract_ref",
                "rows_ref",
                "payload",
                "authority_run_id",
                "run_result_ref",
                "run_id",
                "run_query_contract_id",
                "execution_status",
                "run_query_hash",
                "run_rows_ref",
                "run_completeness_report_ref",
                "result_payload",
                "contract_query_id",
                "contract_run_id",
                "analysis_contract_id",
                "stored_contract_signature",
                "contract_payload",
                "analysis_run_id",
                "stored_analysis_signature",
                "analysis_payload",
                "analysis_run_id_actual",
                "thread_id",
                "topic_id",
            ),
        )
        record = _record_from_row("query_execution", row)
        _require_columns(
            record,
            row,
            {
                "record_ref": "record_ref",
                "record_digest": "record_digest",
                "result_ref": "result_ref",
                "query_contract_ref": "query_contract_ref",
                "rows_ref": "rows_ref",
            },
            "query_execution_record_column_mismatch",
        )
        _validate_query_execution_join(record, row)
        return record

    def resolve_rows(self, rows_ref: str) -> RowsRecord | None:
        return self._resolve_rows("r.rows_ref = %(ref)s", rows_ref)

    def resolve_rows_record(self, record_ref: str) -> RowsRecord | None:
        return self._resolve_rows("r.record_ref = %(ref)s", record_ref)

    def _resolve_rows(self, where: str, ref: str) -> RowsRecord | None:
        row = self._one(
            f"""
            SELECT r.record_ref, r.record_digest, r.rows_ref,
                   r.rows_content_hash, r.row_count, r.unique_key_fields,
                   r.storage_ref, r.payload,
                   q.run_id AS authority_run_id,
                   q.rows_ref AS query_rows_ref,
                   qr.run_id, qr.rows_ref AS run_rows_ref
            FROM waje_runtime.rows_metadata_authority r
            JOIN waje_runtime.query_execution_authority q ON q.rows_ref = r.rows_ref
            JOIN waje_runtime.query_runs qr ON qr.result_ref = q.result_ref
            WHERE {where}
            """,
            {"ref": str(ref)},
        )
        if row is None:
            return None
        row = _row_mapping(
            row,
            (
                "record_ref",
                "record_digest",
                "rows_ref",
                "rows_content_hash",
                "row_count",
                "unique_key_fields",
                "storage_ref",
                "payload",
                "authority_run_id",
                "query_rows_ref",
                "run_id",
                "run_rows_ref",
            ),
        )
        record = _record_from_row("rows", row)
        _require_columns(
            record,
            row,
            {
                "record_ref": "record_ref",
                "record_digest": "record_digest",
                "rows_ref": "rows_ref",
                "rows_content_hash": "rows_content_hash",
                "row_count": "row_count",
                "unique_key_fields": "unique_key_fields",
                "storage_ref": "storage_ref",
            },
            "rows_record_column_mismatch",
        )
        if (
            str(row.get("authority_run_id") or "") != str(row.get("run_id") or "")
            or record.rows_ref != row.get("query_rows_ref")
            or record.rows_ref != row.get("run_rows_ref")
        ):
            raise EvidenceIntegrityError("rows_record_run_membership_mismatch")
        return record

    def resolve_snapshot(self, snapshot_ref: str) -> SnapshotRecord | None:
        row = self._one(
            """
            SELECT s.record_ref, s.record_digest, s.snapshot_ref, s.payload
            FROM waje_runtime.snapshot_authority s
            WHERE s.snapshot_ref = %(ref)s
            """,
            {"ref": str(snapshot_ref)},
        )
        if row is None:
            return None
        row = _row_mapping(
            row, ("record_ref", "record_digest", "snapshot_ref", "payload")
        )
        record = _record_from_row("snapshot", row)
        _require_columns(
            record,
            row,
            {
                "record_ref": "record_ref",
                "record_digest": "record_digest",
                "snapshot_ref": "snapshot_ref",
            },
            "snapshot_record_column_mismatch",
        )
        return record

    def resolve_completeness(self, record_ref: str) -> CompletenessRecord | None:
        return self._resolve_completeness("c.record_ref = %(ref)s", record_ref)

    def resolve_latest_completeness(self, report_ref: str) -> CompletenessRecord | None:
        return self._resolve_completeness(
            "c.report_ref = %(ref)s ORDER BY c.created_at DESC, c.record_ref DESC LIMIT 1",
            report_ref,
        )

    def _resolve_completeness(self, where: str, ref: str) -> CompletenessRecord | None:
        row = self._one(
            f"""
            SELECT c.record_ref, c.report_ref, c.report_digest,
                   c.result_ref, c.query_contract_ref, c.payload,
                   c.run_id AS authority_run_id,
                   c.completeness_status AS stored_completeness_status,
                   c.analysis_readiness AS stored_analysis_readiness,
                   qr.run_id, qr.query_contract_id AS run_query_contract_id,
                   qr.result_ref AS run_result_ref,
                   qr.completeness_report_ref AS run_completeness_report_ref,
                   qc.run_id AS contract_run_id,
                   qc.query_contract_id AS contract_query_id,
                   qc.analysis_contract_id,
                   qc.contract_signature AS stored_contract_signature,
                   qc.payload AS contract_payload,
                   ac.run_id AS analysis_run_id,
                   ac.contract_signature AS stored_analysis_signature,
                   ac.payload AS analysis_payload,
                   ar.run_id AS analysis_run_id_actual
            FROM waje_runtime.query_completeness_reports c
            JOIN waje_runtime.query_runs qr ON qr.result_ref = c.result_ref
            JOIN waje_runtime.query_contracts qc
              ON qc.query_contract_id = c.query_contract_ref
            JOIN waje_runtime.analysis_contracts ac
              ON ac.analysis_contract_id = qc.analysis_contract_id
            JOIN waje_runtime.analysis_runs ar ON ar.run_id = c.run_id
            WHERE {where}
            """,
            {"ref": str(ref)},
        )
        if row is None:
            return None
        row = _row_mapping(
            row,
            (
                "record_ref",
                "report_ref",
                "report_digest",
                "result_ref",
                "query_contract_ref",
                "payload",
                "authority_run_id",
                "stored_completeness_status",
                "stored_analysis_readiness",
                "run_id",
                "run_query_contract_id",
                "run_result_ref",
                "run_completeness_report_ref",
                "contract_run_id",
                "contract_query_id",
                "analysis_contract_id",
                "stored_contract_signature",
                "contract_payload",
                "analysis_run_id",
                "stored_analysis_signature",
                "analysis_payload",
                "analysis_run_id_actual",
            ),
        )
        record = _record_from_row("completeness", row)
        _require_columns(
            record,
            row,
            {
                "record_ref": "record_ref",
                "report_ref": "report_ref",
                "report_digest": "report_digest",
                "result_ref": "result_ref",
                "query_contract_ref": "query_contract_ref",
            },
            "completeness_record_column_mismatch",
        )
        _validate_completeness_join(record, row)
        return record

    def resolve_capability_binding(
        self, binding_ref: str
    ) -> CapabilityBindingRecord | None:
        row = self._one(
            """
            SELECT b.record_ref, b.binding_digest, b.capability_id,
                   b.claim_strength_taxonomy_version,
                   b.maximum_claim_strength_rank, b.payload,
                   b.run_id AS authority_run_id,
                   b.analysis_contract_id AS stored_analysis_contract_id,
                   ac.run_id AS analysis_run_id,
                   ac.contract_signature AS stored_analysis_signature,
                   ac.payload AS analysis_payload,
                   ar.run_id AS analysis_run_id_actual
            FROM waje_runtime.capability_binding_authority b
            JOIN waje_runtime.analysis_contracts ac
              ON ac.analysis_contract_id = b.analysis_contract_id
            JOIN waje_runtime.analysis_runs ar ON ar.run_id = b.run_id
            WHERE b.record_ref = %(ref)s
            """,
            {"ref": str(binding_ref)},
        )
        if row is None:
            return None
        row = _row_mapping(
            row,
            (
                "record_ref",
                "binding_digest",
                "capability_id",
                "claim_strength_taxonomy_version",
                "maximum_claim_strength_rank",
                "payload",
                "authority_run_id",
                "stored_analysis_contract_id",
                "analysis_run_id",
                "stored_analysis_signature",
                "analysis_payload",
                "analysis_run_id_actual",
            ),
        )
        record = _record_from_row("capability_binding", row)
        _require_columns(
            record,
            row,
            {
                "record_ref": "record_ref",
                "binding_digest": "binding_digest",
                "capability_id": "capability_id",
                "claim_strength_taxonomy_version": "claim_strength_taxonomy_version",
                "maximum_claim_strength_rank": "maximum_claim_strength_rank",
            },
            "capability_binding_record_column_mismatch",
        )
        _validate_binding_join(record, row)
        return record

    def _one(self, statement: str, params: Mapping[str, Any]) -> Any:
        return self.connection.execute(statement, dict(params)).fetchone()


def _record_from_row(kind: str, row: Any) -> Any:
    payload = _json_value(_field(row, "payload"))
    if (
        not isinstance(payload, Mapping)
        or set(payload) != {"kind", "record"}
        or payload.get("kind") != kind
    ):
        if isinstance(payload, Mapping) and payload.get("kind") == kind:
            raise EvidenceIntegrityError(f"{kind}_record_payload_keys_invalid")
        raise EvidenceIntegrityError(f"{kind}_record_kind_mismatch")
    record_payload = payload.get("record")
    if not isinstance(record_payload, Mapping):
        raise EvidenceIntegrityError(f"{kind}_record_payload_invalid")
    try:
        record = _record_from_payload(kind, record_payload)
    except EvidenceIntegrityError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise EvidenceIntegrityError(f"{kind}_record_payload_invalid") from exc
    errors = runtime_evidence_record_integrity_errors(record)
    if errors:
        raise EvidenceIntegrityError(errors[0])
    return record


def _record_from_payload(kind: str, payload: Mapping[str, Any]) -> Any:
    data = canonical_thaw(payload)
    record_types = {
        "snapshot": SnapshotRecord,
        "rows": RowsRecord,
        "query_execution": QueryExecutionRecord,
        "completeness": CompletenessRecord,
        "capability_binding": CapabilityBindingRecord,
    }
    expected_fields = {field.name for field in fields(record_types[kind])}
    if set(data) != expected_fields:
        raise EvidenceIntegrityError(f"{kind}_record_fields_invalid")
    if kind == "snapshot":
        snapshot_payload = dict(data["snapshot"])
        for name in (
            "schema_fields",
            "source_checksums",
            "date_range",
            "no_data_partitions",
            "no_data_partition_windows",
        ):
            snapshot_payload[name] = tuple(snapshot_payload.get(name) or ())
        return SnapshotRecord(
            record_ref=str(data["record_ref"]),
            record_digest=str(data["record_digest"]),
            snapshot_ref=str(data["snapshot_ref"]),
            payload=_deep_freeze(data["payload"]),
            payload_digest=str(data["payload_digest"]),
            snapshot=DatasetSnapshot(**snapshot_payload),
        )
    if kind == "rows":
        return RowsRecord(
            record_ref=str(data["record_ref"]),
            record_digest=str(data["record_digest"]),
            rows_ref=str(data["rows_ref"]),
            rows_content_hash=str(data["rows_content_hash"]),
            row_count=int(data["row_count"]),
            unique_key_fields=tuple(data["unique_key_fields"]),
            storage_ref=str(data["storage_ref"]),
            metadata_payload=_deep_freeze(data["metadata_payload"]),
        )
    if kind == "query_execution":
        if set(data["query_contract"]) != {
            field.name for field in fields(QueryContract)
        }:
            raise EvidenceIntegrityError("query_contract_payload_fields_invalid")
        contract = query_contract_from_dict(data["query_contract"])
        contract = replace(
            contract,
            filters=tuple(_deep_freeze(item) for item in contract.filters),
            query_parameters=_deep_freeze(contract.query_parameters),
        )
        return QueryExecutionRecord(
            record_ref=str(data["record_ref"]),
            record_digest=str(data["record_digest"]),
            record_payload=_deep_freeze(data["record_payload"]),
            query_contract_ref=str(data["query_contract_ref"]),
            contract_signature=str(data["contract_signature"]),
            query_contract=_deep_freeze(data["query_contract"]),
            contract=contract,
            query_hash=str(data["query_hash"]),
            execution_attempt_ref=str(data["execution_attempt_ref"]),
            result_ref=str(data["result_ref"]),
            rows_ref=str(data["rows_ref"]),
            completeness_report_ref=str(data["completeness_report_ref"]),
            execution_status=str(data["execution_status"]),
            row_count=int(data["row_count"]),
            rows_content_hash=str(data["rows_content_hash"]),
            source_snapshot_refs=tuple(data["source_snapshot_refs"]),
            source_snapshot_record_refs=tuple(data["source_snapshot_record_refs"]),
            source_snapshot_record_digests=tuple(
                data["source_snapshot_record_digests"]
            ),
            result_payload=_deep_freeze(data["result_payload"]),
        )
    if kind == "completeness":
        return CompletenessRecord(
            record_ref=str(data["record_ref"]),
            report_ref=str(data["report_ref"]),
            query_contract_ref=str(data["query_contract_ref"]),
            result_ref=str(data["result_ref"]),
            report_digest=str(data["report_digest"]),
            report_payload=_deep_freeze(data["report_payload"]),
        )
    if kind == "capability_binding":
        tuple_fields = {
            field.name
            for field in fields(CapabilityBindingRecord)
            if field.name.endswith(
                ("_refs", "_digests", "_hashes", "_types", "_statuses")
            )
        }
        tuple_fields.update({"query_contract_refs", "result_refs", "rows_refs"})
        kwargs = {}
        for field in fields(CapabilityBindingRecord):
            value = data[field.name]
            kwargs[field.name] = tuple(value) if field.name in tuple_fields else value
        kwargs["plan_payload"] = _deep_freeze(kwargs["plan_payload"])
        kwargs["binding_payload"] = _deep_freeze(kwargs["binding_payload"])
        return CapabilityBindingRecord(**kwargs)
    raise EvidenceIntegrityError(f"runtime_authority_record_kind_invalid:{kind}")


def _require_columns(
    record: Any,
    row: Any,
    columns: Mapping[str, str],
    code: str,
) -> None:
    for attribute, column in columns.items():
        expected = canonical_value(getattr(record, attribute))
        actual = _field(row, column)
        if isinstance(expected, (list, dict)):
            actual = _json_value(actual)
        if actual != expected:
            raise EvidenceIntegrityError(code)


def _validate_query_execution_join(
    record: QueryExecutionRecord,
    row: Mapping[str, Any],
) -> None:
    run_ids = {
        str(row.get(name) or "")
        for name in (
            "authority_run_id",
            "run_id",
            "contract_run_id",
            "analysis_run_id",
            "analysis_run_id_actual",
        )
    }
    if len(run_ids) != 1 or "" in run_ids:
        raise EvidenceIntegrityError("query_execution_run_membership_mismatch")
    scalar_pairs = (
        (record.result_ref, row.get("run_result_ref")),
        (record.query_contract_ref, row.get("run_query_contract_id")),
        (record.query_contract_ref, row.get("contract_query_id")),
        (record.execution_status, row.get("execution_status")),
        (record.query_hash, row.get("run_query_hash")),
        (record.rows_ref, row.get("run_rows_ref")),
        (record.completeness_report_ref, row.get("run_completeness_report_ref")),
        (record.contract.analysis_contract_ref, row.get("analysis_contract_id")),
        (record.contract_signature, row.get("stored_contract_signature")),
    )
    if any(expected != actual for expected, actual in scalar_pairs):
        raise EvidenceIntegrityError("query_execution_join_mirror_mismatch")
    if canonical_value(_json_value(row.get("result_payload"))) != canonical_value(
        record.result_payload
    ):
        raise EvidenceIntegrityError("query_execution_result_payload_mismatch")
    if canonical_value(_json_value(row.get("contract_payload"))) != canonical_value(
        record.query_contract
    ):
        raise EvidenceIntegrityError("query_execution_contract_payload_mismatch")
    analysis_payload = _json_value(row.get("analysis_payload"))
    analysis_signature = str(row.get("stored_analysis_signature") or "")
    analysis = _analysis_contract_from_envelope(
        analysis_payload,
        stored_signature=analysis_signature,
        code="query_execution_analysis_contract_mismatch",
    )
    if analysis.analysis_contract_id != record.contract.analysis_contract_ref:
        raise EvidenceIntegrityError("query_execution_analysis_contract_mismatch")
    try:
        _validate_query_contract_analysis_semantics(analysis, record.contract)
    except EvidenceIntegrityError as exc:
        raise EvidenceIntegrityError(
            "query_execution_analysis_contract_mismatch"
        ) from exc


def _validate_completeness_join(
    record: CompletenessRecord,
    row: Mapping[str, Any],
) -> None:
    run_ids = {
        str(row.get(name) or "")
        for name in (
            "authority_run_id",
            "run_id",
            "contract_run_id",
            "analysis_run_id",
            "analysis_run_id_actual",
        )
    }
    if len(run_ids) != 1 or "" in run_ids:
        raise EvidenceIntegrityError("completeness_run_membership_mismatch")
    report = canonical_value(record.report_payload)
    scalar_pairs = (
        (record.result_ref, row.get("run_result_ref")),
        (record.query_contract_ref, row.get("run_query_contract_id")),
        (record.query_contract_ref, row.get("contract_query_id")),
        (record.report_ref, row.get("run_completeness_report_ref")),
        (report.get("completeness_status"), row.get("stored_completeness_status")),
        (report.get("analysis_readiness"), row.get("stored_analysis_readiness")),
    )
    if any(expected != actual for expected, actual in scalar_pairs):
        raise EvidenceIntegrityError("completeness_join_mirror_mismatch")
    contract_payload = _json_value(row.get("contract_payload"))
    if not isinstance(contract_payload, Mapping):
        raise EvidenceIntegrityError("completeness_contract_payload_invalid")
    try:
        contract = query_contract_from_dict(contract_payload)
    except (TypeError, ValueError) as exc:
        raise EvidenceIntegrityError("completeness_contract_payload_invalid") from exc
    if (
        contract.query_contract_id != record.query_contract_ref
        or contract.contract_signature != row.get("stored_contract_signature")
        or query_contract_signature(contract) != contract.contract_signature
        or contract.analysis_contract_ref != row.get("analysis_contract_id")
    ):
        raise EvidenceIntegrityError("completeness_contract_mismatch")
    analysis_payload = _json_value(row.get("analysis_payload"))
    analysis_signature = str(row.get("stored_analysis_signature") or "")
    analysis = _analysis_contract_from_envelope(
        analysis_payload,
        stored_signature=analysis_signature,
        code="completeness_analysis_contract_mismatch",
    )
    if analysis.analysis_contract_id != contract.analysis_contract_ref:
        raise EvidenceIntegrityError("completeness_analysis_contract_mismatch")
    try:
        _validate_query_contract_analysis_semantics(analysis, contract)
    except EvidenceIntegrityError as exc:
        raise EvidenceIntegrityError("completeness_analysis_contract_mismatch") from exc


def _validate_binding_join(
    record: CapabilityBindingRecord,
    row: Mapping[str, Any],
) -> None:
    run_ids = {
        str(row.get(name) or "")
        for name in ("authority_run_id", "analysis_run_id", "analysis_run_id_actual")
    }
    if len(run_ids) != 1 or "" in run_ids:
        raise EvidenceIntegrityError("capability_binding_run_membership_mismatch")
    if record.analysis_contract_ref != row.get("stored_analysis_contract_id"):
        raise EvidenceIntegrityError("capability_binding_analysis_contract_mismatch")
    analysis_payload = _json_value(row.get("analysis_payload"))
    analysis_signature = str(row.get("stored_analysis_signature") or "")
    analysis = _analysis_contract_from_envelope(
        analysis_payload,
        stored_signature=analysis_signature,
        code="capability_binding_analysis_contract_mismatch",
    )
    if analysis.analysis_contract_id != record.analysis_contract_ref:
        raise EvidenceIntegrityError("capability_binding_analysis_contract_mismatch")
    try:
        _validate_capability_binding_analysis_closure(
            analysis,
            record,
            None,
        )
    except EvidenceIntegrityError as exc:
        raise EvidenceIntegrityError(
            "capability_binding_analysis_contract_mismatch"
        ) from exc


def _field(row: Any, name: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(name)
    try:
        return getattr(row, name)
    except AttributeError as exc:
        raise EvidenceIntegrityError(
            f"runtime_authority_row_column_missing:{name}"
        ) from exc


def _row_mapping(row: Any, names: Sequence[str]) -> Mapping[str, Any]:
    if isinstance(row, Mapping):
        return row
    if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
        raise EvidenceIntegrityError("runtime_authority_row_invalid")
    if len(row) != len(names):
        raise EvidenceIntegrityError("runtime_authority_row_column_count_mismatch")
    return dict(zip(names, row))


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return canonical_thaw(value)


def _authority_mapping(value: Any, kind: str) -> Mapping[str, Any]:
    payload = _json_value(value)
    if not isinstance(payload, Mapping):
        raise EvidenceIntegrityError(f"{kind}_payload_invalid")
    return canonical_value(payload)
