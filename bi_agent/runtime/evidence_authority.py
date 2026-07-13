from __future__ import annotations

from dataclasses import asdict, dataclass, fields, is_dataclass, replace
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import math
import os
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

from bi_agent.runtime.analysis_contracts import (
    CapabilityExecutionPlan,
    CompletenessReport,
    QueryContract,
    QueryResultEnvelope,
    query_contract_signature,
)
from bi_agent.runtime.canonical_values import canonical_thaw
from bi_agent.runtime.dataset_catalog import DatasetSnapshot
from bi_agent.runtime.query_audit import query_audit_refs


class EvidenceIntegrityError(ValueError):
    pass


@dataclass(frozen=True)
class SnapshotRecord:
    record_ref: str
    record_digest: str
    snapshot_ref: str
    payload: Mapping[str, Any]
    payload_digest: str
    snapshot: DatasetSnapshot


@dataclass(frozen=True)
class RowsRecord:
    record_ref: str
    record_digest: str
    rows_ref: str
    rows_content_hash: str
    row_count: int
    unique_key_fields: tuple[str, ...]
    storage_ref: str
    metadata_payload: Mapping[str, Any]


@dataclass(frozen=True)
class QueryExecutionRecord:
    record_ref: str
    record_digest: str
    record_payload: Mapping[str, Any]
    query_contract_ref: str
    contract_signature: str
    query_contract: Mapping[str, Any]
    contract: QueryContract
    query_hash: str
    execution_attempt_ref: str
    result_ref: str
    rows_ref: str
    completeness_report_ref: str
    execution_status: str
    row_count: int
    rows_content_hash: str
    source_snapshot_refs: tuple[str, ...]
    source_snapshot_record_refs: tuple[str, ...]
    source_snapshot_record_digests: tuple[str, ...]
    result_payload: Mapping[str, Any]


@dataclass(frozen=True)
class CompletenessRecord:
    record_ref: str
    report_ref: str
    query_contract_ref: str
    result_ref: str
    report_digest: str
    report_payload: Mapping[str, Any]


@dataclass(frozen=True)
class CapabilityBindingRecord:
    record_ref: str
    binding_digest: str
    capability_id: str
    capability_contract_version: str
    capability_contract_signature: str
    analysis_contract_ref: str
    status: str
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
    plan_payload: Mapping[str, Any]
    binding_payload: Mapping[str, Any]


@runtime_checkable
class RuntimeEvidenceResolver(Protocol):
    def resolve_query_execution(self, result_ref: str) -> QueryExecutionRecord | None: ...

    def resolve_query_execution_record(
        self,
        record_ref: str,
    ) -> QueryExecutionRecord | None: ...

    def resolve_rows(self, rows_ref: str) -> RowsRecord | None: ...

    def resolve_rows_record(self, record_ref: str) -> RowsRecord | None: ...

    def resolve_snapshot(self, snapshot_ref: str) -> SnapshotRecord | None: ...

    def resolve_completeness(self, record_ref: str) -> CompletenessRecord | None: ...

    def resolve_capability_binding(
        self,
        binding_ref: str,
    ) -> CapabilityBindingRecord | None: ...


@runtime_checkable
class RowsPayloadLoader(Protocol):
    def load_rows(self, storage_ref: str) -> tuple[Mapping[str, Any], ...] | None: ...


@runtime_checkable
class RuntimeEvidenceWriter(Protocol):
    def record_query_execution(
        self,
        contract: QueryContract,
        result: QueryResultEnvelope,
        snapshots: Mapping[str, DatasetSnapshot],
    ) -> QueryExecutionRecord: ...

    def record_completeness(self, report: CompletenessReport) -> CompletenessRecord: ...

    def record_capability_binding(
        self,
        plan: CapabilityExecutionPlan,
        binding_payload: Mapping[str, Any],
    ) -> CapabilityBindingRecord: ...


class InMemoryRowsPayloadLoader:
    def __init__(self) -> None:
        self.__rows: dict[str, tuple[Mapping[str, Any], ...]] = {}

    def load_rows(self, storage_ref: str) -> tuple[Mapping[str, Any], ...] | None:
        rows = self.__rows.get(str(storage_ref))
        if rows is None:
            return None
        return tuple(dict(row) for row in rows)

    def _typed_store(self) -> Callable[[str, Sequence[Mapping[str, Any]]], None]:
        rows_store = self.__rows

        def store(storage_ref: str, rows: Sequence[Mapping[str, Any]]) -> None:
            if not storage_ref or any(not isinstance(row, Mapping) for row in rows):
                raise EvidenceIntegrityError("rows_payload_invalid")
            if canonical_rows_storage_ref(rows) != storage_ref:
                raise EvidenceIntegrityError("rows_storage_ref_content_mismatch")
            frozen = tuple(_deep_freeze(dict(row)) for row in rows)
            current = rows_store.get(storage_ref)
            if current is not None and current != frozen:
                raise EvidenceIntegrityError(
                    f"authority_ref_collision:rows_payload:{storage_ref}"
                )
            rows_store[storage_ref] = frozen

        return store


class RuntimeEvidenceAuthority:
    def __init__(
        self,
        *,
        rows_loader: InMemoryRowsPayloadLoader | None = None,
        runtime_registry: Any = None,
    ) -> None:
        self.__queries: dict[str, QueryExecutionRecord] = {}
        self.__query_records: dict[str, QueryExecutionRecord] = {}
        self.__rows: dict[str, RowsRecord] = {}
        self.__rows_records: dict[str, RowsRecord] = {}
        self.__snapshots: dict[str, SnapshotRecord] = {}
        self.__completeness: dict[str, CompletenessRecord] = {}
        self.__completeness_aliases: dict[str, CompletenessRecord] = {}
        self.__bindings: dict[str, CapabilityBindingRecord] = {}
        self.rows_loader = rows_loader or InMemoryRowsPayloadLoader()
        self.runtime_registry = runtime_registry

        def put_typed(kind: str, ref: str, record: Any) -> None:
            expected = {
                "query": QueryExecutionRecord,
                "rows": RowsRecord,
                "snapshot": SnapshotRecord,
                "completeness": CompletenessRecord,
                "binding": CapabilityBindingRecord,
            }
            record_type = expected.get(kind)
            if record_type is None or type(record) is not record_type:
                raise EvidenceIntegrityError(f"authority_record_type_invalid:{kind}")
            _validate_record_identity(kind, ref, record)
            stores = {
                "query": self.__queries,
                "rows": self.__rows,
                "snapshot": self.__snapshots,
                "completeness": self.__completeness,
                "binding": self.__bindings,
            }
            current = stores[kind].get(ref)
            if current is not None and current != record:
                raise EvidenceIntegrityError(f"authority_ref_collision:{kind}:{ref}")
            stores[kind][ref] = record
            immutable_stores = {
                "query": self.__query_records,
                "rows": self.__rows_records,
            }
            immutable_store = immutable_stores.get(kind)
            if immutable_store is not None:
                immutable_ref = record.record_ref
                immutable_current = immutable_store.get(immutable_ref)
                if immutable_current is not None and immutable_current != record:
                    raise EvidenceIntegrityError(
                        f"authority_ref_collision:{kind}_record:{immutable_ref}"
                    )
                immutable_store[immutable_ref] = record

        def link_latest(report_ref: str, record: CompletenessRecord) -> None:
            if type(record) is not CompletenessRecord or record.report_ref != report_ref:
                raise EvidenceIntegrityError("completeness_alias_invalid")
            self.__completeness_aliases[report_ref] = record

        self.__writer = _InMemoryRuntimeEvidenceWriter(
            resolver=self,
            put_typed=put_typed,
            link_latest=link_latest,
            store_rows=self.rows_loader._typed_store(),
        )

    def resolve_query_execution(self, result_ref: str) -> QueryExecutionRecord | None:
        return self.__queries.get(str(result_ref))

    def resolve_query_execution_record(
        self,
        record_ref: str,
    ) -> QueryExecutionRecord | None:
        return self.__query_records.get(str(record_ref))

    def resolve_rows(self, rows_ref: str) -> RowsRecord | None:
        return self.__rows.get(str(rows_ref))

    def resolve_rows_record(self, record_ref: str) -> RowsRecord | None:
        return self.__rows_records.get(str(record_ref))

    def resolve_snapshot(self, snapshot_ref: str) -> SnapshotRecord | None:
        return self.__snapshots.get(str(snapshot_ref))

    def resolve_completeness(self, report_ref: str) -> CompletenessRecord | None:
        return self.__completeness.get(str(report_ref))

    def resolve_latest_completeness(
        self,
        report_ref: str,
    ) -> CompletenessRecord | None:
        return self.__completeness_aliases.get(str(report_ref))

    def resolve_capability_binding(
        self,
        binding_ref: str,
    ) -> CapabilityBindingRecord | None:
        return self.__bindings.get(str(binding_ref))

    def _runtime_writer(self) -> RuntimeEvidenceWriter:
        return self.__writer


class _InMemoryRuntimeEvidenceWriter:
    def __init__(
        self,
        *,
        resolver: RuntimeEvidenceResolver,
        put_typed: Callable[[str, str, Any], None],
        link_latest: Callable[[str, CompletenessRecord], None],
        store_rows: Callable[[str, Sequence[Mapping[str, Any]]], None],
    ) -> None:
        self.__resolver = resolver
        self.__put_typed = put_typed
        self.__link_latest = link_latest
        self.__store_rows = store_rows

    def record_query_execution(
        self,
        contract: QueryContract,
        result: QueryResultEnvelope,
        snapshots: Mapping[str, DatasetSnapshot],
    ) -> QueryExecutionRecord:
        if (
            type(contract) is not QueryContract
            or type(result) is not QueryResultEnvelope
            or not isinstance(snapshots, Mapping)
        ):
            raise EvidenceIntegrityError("query_execution_write_type_invalid")
        return _write_query_execution(
            self.__put_typed,
            self.__store_rows,
            contract,
            result,
            snapshots,
        )

    def record_completeness(self, report: CompletenessReport) -> CompletenessRecord:
        if type(report) is not CompletenessReport:
            raise EvidenceIntegrityError("completeness_write_type_invalid")
        return _write_completeness(
            self.__resolver,
            self.__put_typed,
            self.__link_latest,
            report,
        )

    def record_capability_binding(
        self,
        plan: CapabilityExecutionPlan,
        binding_payload: Mapping[str, Any],
    ) -> CapabilityBindingRecord:
        if type(plan) is not CapabilityExecutionPlan or not isinstance(
            binding_payload,
            Mapping,
        ):
            raise EvidenceIntegrityError("capability_binding_write_type_invalid")
        return _write_capability_binding(
            self.__put_typed,
            plan,
            binding_payload,
        )


def legacy_fixture_enabled(run_mode: str) -> bool:
    return bool(
        run_mode == "fixture"
        and os.environ.get("WAJE_ALLOW_LEGACY_FIXTURES") == "1"
        and os.environ.get("WAJE_RUNTIME_ENV") in {"test", "development"}
    )


def canonical_digest(value: Any) -> str:
    try:
        payload = json.dumps(
            _canonical_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise EvidenceIntegrityError(f"canonical_json_invalid:{exc}") from exc
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def canonical_value(value: Any) -> Any:
    return _canonical_value(value)


def canonical_rows_hash(
    rows: Sequence[Mapping[str, Any]],
    unique_key_fields: Sequence[str],
) -> str:
    return canonical_digest(
        _canonical_ordered_rows(rows, unique_key_fields)
    )


def _canonical_ordered_rows(
    rows: Sequence[Mapping[str, Any]],
    unique_key_fields: Sequence[str],
) -> tuple[Mapping[str, Any], ...]:
    normalized = []
    keys_seen = set()
    key_fields = tuple(str(field) for field in unique_key_fields if field)
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise EvidenceIntegrityError(f"row_not_mapping:{index}")
        canonical_row = _canonical_value(row)
        if key_fields:
            missing = tuple(field for field in key_fields if field not in row)
            if missing:
                raise EvidenceIntegrityError(
                    f"unique_key_fields_missing:{index}:{','.join(missing)}"
                )
            raw_key = tuple(row[field] for field in key_fields)
            if any(not _scalar_key(value) for value in raw_key):
                raise EvidenceIntegrityError(f"unique_key_not_scalar:{index}")
            key = canonical_digest(raw_key)
            if key in keys_seen:
                raise EvidenceIntegrityError(f"duplicate_unique_key:{index}")
            keys_seen.add(key)
            sort_key = key
        else:
            sort_key = canonical_digest(canonical_row)
        normalized.append((sort_key, canonical_row))
    normalized.sort(key=lambda item: item[0])
    return tuple(row for _, row in normalized)


def canonical_result_rows(
    rows: Sequence[Mapping[str, Any]],
    unique_key_fields: Sequence[str],
) -> tuple[Mapping[str, Any], ...]:
    try:
        return _canonical_ordered_rows(rows, unique_key_fields)
    except EvidenceIntegrityError as exc:
        if not str(exc).startswith("unique_key_fields_missing:"):
            raise
        return _canonical_ordered_rows(rows, ())


def canonical_result_rows_hash(
    rows: Sequence[Mapping[str, Any]],
    unique_key_fields: Sequence[str],
) -> str:
    return canonical_digest(canonical_result_rows(rows, unique_key_fields))


def canonical_rows_storage_ref(rows: Sequence[Mapping[str, Any]]) -> str:
    if any(not isinstance(row, Mapping) for row in rows):
        raise EvidenceIntegrityError("rows_payload_invalid")
    digest = canonical_digest(tuple(_canonical_value(row) for row in rows))
    return f"rows-storage:sha256:{digest}"


def snapshot_authority_record(snapshot: DatasetSnapshot) -> SnapshotRecord:
    if type(snapshot) is not DatasetSnapshot:
        raise EvidenceIntegrityError("snapshot_record_write_type_invalid")
    payload = _canonical_value(asdict(snapshot))
    digest = canonical_digest(payload)
    return SnapshotRecord(
        record_ref=f"snapshot-record:{snapshot.snapshot_ref}:{digest}",
        record_digest=digest,
        snapshot_ref=snapshot.snapshot_ref,
        payload=_deep_freeze(payload),
        payload_digest=digest,
        snapshot=snapshot,
    )


def _content_addressed_storage_ref(value: Any) -> bool:
    text = str(value or "")
    prefix = "rows-storage:sha256:"
    digest = text[len(prefix) :] if text.startswith(prefix) else ""
    return len(digest) == 64 and all(char in "0123456789abcdef" for char in digest)


def _record_query_execution(
    writer: RuntimeEvidenceWriter | RuntimeEvidenceAuthority,
    contract: QueryContract,
    result: QueryResultEnvelope,
    snapshots: Mapping[str, DatasetSnapshot],
) -> QueryExecutionRecord:
    return _as_writer(writer).record_query_execution(contract, result, snapshots)


def _write_query_execution(
    put_typed: Callable[[str, str, Any], None],
    store_rows: Callable[[str, Sequence[Mapping[str, Any]]], None],
    contract: QueryContract,
    result: QueryResultEnvelope,
    snapshots: Mapping[str, DatasetSnapshot],
) -> QueryExecutionRecord:
    if (
        query_contract_signature(contract) != contract.contract_signature
        and result.execution_status == "succeeded"
    ):
        raise EvidenceIntegrityError("query_contract_signature_mismatch")
    if tuple(result.source_snapshot_refs) != tuple(contract.dataset_snapshot_refs):
        raise EvidenceIntegrityError("query_snapshot_refs_mismatch")
    if set(snapshots) != set(contract.dataset_snapshot_refs):
        raise EvidenceIntegrityError("query_snapshot_payloads_mismatch")
    ordered_rows = canonical_result_rows(
        result.rows,
        contract.result_shape.unique_key,
    )
    rows_hash = canonical_digest(ordered_rows)
    expected = query_audit_refs(
        result.query_hash,
        contract.contract_signature,
        contract.dataset_snapshot_refs,
        query_contract_ref=contract.query_contract_id,
        execution_attempt_ref=result.execution_attempt_ref,
        rows_content_hash=rows_hash if result.execution_status == "succeeded" else "",
    )
    if (
        result.query_contract_ref != contract.query_contract_id
        or result.result_ref != expected.result_ref
        or result.rows_ref != expected.rows_ref
        or result.completeness_report_ref != expected.completeness_report_ref
        or result.row_count != len(result.rows)
    ):
        raise EvidenceIntegrityError("query_execution_provenance_mismatch")
    snapshot_records = []
    for snapshot_ref in result.source_snapshot_refs:
        snapshot = snapshots[snapshot_ref]
        if snapshot.snapshot_ref != snapshot_ref:
            raise EvidenceIntegrityError("snapshot_mapping_ref_mismatch")
        snapshot_record = snapshot_authority_record(snapshot)
        put_typed(
            "snapshot",
            snapshot_ref,
            snapshot_record,
        )
        snapshot_records.append(snapshot_record)
    storage_ref = canonical_rows_storage_ref(ordered_rows)
    rows_metadata = {
        "rows_ref": result.rows_ref,
        "rows_content_hash": rows_hash,
        "row_count": len(result.rows),
        "unique_key_fields": tuple(contract.result_shape.unique_key),
        "storage_ref": storage_ref,
    }
    rows_metadata_digest = canonical_digest(rows_metadata)
    rows_record = RowsRecord(
        record_ref=(
            f"rows-record:{result.rows_ref}:{rows_metadata_digest}"
        ),
        record_digest=rows_metadata_digest,
        rows_ref=result.rows_ref,
        rows_content_hash=rows_hash,
        row_count=len(result.rows),
        unique_key_fields=tuple(contract.result_shape.unique_key),
        storage_ref=storage_ref,
        metadata_payload=_deep_freeze(_canonical_value(rows_metadata)),
    )
    store_rows(storage_ref, ordered_rows)
    put_typed("rows", result.rows_ref, rows_record)
    result_payload = _canonical_value(result.to_dict())
    result_payload.pop("rows", None)
    if result.execution_status == "succeeded":
        result_payload["observed_windows"] = list(
            _ordered_observed_windows(ordered_rows)
        )
    record_payload = {
        "query_contract": _canonical_value(contract.to_dict()),
        "result": result_payload,
        "rows_content_hash": rows_hash,
        "source_snapshot_record_refs": tuple(
            item.record_ref for item in snapshot_records
        ),
        "source_snapshot_record_digests": tuple(
            item.record_digest for item in snapshot_records
        ),
    }
    record_digest = canonical_digest(record_payload)
    record = QueryExecutionRecord(
        record_ref=f"query-execution:{result.result_ref}:{record_digest}",
        record_digest=record_digest,
        record_payload=_deep_freeze(_canonical_value(record_payload)),
        query_contract_ref=contract.query_contract_id,
        contract_signature=contract.contract_signature,
        query_contract=_deep_freeze(_canonical_value(contract.to_dict())),
        contract=replace(
            contract,
            filters=tuple(_deep_freeze(item) for item in contract.filters),
            query_parameters=_deep_freeze(contract.query_parameters),
        ),
        query_hash=result.query_hash,
        execution_attempt_ref=result.execution_attempt_ref,
        result_ref=result.result_ref,
        rows_ref=result.rows_ref,
        completeness_report_ref=result.completeness_report_ref,
        execution_status=result.execution_status,
        row_count=result.row_count,
        rows_content_hash=rows_hash,
        source_snapshot_refs=tuple(result.source_snapshot_refs),
        source_snapshot_record_refs=tuple(
            item.record_ref for item in snapshot_records
        ),
        source_snapshot_record_digests=tuple(
            item.record_digest for item in snapshot_records
        ),
        result_payload=_deep_freeze(result_payload),
    )
    put_typed("query", result.result_ref, record)
    return record


def _record_completeness(
    writer: RuntimeEvidenceWriter | RuntimeEvidenceAuthority,
    report: CompletenessReport,
) -> CompletenessRecord:
    return _as_writer(writer).record_completeness(report)


def _write_completeness(
    resolver: RuntimeEvidenceResolver,
    put_typed: Callable[[str, str, Any], None],
    link_latest: Callable[[str, CompletenessRecord], None],
    report: CompletenessReport,
) -> CompletenessRecord:
    query_record = resolver.resolve_query_execution(report.result_ref)
    if query_record is None:
        raise EvidenceIntegrityError("completeness_query_execution_missing")
    if (
        report.query_contract_ref != query_record.query_contract_ref
        or report.report_ref != query_record.completeness_report_ref
    ):
        raise EvidenceIntegrityError("completeness_provenance_mismatch")
    payload = _canonical_value(report.to_dict())
    coverage = payload.get("coverage_summary")
    if isinstance(coverage, Mapping) and "observed_windows" in coverage:
        payload["coverage_summary"] = {
            **coverage,
            "observed_windows": list(
                query_record.result_payload.get("observed_windows") or ()
            ),
        }
    digest = canonical_digest(payload)
    record = CompletenessRecord(
        record_ref=f"completeness-record:{report.report_ref}:{digest}",
        report_ref=report.report_ref,
        query_contract_ref=report.query_contract_ref,
        result_ref=report.result_ref,
        report_digest=digest,
        report_payload=_deep_freeze(payload),
    )
    put_typed("completeness", record.record_ref, record)
    link_latest(report.report_ref, record)
    return record


def _ordered_observed_windows(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            str(row.get("window_id") or "")
            for row in rows
            if row.get("window_id") not in (None, "")
        )
    )


def _record_capability_binding(
    writer: RuntimeEvidenceWriter | RuntimeEvidenceAuthority,
    plan: CapabilityExecutionPlan,
    binding_payload: Mapping[str, Any],
) -> CapabilityBindingRecord:
    return _as_writer(writer).record_capability_binding(plan, binding_payload)


def _write_capability_binding(
    put_typed: Callable[[str, str, Any], None],
    plan: CapabilityExecutionPlan,
    binding_payload: Mapping[str, Any],
) -> CapabilityBindingRecord:
    payload = _canonical_value(binding_payload)
    digest = canonical_digest({"plan": asdict(plan), "binding": payload})
    ref = f"capability-binding:{plan.capability_id}:{digest}"
    record = CapabilityBindingRecord(
        record_ref=ref,
        binding_digest=digest,
        capability_id=plan.capability_id,
        capability_contract_version=plan.capability_contract_version,
        capability_contract_signature=plan.capability_contract_signature,
        analysis_contract_ref=plan.analysis_contract_ref,
        status=str(payload.get("status") or ""),
        query_contract_refs=tuple(payload.get("query_contract_refs") or ()),
        result_refs=tuple(payload.get("result_refs") or ()),
        query_execution_record_refs=tuple(
            payload.get("query_execution_record_refs") or ()
        ),
        query_execution_record_digests=tuple(
            payload.get("query_execution_record_digests") or ()
        ),
        rows_refs=tuple(payload.get("rows_refs") or ()),
        rows_metadata_record_refs=tuple(
            payload.get("rows_metadata_record_refs") or ()
        ),
        rows_metadata_record_digests=tuple(
            payload.get("rows_metadata_record_digests") or ()
        ),
        rows_content_hashes=tuple(payload.get("rows_content_hashes") or ()),
        completeness_report_refs=tuple(payload.get("completeness_report_refs") or ()),
        completeness_record_refs=tuple(payload.get("completeness_record_refs") or ()),
        completeness_record_digests=tuple(
            payload.get("completeness_record_digests") or ()
        ),
        source_snapshot_refs=tuple(payload.get("source_snapshot_refs") or ()),
        validation_query_contract_refs=tuple(
            payload.get("validation_query_contract_refs") or ()
        ),
        validation_result_refs=tuple(payload.get("validation_result_refs") or ()),
        validation_query_execution_record_refs=tuple(
            payload.get("validation_query_execution_record_refs") or ()
        ),
        validation_query_execution_record_digests=tuple(
            payload.get("validation_query_execution_record_digests") or ()
        ),
        validation_rows_refs=tuple(payload.get("validation_rows_refs") or ()),
        validation_rows_metadata_record_refs=tuple(
            payload.get("validation_rows_metadata_record_refs") or ()
        ),
        validation_rows_metadata_record_digests=tuple(
            payload.get("validation_rows_metadata_record_digests") or ()
        ),
        validation_rows_content_hashes=tuple(
            payload.get("validation_rows_content_hashes") or ()
        ),
        validation_completeness_report_refs=tuple(
            payload.get("validation_completeness_report_refs") or ()
        ),
        validation_completeness_record_refs=tuple(
            payload.get("validation_completeness_record_refs") or ()
        ),
        validation_completeness_record_digests=tuple(
            payload.get("validation_completeness_record_digests") or ()
        ),
        validation_source_snapshot_refs=tuple(
            payload.get("validation_source_snapshot_refs") or ()
        ),
        supported_evidence_types=tuple(payload.get("supported_evidence_types") or ()),
        supported_claim_types=tuple(payload.get("supported_claim_types") or ()),
        maximum_claim_strength=str(payload.get("maximum_claim_strength") or ""),
        maximum_claim_strength_rank=int(
            payload.get("maximum_claim_strength_rank", -1)
        ),
        claim_strength_taxonomy_version=str(
            payload.get("claim_strength_taxonomy_version") or ""
        ),
        input_completeness_statuses=tuple(
            payload.get("input_completeness_statuses") or ()
        ),
        plan_payload=_deep_freeze(_canonical_value(asdict(plan))),
        binding_payload=_deep_freeze(payload),
    )
    put_typed("binding", ref, record)
    return record


def _as_writer(
    writer: RuntimeEvidenceWriter | RuntimeEvidenceAuthority,
) -> RuntimeEvidenceWriter:
    if isinstance(writer, RuntimeEvidenceAuthority):
        return writer._runtime_writer()
    if not isinstance(writer, RuntimeEvidenceWriter):
        raise EvidenceIntegrityError("runtime_evidence_writer_invalid")
    return writer


def _validate_record_identity(kind: str, ref: str, record: Any) -> None:
    if not ref:
        raise EvidenceIntegrityError(f"authority_record_ref_missing:{kind}")
    ref_field = {
        "query": "result_ref",
        "rows": "rows_ref",
        "snapshot": "snapshot_ref",
        "completeness": "record_ref",
        "binding": "record_ref",
    }[kind]
    expected_ref = getattr(record, ref_field)
    if ref != expected_ref:
        raise EvidenceIntegrityError(f"authority_record_ref_mismatch:{kind}")
    integrity_errors = runtime_evidence_record_integrity_errors(record)
    if integrity_errors:
        raise EvidenceIntegrityError(integrity_errors[0])


def runtime_evidence_record_integrity_errors(record: Any) -> tuple[str, ...]:
    """Return canonical consumer-side integrity failures for one authority record."""
    if type(record) is SnapshotRecord:
        payload = _canonical_value(record.payload)
        expected_payload = _canonical_value(asdict(record.snapshot))
        errors = []
        if payload != expected_payload:
            errors.append("snapshot_record_payload_mismatch")
        if record.snapshot_ref != record.snapshot.snapshot_ref:
            errors.append("snapshot_record_ref_mismatch")
        digest = canonical_digest(payload)
        if digest != record.payload_digest or digest != record.record_digest:
            errors.append("snapshot_record_digest_mismatch")
        if record.record_ref != f"snapshot-record:{record.snapshot_ref}:{digest}":
            errors.append("snapshot_record_ref_mismatch")
        return tuple(errors)
    if type(record) is RowsRecord:
        errors = []
        payload = _canonical_value(record.metadata_payload)
        expected_payload = _canonical_value(
            {
                "rows_ref": record.rows_ref,
                "rows_content_hash": record.rows_content_hash,
                "row_count": record.row_count,
                "unique_key_fields": record.unique_key_fields,
                "storage_ref": record.storage_ref,
            }
        )
        if not record.rows_ref or not _content_addressed_storage_ref(
            record.storage_ref
        ):
            errors.append("rows_record_ref_mismatch")
        if payload != expected_payload:
            errors.append("rows_record_payload_mismatch")
        digest = canonical_digest(payload)
        if record.record_digest != digest:
            errors.append("rows_record_digest_mismatch")
        if record.record_ref != f"rows-record:{record.rows_ref}:{digest}":
            errors.append("rows_record_ref_mismatch")
        if record.row_count < 0:
            errors.append("rows_record_count_invalid")
        if (
            len(record.rows_content_hash) != 64
            or any(char not in "0123456789abcdef" for char in record.rows_content_hash)
        ):
            errors.append("rows_record_hash_invalid")
        return tuple(errors)
    if type(record) is QueryExecutionRecord:
        payload = _canonical_value(record.result_payload)
        contract_payload = _canonical_value(record.query_contract)
        record_payload = _canonical_value(record.record_payload)
        errors = []
        if "rows" in payload:
            errors.append("query_execution_rows_payload_forbidden")
        expected_fields = {
            "query_contract_ref": record.query_contract_ref,
            "query_hash": record.query_hash,
            "execution_attempt_ref": record.execution_attempt_ref,
            "result_ref": record.result_ref,
            "rows_ref": record.rows_ref,
            "completeness_report_ref": record.completeness_report_ref,
            "execution_status": record.execution_status,
            "row_count": record.row_count,
            "source_snapshot_refs": list(record.source_snapshot_refs),
        }
        if any(payload.get(key) != value for key, value in expected_fields.items()):
            errors.append("query_execution_payload_mismatch")
        if contract_payload != _dataclass_payload(record.contract):
            errors.append("query_contract_payload_mismatch")
        try:
            canonical_contract_signature = query_contract_signature(record.contract)
        except (TypeError, ValueError):
            canonical_contract_signature = ""
        if (
            not canonical_contract_signature
            or record.contract_signature != canonical_contract_signature
        ):
            errors.append("query_contract_signature_invalid")
        expected_record_payload = {
            "query_contract": contract_payload,
            "result": payload,
            "rows_content_hash": record.rows_content_hash,
            "source_snapshot_record_refs": list(
                record.source_snapshot_record_refs
            ),
            "source_snapshot_record_digests": list(
                record.source_snapshot_record_digests
            ),
        }
        if record_payload != expected_record_payload:
            errors.append("query_execution_record_payload_mismatch")
        digest = canonical_digest(record_payload)
        if record.record_digest != digest:
            errors.append("query_execution_record_digest_mismatch")
        if (
            record.contract_signature != record.contract.contract_signature
            or record.query_contract_ref != record.contract.query_contract_id
        ):
            errors.append("query_contract_denormalized_mismatch")
        if not (
            len(record.source_snapshot_refs)
            == len(record.source_snapshot_record_refs)
            == len(record.source_snapshot_record_digests)
        ):
            errors.append("query_snapshot_record_cardinality_mismatch")
        expected_refs = query_audit_refs(
            record.query_hash,
            record.contract_signature,
            record.source_snapshot_refs,
            query_contract_ref=record.query_contract_ref,
            execution_attempt_ref=record.execution_attempt_ref,
            rows_content_hash=(
                record.rows_content_hash
                if record.execution_status == "succeeded"
                else ""
            ),
        )
        if (
            record.record_ref
            != f"query-execution:{record.result_ref}:{digest}"
            or record.result_ref != expected_refs.result_ref
            or record.rows_ref != expected_refs.rows_ref
            or record.completeness_report_ref != expected_refs.completeness_report_ref
        ):
            errors.append("query_execution_record_ref_mismatch")
        return tuple(errors)
    if type(record) is CompletenessRecord:
        payload = _canonical_value(record.report_payload)
        digest = canonical_digest(payload)
        errors = []
        if (
            payload.get("report_ref") != record.report_ref
            or payload.get("query_contract_ref") != record.query_contract_ref
            or payload.get("result_ref") != record.result_ref
        ):
            errors.append("completeness_record_payload_mismatch")
        if digest != record.report_digest:
            errors.append("completeness_record_digest_mismatch")
        if record.record_ref != f"completeness-record:{record.report_ref}:{digest}":
            errors.append("completeness_record_ref_mismatch")
        return tuple(errors)
    if type(record) is CapabilityBindingRecord:
        payload = _canonical_value(record.binding_payload)
        plan = _canonical_value(record.plan_payload)
        errors = []
        digest = canonical_digest({"plan": plan, "binding": payload})
        if (
            record.binding_digest != digest
            or record.record_ref
            != f"capability-binding:{record.capability_id}:{digest}"
        ):
            errors.append("capability_binding_record_digest_mismatch")
        plan_fields = {
            "capability_id": record.capability_id,
            "capability_contract_version": record.capability_contract_version,
            "capability_contract_signature": record.capability_contract_signature,
            "analysis_contract_ref": record.analysis_contract_ref,
        }
        if any(plan.get(key) != value for key, value in plan_fields.items()):
            errors.append("capability_binding_plan_payload_mismatch")
        binding_fields = (
            "status",
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
        if any(
            _canonical_value(getattr(record, field)) != payload.get(field)
            for field in binding_fields
        ):
            errors.append("capability_binding_record_payload_mismatch")
        provenance_groups = (
            (
                record.query_contract_refs,
                record.result_refs,
                record.query_execution_record_refs,
                record.query_execution_record_digests,
                record.rows_refs,
                record.rows_metadata_record_refs,
                record.rows_metadata_record_digests,
                record.rows_content_hashes,
                record.completeness_report_refs,
                record.completeness_record_refs,
                record.completeness_record_digests,
            ),
            (
                record.validation_query_contract_refs,
                record.validation_result_refs,
                record.validation_query_execution_record_refs,
                record.validation_query_execution_record_digests,
                record.validation_rows_refs,
                record.validation_rows_metadata_record_refs,
                record.validation_rows_metadata_record_digests,
                record.validation_rows_content_hashes,
                record.validation_completeness_report_refs,
                record.validation_completeness_record_refs,
                record.validation_completeness_record_digests,
            ),
        )
        if any(len({len(values) for values in group}) != 1 for group in provenance_groups):
            errors.append("capability_binding_record_cardinality_mismatch")
        return tuple(errors)
    return ("runtime_evidence_record_type_invalid",)


def _dataclass_payload(value: Any) -> Mapping[str, Any]:
    return {
        field.name: _canonical_value(getattr(value, field.name))
        for field in fields(value)
    }


def _canonical_value(value: Any) -> Any:
    if is_dataclass(value):
        return _canonical_value(canonical_thaw(value))
    if isinstance(value, Mapping):
        normalized = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise EvidenceIntegrityError("canonical_mapping_key_not_string")
            normalized[key] = _canonical_value(item)
        return {key: normalized[key] for key in sorted(normalized)}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        raise EvidenceIntegrityError("canonical_set_not_supported")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise EvidenceIntegrityError("canonical_number_not_finite")
        return {"$decimal": str(value)}
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise EvidenceIntegrityError("canonical_datetime_timezone_required")
        return {"$datetime": value.isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, float) and not math.isfinite(value):
        raise EvidenceIntegrityError("canonical_number_not_finite")
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise EvidenceIntegrityError(f"canonical_type_unsupported:{type(value).__name__}")


def _scalar_key(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, Decimal):
        return value.is_finite()
    return isinstance(value, (str, int, bool, date)) or value is None


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    return value
