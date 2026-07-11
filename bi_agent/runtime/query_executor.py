from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import secrets
from typing import Any, Mapping, Sequence

from bi_agent.runtime.analysis_contracts import (
    QueryContract,
    QueryResultEnvelope,
    canonical_exact_additive_count,
    query_contract_signature,
)
from bi_agent.runtime.clickhouse_query_compiler import compile_clickhouse_query
from bi_agent.runtime.clickhouse_runtime import ClickHouseRuntime, audit_query_hash
from bi_agent.runtime.dataset_catalog import DatasetReleaseResolver, DatasetSnapshot
from bi_agent.runtime.evidence_authority import (
    EvidenceIntegrityError,
    RowsPayloadLoader,
    RuntimeEvidenceAuthority,
    RuntimeEvidenceResolver,
    RuntimeEvidenceWriter,
    _record_query_execution,
    canonical_digest,
    canonical_result_rows_hash,
    runtime_evidence_record_integrity_errors,
)
from bi_agent.runtime.query_audit import query_audit_refs, query_rows_ref
from bi_agent.runtime.sql_safety import validate_select_only


@dataclass
class AggregateRowsStore:
    _rows: dict[str, tuple[Mapping[str, Any], ...]] = field(default_factory=dict)

    def persist(
        self,
        query_hash: str,
        semantic_signature: str,
        snapshot_refs: Sequence[str],
        rows: Sequence[Mapping[str, Any]],
        unique_key_fields: Sequence[str] = (),
    ) -> str:
        rows_content_hash = canonical_result_rows_hash(rows, unique_key_fields)
        rows_ref = query_rows_ref(
            query_hash,
            semantic_signature,
            snapshot_refs,
            rows_content_hash,
        )
        self._rows[rows_ref] = deepcopy(tuple(dict(row) for row in rows))
        return rows_ref

    def get(self, rows_ref: str) -> tuple[Mapping[str, Any], ...]:
        return deepcopy(self._rows[rows_ref])

    def load_rows(self, storage_ref: str) -> tuple[Mapping[str, Any], ...] | None:
        rows = self._rows.get(storage_ref)
        return deepcopy(rows) if rows is not None else None


class ClickHouseQueryExecutor:
    def __init__(
        self,
        runtime: ClickHouseRuntime,
        *,
        rows_store: AggregateRowsStore | None = None,
        evidence_authority: RuntimeEvidenceAuthority | None = None,
        evidence_resolver: RuntimeEvidenceResolver | None = None,
        evidence_writer: RuntimeEvidenceWriter | None = None,
        rows_loader: RowsPayloadLoader | None = None,
        release_resolver: DatasetReleaseResolver | None = None,
    ) -> None:
        self.runtime = runtime
        self.rows_store = rows_store or AggregateRowsStore()
        authority = evidence_authority
        if authority is None and (
            evidence_resolver is None or evidence_writer is None or rows_loader is None
        ):
            authority = RuntimeEvidenceAuthority()
        self.evidence_authority = authority
        self.evidence_resolver = evidence_resolver or authority
        self.evidence_writer = evidence_writer or (
            authority._runtime_writer() if authority is not None else None
        )
        self.rows_loader = rows_loader or (
            authority.rows_loader if authority is not None else None
        )
        self.release_resolver = release_resolver
        if self.evidence_writer is None:
            raise ValueError("runtime_evidence_writer_missing")

    def execute(
        self,
        contract: QueryContract,
        snapshots: Mapping[str, DatasetSnapshot],
        *,
        execution_attempt_ref: str = "",
        release_resolver: DatasetReleaseResolver | None = None,
    ) -> QueryResultEnvelope:
        def finish(envelope: QueryResultEnvelope) -> QueryResultEnvelope:
            if (
                envelope.execution_status != "succeeded"
                and query_contract_signature(contract)
                != contract.contract_signature
            ):
                return envelope
            record = _record_query_execution(
                self.evidence_writer,
                contract,
                envelope,
                snapshots,
            )
            expected_result_payload = envelope.to_dict()
            expected_result_payload.pop("rows", None)
            if (
                runtime_evidence_record_integrity_errors(record)
                or record.query_contract_ref != contract.query_contract_id
                or record.result_ref != envelope.result_ref
                or record.rows_ref != envelope.rows_ref
                or record.completeness_report_ref
                != envelope.completeness_report_ref
                or canonical_digest(record.query_contract)
                != canonical_digest(contract.to_dict())
                or canonical_digest(record.result_payload)
                != canonical_digest(expected_result_payload)
            ):
                raise EvidenceIntegrityError(
                    "query_execution_writer_record_invalid"
                )
            return envelope

        attempt_ref = execution_attempt_ref or (
            "attempt:" + secrets.token_urlsafe(18)
        )
        attempt_identity = hashlib.sha256(
            attempt_ref.encode("utf-8")
        ).hexdigest()[:12]
        blocked_query_id = (
            f"clickhouse:{contract.query_contract_id}:blocked:{attempt_identity}"
        )
        try:
            compiled = compile_clickhouse_query(
                contract,
                snapshots,
                release_resolver=release_resolver or self.release_resolver,
            )
        except (KeyError, PermissionError, TypeError, ValueError) as exc:
            return finish(_failed_envelope(
                contract,
                query_id=blocked_query_id,
                query_hash="",
                reason=str(exc),
                execution_status="blocked",
                execution_attempt_ref=attempt_ref,
            ))
        query_hash = audit_query_hash(compiled.sql_text, compiled.parameters)
        query_id = (
            f"clickhouse:{contract.query_contract_id}:"
            f"{query_hash[:12]}:{attempt_identity}"
        )
        bounded_context = (
            contract.result_shape.result_semantics == "complete_context_rows"
        )
        validation = validate_select_only(
            compiled.sql_text,
            aggregate=not bounded_context,
        )
        if not validation.ok:
            return finish(_failed_envelope(
                contract,
                query_id=query_id,
                query_hash=query_hash,
                reason=validation.reason,
                execution_status="blocked",
                execution_attempt_ref=attempt_ref,
            ))

        try:
            execute = (
                self.runtime.bounded_context
                if bounded_context
                else self.runtime.aggregate
            )
            result = execute(
                compiled.sql_text,
                query_id=query_id,
                parameters=compiled.parameters,
                settings=compiled.settings,
                execution_attempt_ref=attempt_ref,
            )
        except TypeError as exc:
            return finish(_failed_envelope(
                contract,
                query_id=query_id,
                query_hash=query_hash,
                reason=f"clickhouse_provider_type_error:{exc}",
                execution_status="failed",
                execution_attempt_ref=attempt_ref,
            ))
        effective_hash = result.query_hash or query_hash
        if not result.ok:
            return finish(_failed_envelope(
                contract,
                query_id=result.query_id or query_id,
                query_hash=effective_hash,
                reason=result.reason,
                provider_stats=result.provider_stats,
                execution_status="failed",
                execution_attempt_ref=attempt_ref,
            ))

        if (
            bounded_context
            and compiled.max_context_rows > 0
            and len(result.rows) > compiled.max_context_rows
        ):
            provider_stats = dict(result.provider_stats)
            provider_stats.update(
                {
                    "context_row_count": len(result.rows),
                    "max_context_rows": compiled.max_context_rows,
                }
            )
            return finish(_failed_envelope(
                contract,
                query_id=result.query_id or query_id,
                query_hash=effective_hash,
                reason=f"context_row_bound_exceeded:{compiled.max_context_rows}",
                provider_stats=provider_stats,
                execution_status="failed",
                execution_attempt_ref=attempt_ref,
            ))

        rows, join_audit_stats, failure_reason = _aggregate_rows(
            result.rows,
            contract,
        )
        if failure_reason:
            return finish(_failed_envelope(
                contract,
                query_id=result.query_id or query_id,
                query_hash=effective_hash,
                reason=failure_reason,
                provider_stats=result.provider_stats,
                execution_status=(
                    "blocked"
                    if failure_reason.startswith(
                        "unreviewed_output_field_rejected:"
                    )
                    else "failed"
                ),
                execution_attempt_ref=attempt_ref,
            ))

        try:
            rows_content_hash = canonical_result_rows_hash(
                rows,
                contract.result_shape.unique_key,
            )
        except EvidenceIntegrityError as exc:
            return finish(_failed_envelope(
                contract,
                query_id=result.query_id or query_id,
                query_hash=effective_hash,
                reason=f"invalid_result_rows:{exc}",
                provider_stats=result.provider_stats,
                execution_status="failed",
                execution_attempt_ref=attempt_ref,
            ))
        audit_refs = query_audit_refs(
            effective_hash,
            contract.contract_signature,
            contract.dataset_snapshot_refs,
            query_contract_ref=contract.query_contract_id,
            execution_attempt_ref=attempt_ref,
            rows_content_hash=rows_content_hash,
        )
        rows_ref = self.rows_store.persist(
            effective_hash,
            contract.contract_signature,
            contract.dataset_snapshot_refs,
            rows,
            contract.result_shape.unique_key,
        )
        provider_stats = dict(result.provider_stats)
        provider_stats.update(join_audit_stats)
        return finish(QueryResultEnvelope(
            query_contract_ref=contract.query_contract_id,
            query_id=result.query_id or query_id,
            query_hash=effective_hash,
            result_ref=audit_refs.result_ref,
            execution_status="succeeded",
            rows_ref=rows_ref,
            row_count=len(rows),
            completeness_report_ref=audit_refs.completeness_report_ref,
            rows=rows,
            observed_schema=_observed_schema(rows),
            observed_windows=_observed_windows(rows),
            observed_grain=_observed_grain(rows, contract.result_shape.grain),
            source_snapshot_refs=contract.dataset_snapshot_refs,
            provider_stats=provider_stats,
            execution_attempt_ref=attempt_ref,
        ))


def _failed_envelope(
    contract: QueryContract,
    *,
    query_id: str,
    query_hash: str,
    reason: str,
    provider_stats: Mapping[str, Any] | None = None,
    execution_status: str,
    execution_attempt_ref: str,
) -> QueryResultEnvelope:
    audit_refs = query_audit_refs(
        query_hash,
        contract.contract_signature,
        contract.dataset_snapshot_refs,
        query_contract_ref=contract.query_contract_id,
        execution_attempt_ref=execution_attempt_ref,
    )
    return QueryResultEnvelope(
        query_contract_ref=contract.query_contract_id,
        query_id=query_id,
        query_hash=query_hash,
        result_ref=audit_refs.result_ref,
        execution_status=execution_status,
        rows_ref=audit_refs.rows_ref,
        row_count=0,
        completeness_report_ref=audit_refs.completeness_report_ref,
        rows=(),
        observed_schema={},
        observed_windows=(),
        observed_grain=(),
        source_snapshot_refs=contract.dataset_snapshot_refs,
        provider_stats=dict(provider_stats or {}),
        failure_reason=reason,
        execution_attempt_ref=execution_attempt_ref,
    )


def _aggregate_rows(
    raw_rows: Sequence[Any],
    contract: QueryContract,
) -> tuple[tuple[Mapping[str, Any], ...], Mapping[str, Any], str]:
    allowed = set(contract.result_shape.required_fields)
    audit_fields = (
        set(contract.join_expectation.audit_fields)
        if contract.join_expectation is not None
        else set()
    )
    rows = []
    exact_count_metrics = {
        binding.metric_id
        for binding in contract.metric_bindings
        if binding.reconciliation_strategy == "exact_additive_count"
    }
    audit_values: dict[str, list[Any]] = {
        field: [] for field in audit_fields
    }
    for raw_row in raw_rows:
        if not isinstance(raw_row, Mapping):
            return (), {}, "invalid_clickhouse_row_shape"
        raw_keys = {str(key) for key in raw_row}
        unreviewed = raw_keys - allowed - audit_fields
        if unreviewed:
            return (), {}, (
                "unreviewed_output_field_rejected:"
                + ",".join(sorted(unreviewed))
            )
        for field in audit_fields:
            if field in raw_row:
                audit_values[field].append(raw_row[field])
        row = {
            str(key): value
            for key, value in raw_row.items()
            if str(key) in allowed
        }
        for metric_id in exact_count_metrics.intersection(row):
            canonical = canonical_exact_additive_count(row[metric_id])
            if canonical is not None:
                row[metric_id] = canonical
        rows.append(row)
    join_audit_stats: dict[str, Any] = {}
    if contract.join_expectation is not None:
        for field, values in audit_values.items():
            provider_field = field.removeprefix("__")
            if values and all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in values
            ):
                join_audit_stats[provider_field] = sum(values)
            elif values:
                join_audit_stats[provider_field] = values[0]
    return tuple(rows), join_audit_stats, ""


def _observed_schema(rows: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    fields: dict[str, set[str]] = {}
    for row in rows:
        for key, value in row.items():
            fields.setdefault(str(key), set()).add(_type_name(value))
    return {
        key: (
            next(iter(types))
            if len(types) == 1
            else "mixed[" + ",".join(sorted(types)) + "]"
        )
        for key, types in fields.items()
    }


def _observed_windows(rows: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            str(row["window_id"])
            for row in rows
            if row.get("window_id") not in (None, "")
        )
    )


def _observed_grain(
    rows: Sequence[Mapping[str, Any]],
    expected_grain: Sequence[str],
) -> tuple[str, ...]:
    if not rows:
        return ()
    return tuple(
        field
        for field in expected_grain
        if all(field in row for row in rows)
    )


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    return type(value).__name__
