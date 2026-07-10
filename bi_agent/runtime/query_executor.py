from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Mapping, Sequence

from bi_agent.runtime.analysis_contracts import QueryContract, QueryResultEnvelope
from bi_agent.runtime.clickhouse_query_compiler import compile_clickhouse_query
from bi_agent.runtime.clickhouse_runtime import ClickHouseRuntime, audit_query_hash
from bi_agent.runtime.dataset_catalog import DatasetSnapshot
from bi_agent.runtime.sql_safety import validate_select_only


_RAW_IDENTIFIER_FIELDS = frozenset(
    {"user_id", "order_id", "payment_order_id", "订单id"}
)
_SAFE_AUXILIARY_FIELDS = frozenset(
    {
        "calendar_week",
        "weekday",
        "month_phase",
        "source_row_count",
        "event_count",
        "high_value_threshold",
        "high_value_amount",
        "high_value_paid_users",
    }
)


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
        rows_ref = "rows:" + _audit_identity(
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
    ) -> QueryResultEnvelope:
        blocked_query_id = f"clickhouse:{contract.query_contract_id}:blocked"
        try:
            compiled = compile_clickhouse_query(contract, snapshots)
        except (KeyError, PermissionError, TypeError, ValueError) as exc:
            return _failed_envelope(
                contract,
                query_id=blocked_query_id,
                query_hash="",
                reason=str(exc),
                execution_status="blocked",
            )
        query_hash = audit_query_hash(compiled.sql_text, compiled.parameters)
        query_id = (
            f"clickhouse:{contract.query_contract_id}:{query_hash[:12]}"
        )
        validation = validate_select_only(compiled.sql_text, aggregate=True)
        if not validation.ok:
            return _failed_envelope(
                contract,
                query_id=query_id,
                query_hash=query_hash,
                reason=validation.reason,
                execution_status="blocked",
            )

        try:
            result = self.runtime.aggregate(
                compiled.sql_text,
                query_id=query_id,
                parameters=compiled.parameters,
                settings=compiled.settings,
            )
        except TypeError as exc:
            return _failed_envelope(
                contract,
                query_id=query_id,
                query_hash=query_hash,
                reason=f"clickhouse_provider_type_error:{exc}",
                execution_status="failed",
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
            )

        rows, failure_reason = _aggregate_rows(result.rows, contract)
        if failure_reason:
            return _failed_envelope(
                contract,
                query_id=result.query_id or query_id,
                query_hash=effective_hash,
                reason=failure_reason,
                provider_stats=result.provider_stats,
                execution_status=(
                    "blocked"
                    if failure_reason.startswith("raw_identifier_output_rejected:")
                    else "failed"
                ),
            )

        audit_identity = _audit_identity(
            effective_hash,
            contract.contract_signature,
            contract.dataset_snapshot_refs,
        )
        rows_ref = self.rows_store.persist(
            effective_hash,
            contract.contract_signature,
            contract.dataset_snapshot_refs,
            rows,
        )
        result_ref = f"result:{audit_identity}"
        completeness_report_ref = f"completeness:{audit_identity}"
        return QueryResultEnvelope(
            query_contract_ref=contract.query_contract_id,
            query_id=result.query_id or query_id,
            query_hash=effective_hash,
            result_ref=result_ref,
            execution_status="succeeded",
            rows_ref=rows_ref,
            row_count=len(rows),
            completeness_report_ref=completeness_report_ref,
            rows=rows,
            observed_schema=_observed_schema(rows),
            observed_windows=_observed_windows(rows),
            observed_grain=_observed_grain(rows, contract.result_shape.grain),
            source_snapshot_refs=contract.dataset_snapshot_refs,
            provider_stats=dict(result.provider_stats),
        )


def _failed_envelope(
    contract: QueryContract,
    *,
    query_id: str,
    query_hash: str,
    reason: str,
    provider_stats: Mapping[str, Any] | None = None,
    execution_status: str,
) -> QueryResultEnvelope:
    return QueryResultEnvelope(
        query_contract_ref=contract.query_contract_id,
        query_id=query_id,
        query_hash=query_hash,
        result_ref="",
        execution_status=execution_status,
        rows_ref="",
        row_count=0,
        completeness_report_ref="",
        rows=(),
        observed_schema={},
        observed_windows=(),
        observed_grain=(),
        source_snapshot_refs=contract.dataset_snapshot_refs,
        provider_stats=dict(provider_stats or {}),
        failure_reason=reason,
    )


def _aggregate_rows(
    raw_rows: Sequence[Any],
    contract: QueryContract,
) -> tuple[tuple[Mapping[str, Any], ...], str]:
    allowed = set(contract.result_shape.required_fields) | set(_SAFE_AUXILIARY_FIELDS)
    rows = []
    for raw_row in raw_rows:
        if not isinstance(raw_row, Mapping):
            return (), "invalid_clickhouse_row_shape"
        raw_keys = {str(key) for key in raw_row}
        leaked = raw_keys & _RAW_IDENTIFIER_FIELDS
        if leaked:
            return (), f"raw_identifier_output_rejected:{','.join(sorted(leaked))}"
        rows.append(
            {
                str(key): value
                for key, value in raw_row.items()
                if str(key) in allowed
            }
        )
    return tuple(rows), ""


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


def _audit_identity(
    query_hash: str,
    semantic_signature: str,
    snapshot_refs: Sequence[str],
) -> str:
    snapshot_payload = json.dumps(
        tuple(str(item) for item in snapshot_refs),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    snapshot_identity = hashlib.sha256(
        snapshot_payload.encode("utf-8")
    ).hexdigest()[:16]
    return ":".join(
        (
            query_hash,
            semantic_signature[:16] or "unsigned",
            snapshot_identity,
        )
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
