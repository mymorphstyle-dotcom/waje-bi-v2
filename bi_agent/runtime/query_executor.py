from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import secrets
from typing import Any, Mapping, Sequence

from bi_agent.runtime.analysis_contracts import QueryContract, QueryResultEnvelope
from bi_agent.runtime.clickhouse_query_compiler import compile_clickhouse_query
from bi_agent.runtime.clickhouse_runtime import ClickHouseRuntime, audit_query_hash
from bi_agent.runtime.dataset_catalog import DatasetSnapshot
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
    ) -> str:
        rows_ref = query_rows_ref(
            query_hash,
            semantic_signature,
            snapshot_refs,
        )
        self._rows[rows_ref] = deepcopy(tuple(dict(row) for row in rows))
        return rows_ref

    def get(self, rows_ref: str) -> tuple[Mapping[str, Any], ...]:
        return deepcopy(self._rows[rows_ref])


class ClickHouseQueryExecutor:
    def __init__(
        self,
        runtime: ClickHouseRuntime,
        *,
        rows_store: AggregateRowsStore | None = None,
    ) -> None:
        self.runtime = runtime
        self.rows_store = rows_store or AggregateRowsStore()

    def execute(
        self,
        contract: QueryContract,
        snapshots: Mapping[str, DatasetSnapshot],
        *,
        execution_attempt_ref: str = "",
    ) -> QueryResultEnvelope:
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
            compiled = compile_clickhouse_query(contract, snapshots)
        except (KeyError, PermissionError, TypeError, ValueError) as exc:
            return _failed_envelope(
                contract,
                query_id=blocked_query_id,
                query_hash="",
                reason=str(exc),
                execution_status="blocked",
                execution_attempt_ref=attempt_ref,
            )
        query_hash = audit_query_hash(compiled.sql_text, compiled.parameters)
        query_id = (
            f"clickhouse:{contract.query_contract_id}:"
            f"{query_hash[:12]}:{attempt_identity}"
        )
        validation = validate_select_only(compiled.sql_text, aggregate=True)
        if not validation.ok:
            return _failed_envelope(
                contract,
                query_id=query_id,
                query_hash=query_hash,
                reason=validation.reason,
                execution_status="blocked",
                execution_attempt_ref=attempt_ref,
            )

        try:
            result = self.runtime.aggregate(
                compiled.sql_text,
                query_id=query_id,
                parameters=compiled.parameters,
                settings=compiled.settings,
                execution_attempt_ref=attempt_ref,
            )
        except TypeError as exc:
            return _failed_envelope(
                contract,
                query_id=query_id,
                query_hash=query_hash,
                reason=f"clickhouse_provider_type_error:{exc}",
                execution_status="failed",
                execution_attempt_ref=attempt_ref,
            )
        effective_hash = result.query_hash or query_hash
        if not result.ok:
            return _failed_envelope(
                contract,
                query_id=result.query_id or query_id,
                query_hash=effective_hash,
                reason=result.reason,
                provider_stats=result.provider_stats,
                execution_status="failed",
                execution_attempt_ref=attempt_ref,
            )

        rows, join_audit_stats, failure_reason = _aggregate_rows(
            result.rows,
            contract,
        )
        if failure_reason:
            return _failed_envelope(
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
            )

        audit_refs = query_audit_refs(
            effective_hash,
            contract.contract_signature,
            contract.dataset_snapshot_refs,
            query_contract_ref=contract.query_contract_id,
            execution_attempt_ref=attempt_ref,
        )
        rows_ref = self.rows_store.persist(
            effective_hash,
            contract.contract_signature,
            contract.dataset_snapshot_refs,
            rows,
        )
        provider_stats = dict(result.provider_stats)
        provider_stats.update(join_audit_stats)
        return QueryResultEnvelope(
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
        )


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
        rows.append(
            {
                str(key): value
                for key, value in raw_row.items()
                if str(key) in allowed
            }
        )
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
