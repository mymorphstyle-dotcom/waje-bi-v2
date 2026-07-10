from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
from typing import Any, Callable, Mapping, Optional, Sequence

from bi_agent.runtime.analysis_contracts import (
    DimensionBinding,
    JoinExpectation,
    MetricBinding,
    QueryContract,
    QueryResultEnvelope,
    ReconciliationBinding,
    ResolvedWindow,
    ResultShape,
)
from bi_agent.runtime.clickhouse_query_planner import build_clickhouse_query_specs
from bi_agent.runtime.clickhouse_runtime import ClickHouseRuntime, IDENTIFIER_PATTERN
from bi_agent.runtime.dataset_catalog import DatasetReleaseResolver, DatasetSnapshot
from bi_agent.runtime.query_executor import ClickHouseQueryExecutor
from bi_agent.runtime.sql_safety import validate_select_only


DEFAULT_TABLE = "paid_order_success_clean_20240101_20260704"
MAX_ROWS = 5000
BASE_FIELDS = ("period", "group", "amount", "paid_users", "orders", "first_paid_users")
JOINT_DIMENSIONS = ("channel", "payment_method", "region", "device_brand")
SEGMENT_DIMENSIONS = ("channel",)
CONTEXT_SNAPSHOT_QUERY_INTENTS = frozenset(
    (
        "channel_context_probe",
        "source_reconciliation_probe",
        "data_quality_probe",
    )
)


@dataclass(frozen=True)
class RevenueQueryPlan:
    sql_text: str
    query_id: str
    intent: str
    required_fields: tuple[str, ...]
    dimension_keys: tuple[str, ...]
    reason: str = ""
    claim_use: str = ""


@dataclass(frozen=True)
class RevenueRowPlan:
    sql_text: str
    query_id: str
    required_fields: tuple[str, ...]
    dimension_keys: tuple[str, ...]
    reason: str = ""
    query_plans: tuple[RevenueQueryPlan, ...] = ()
    contract_mode: str = "legacy"
    query_contracts: tuple[QueryContract, ...] = ()
    snapshots: Mapping[str, DatasetSnapshot] = field(default_factory=dict)


@dataclass(frozen=True)
class RevenueRowsResult:
    ok: bool
    rows: tuple[dict[str, Any], ...] = ()
    reason: str = ""
    query_hash: str = ""
    query_id: str = ""
    result_refs: tuple[str, ...] = ()
    rows_by_intent: Mapping[str, tuple[dict[str, Any], ...]] = field(default_factory=dict)
    result_refs_by_intent: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    query_results: tuple[dict[str, Any], ...] = ()
    contract_mode: str = "legacy"
    query_envelopes: tuple[QueryResultEnvelope, ...] = ()


class ClickHouseRevenueRows:
    def __init__(
        self,
        runtime: Optional[ClickHouseRuntime] = None,
        table: Optional[str] = None,
        *,
        snapshots: Mapping[str, DatasetSnapshot] | None = None,
        snapshot_loader: Callable[..., Mapping[str, DatasetSnapshot]] | None = None,
        executor: ClickHouseQueryExecutor | None = None,
        release_resolver: DatasetReleaseResolver | None = None,
    ) -> None:
        self.runtime = runtime or ClickHouseRuntime.from_env()
        self.table = table or os.environ.get("WAJE_CLICKHOUSE_PAYMENT_TABLE", DEFAULT_TABLE)
        self.snapshots = dict(snapshots or {})
        self.snapshot_loader = snapshot_loader
        self.release_resolver = release_resolver
        self.executor = executor or ClickHouseQueryExecutor(
            self.runtime,
            release_resolver=release_resolver,
        )
        self._schema_fields: tuple[str, ...] | None = None

    @classmethod
    def from_env(
        cls,
        *,
        snapshot_loader: Callable[..., Mapping[str, DatasetSnapshot]] | None = None,
        release_resolver: DatasetReleaseResolver | None = None,
        evidence_resolver: Any = None,
        evidence_writer: Any = None,
        rows_loader: Any = None,
    ) -> "ClickHouseRevenueRows":
        runtime = ClickHouseRuntime.from_env()
        return cls(
            runtime,
            snapshot_loader=snapshot_loader,
            executor=ClickHouseQueryExecutor(
                runtime,
                evidence_resolver=evidence_resolver,
                evidence_writer=evidence_writer,
                rows_loader=rows_loader,
                release_resolver=release_resolver,
            ),
            release_resolver=release_resolver,
        )

    def configured(self) -> bool:
        return self.runtime.configured()

    def binding_reason(self) -> str:
        return self.runtime.binding.reason

    def schema_fields(self) -> tuple[str, ...]:
        if self._schema_fields is not None:
            return self._schema_fields
        if not self.configured() or not _safe_identifier(self.table):
            self._schema_fields = ()
            return self._schema_fields
        result = self.runtime.describe_table(self.table)
        if not result.ok:
            self._schema_fields = ()
            return self._schema_fields
        fields = tuple(
            dict.fromkeys(
                field
                for row in result.rows
                for field in (_describe_field_name(row),)
                if field
            )
        )
        self._schema_fields = fields
        return fields

    def plan(
        self,
        request: Mapping[str, Any],
        intent: Mapping[str, Any],
        accepted_graph: Sequence[str],
    ) -> RevenueRowPlan:
        compiler_runtime_plan = request.get("compiler_runtime_plan")
        if (
            isinstance(compiler_runtime_plan, Mapping)
            and "query_contracts" in compiler_runtime_plan
        ):
            return self._typed_plan(request, compiler_runtime_plan)
        if (
            isinstance(compiler_runtime_plan, Mapping)
            and compiler_runtime_plan.get("query_intents")
        ):
            specs = build_clickhouse_query_specs(
                compiler_runtime_plan,
                table=self.table,
                run_id=str(request.get("run_id", "run")),
            )
            if specs:
                first = _select_query_spec(specs, accepted_graph)
                query_plans = tuple(_query_plan_from_spec(spec) for spec in specs)
                return RevenueRowPlan(
                    sql_text=str(first["sql_text"]),
                    query_id=str(first["query_id"]),
                    required_fields=tuple(str(field) for field in first["required_fields"]),
                    dimension_keys=tuple(str(key) for key in first["dimension_keys"]),
                    reason=str(first.get("reason") or ""),
                    query_plans=query_plans,
                )
            return RevenueRowPlan(
                sql_text="",
                query_id=f"{request.get('run_id', 'run')}:compiler_runtime_plan",
                required_fields=_required_fields(request),
                dimension_keys=_dimension_keys(accepted_graph, request),
                reason=(
                    "invalid_identifier"
                    if not _safe_identifier(self.table)
                    else "no_executable_query_spec"
                ),
            )

        dimensions = _dimension_keys(accepted_graph, request)
        required_fields = _required_fields(request)
        query_id = f"{request.get('run_id', 'run')}:clickhouse_revenue_rows"
        if not _safe_identifier(self.table):
            return RevenueRowPlan(
                sql_text="",
                query_id=query_id,
                required_fields=required_fields,
                dimension_keys=dimensions,
                reason="invalid_identifier",
            )

        select_dimensions = ", ".join(dimensions)
        group_dimensions = ", ".join(dimensions)
        dimension_sql = f", {select_dimensions}" if select_dimensions else ""
        group_sql = f", {group_dimensions}" if group_dimensions else ""
        sql = f"""
SELECT
    business_date_lagos AS period,
    multiIf(
        business_date_lagos = toDate(now('Africa/Lagos')) - 1, 'target',
        business_date_lagos = toDate(now('Africa/Lagos')) - 2, 'baseline',
        'history'
    ) AS group,
    sum(paid_amount_ngn) AS amount,
    uniqExact(user_id) AS paid_users,
    count() AS orders,
    countIf(is_first_payment = '1') AS first_paid_users{dimension_sql}
FROM {self.table}
WHERE business_date_lagos >= toDate(now('Africa/Lagos')) - 36
  AND business_date_lagos <= toDate(now('Africa/Lagos')) - 1
GROUP BY period, group{group_sql}
LIMIT {MAX_ROWS}
"""
        return RevenueRowPlan(
            sql_text=sql.strip(),
            query_id=query_id,
            required_fields=required_fields,
            dimension_keys=dimensions,
            query_plans=(
                RevenueQueryPlan(
                    sql_text=sql.strip(),
                    query_id=query_id,
                    intent="clickhouse_revenue_rows",
                    required_fields=required_fields,
                    dimension_keys=dimensions,
                ),
            ),
        )

    def _typed_plan(
        self,
        request: Mapping[str, Any],
        compiler_runtime_plan: Mapping[str, Any],
    ) -> RevenueRowPlan:
        try:
            query_contracts = _query_contracts(
                compiler_runtime_plan.get("query_contracts") or ()
            )
        except (KeyError, TypeError, ValueError) as exc:
            return RevenueRowPlan(
                sql_text="",
                query_id=f"{request.get('run_id', 'run')}:typed_projection",
                required_fields=(),
                dimension_keys=(),
                reason=f"invalid_typed_query_contract_projection:{exc}",
                contract_mode="typed",
            )
        if not query_contracts:
            return RevenueRowPlan(
                sql_text="",
                query_id=f"{request.get('run_id', 'run')}:typed_query_contracts",
                required_fields=(),
                dimension_keys=(),
                reason="typed_query_contracts_missing",
                contract_mode="typed",
            )
        try:
            snapshots = self._trusted_snapshots(query_contracts)
        except Exception as exc:
            first = query_contracts[0]
            return RevenueRowPlan(
                sql_text="",
                query_id=first.query_contract_id,
                required_fields=first.result_shape.required_fields,
                dimension_keys=tuple(
                    item.dimension_id for item in first.dimension_bindings
                ),
                reason=f"dataset_snapshot_provider_refresh_failed:{exc}",
                contract_mode="typed",
                query_contracts=query_contracts,
            )
        try:
            for source in (
                compiler_runtime_plan.get("dataset_snapshots"),
                request.get("dataset_snapshots"),
            ):
                selected = _request_dataset_snapshots(source or (), snapshots)
                for snapshot_ref, snapshot in selected.items():
                    existing = snapshots.get(snapshot_ref)
                    if existing is not None and existing != snapshot:
                        raise ValueError(
                            f"dataset_snapshot_provider_request_conflict:{snapshot_ref}"
                        )
                    snapshots[snapshot_ref] = snapshot
        except (KeyError, TypeError, ValueError) as exc:
            first = query_contracts[0]
            return RevenueRowPlan(
                sql_text="",
                query_id=first.query_contract_id,
                required_fields=first.result_shape.required_fields,
                dimension_keys=tuple(
                    item.dimension_id for item in first.dimension_bindings
                ),
                reason=f"invalid_typed_query_contract_projection:{exc}",
                contract_mode="typed",
                query_contracts=query_contracts,
            )
        if not snapshots:
            first = query_contracts[0]
            return RevenueRowPlan(
                sql_text="",
                query_id=first.query_contract_id,
                required_fields=first.result_shape.required_fields,
                dimension_keys=tuple(
                    item.dimension_id for item in first.dimension_bindings
                ),
                reason="typed_dataset_snapshots_missing",
                contract_mode="typed",
                query_contracts=query_contracts,
            )
        first = query_contracts[0]
        return RevenueRowPlan(
            sql_text="",
            query_id=first.query_contract_id,
            required_fields=first.result_shape.required_fields,
            dimension_keys=tuple(
                item.dimension_id for item in first.dimension_bindings
            ),
            contract_mode="typed",
            query_contracts=query_contracts,
            snapshots=snapshots,
        )

    def _trusted_snapshots(
        self,
        query_contracts: Sequence[QueryContract],
    ) -> dict[str, DatasetSnapshot]:
        if self.snapshot_loader is None:
            return dict(self.snapshots)
        purpose = (
            "context"
            if any(
                contract.query_intent in CONTEXT_SNAPSHOT_QUERY_INTENTS
                for contract in query_contracts
            )
            else "claim"
        )
        loaded = self.snapshot_loader(purpose=purpose)
        return _dataset_snapshots(loaded)

    def fetch(self, plan: RevenueRowPlan) -> RevenueRowsResult:
        if plan.contract_mode == "typed":
            return self._fetch_typed(plan)
        query_plans = plan.query_plans or (
            RevenueQueryPlan(
                sql_text=plan.sql_text,
                query_id=plan.query_id,
                intent=_row_query_intent(plan.query_id, plan.reason),
                required_fields=plan.required_fields,
                dimension_keys=plan.dimension_keys,
                reason=plan.reason,
            ),
        )
        if not any(_query_plan_executable(query_plan) for query_plan in query_plans):
            blocked = _first_blocked_query(query_plans)
            return RevenueRowsResult(
                ok=False,
                reason=blocked.reason or plan.reason or "no_executable_query_spec",
                query_id=blocked.query_id or plan.query_id,
                query_results=tuple(_blocked_query_result(item) for item in query_plans),
            )

        rows_by_intent: dict[str, tuple[dict[str, Any], ...]] = {}
        result_refs_by_intent: dict[str, tuple[str, ...]] = {}
        query_results: list[dict[str, Any]] = []
        result_refs: list[str] = []
        query_hashes: list[str] = []
        for query_plan in query_plans:
            if not _query_plan_executable(query_plan):
                query_results.append(_blocked_query_result(query_plan))
                continue

            validation = validate_select_only(query_plan.sql_text, aggregate=True)
            if not validation.ok:
                query_results.append(
                    _query_result_payload(
                        query_plan,
                        ok=False,
                        reason=validation.reason,
                        query_hash=validation.query_hash,
                    )
                )
                return RevenueRowsResult(
                    ok=False,
                    reason=validation.reason,
                    query_hash=validation.query_hash,
                    query_id=query_plan.query_id,
                    query_results=tuple(query_results),
                )

            result = self.runtime.aggregate(query_plan.sql_text, query_id=query_plan.query_id)
            if not result.ok:
                query_hash = result.query_hash or validation.query_hash
                query_results.append(
                    _query_result_payload(
                        query_plan,
                        ok=False,
                        reason=result.reason,
                        query_hash=query_hash,
                        query_id=result.query_id or query_plan.query_id,
                    )
                )
                return RevenueRowsResult(
                    ok=False,
                    reason=result.reason,
                    query_hash=query_hash,
                    query_id=result.query_id or query_plan.query_id,
                    query_results=tuple(query_results),
                )

            rows = _safe_rows(result.rows, query_plan.required_fields, query_plan.dimension_keys)
            if rows is None:
                query_hash = result.query_hash or validation.query_hash
                query_results.append(
                    _query_result_payload(
                        query_plan,
                        ok=False,
                        reason="invalid_clickhouse_row_shape",
                        query_hash=query_hash,
                        query_id=result.query_id or query_plan.query_id,
                    )
                )
                return RevenueRowsResult(
                    ok=False,
                    reason="invalid_clickhouse_row_shape",
                    query_hash=query_hash,
                    query_id=result.query_id or query_plan.query_id,
                    query_results=tuple(query_results),
                )

            query_hash = result.query_hash or validation.query_hash
            refs = (query_hash,) if query_hash else ()
            rows_by_intent[query_plan.intent] = rows
            result_refs_by_intent[query_plan.intent] = refs
            result_refs.extend(ref for ref in refs if ref)
            if query_hash:
                query_hashes.append(query_hash)
            query_results.append(
                _query_result_payload(
                    query_plan,
                    ok=True,
                    reason="",
                    query_hash=query_hash,
                    query_id=result.query_id or query_plan.query_id,
                    result_refs=refs,
                    row_count=len(rows),
                )
            )

        primary_intent = _row_query_intent(plan.query_id, plan.reason)
        primary_rows = rows_by_intent.get(primary_intent) or _first_rows(rows_by_intent)
        query_hash = _primary_query_hash(
            query_results,
            primary_intent=primary_intent,
            fallback_hashes=tuple(query_hashes),
        )
        return RevenueRowsResult(
            ok=True,
            rows=primary_rows,
            query_hash=query_hash,
            query_id=plan.query_id,
            result_refs=tuple(dict.fromkeys(result_refs)),
            rows_by_intent=rows_by_intent,
            result_refs_by_intent=result_refs_by_intent,
            query_results=tuple(query_results),
            contract_mode="legacy",
        )

    def _fetch_typed(self, plan: RevenueRowPlan) -> RevenueRowsResult:
        if plan.reason or not plan.query_contracts:
            return RevenueRowsResult(
                ok=False,
                reason=plan.reason or "typed_query_contracts_missing",
                query_id=plan.query_id,
                contract_mode="typed",
            )
        envelopes: list[QueryResultEnvelope] = []
        rows_by_intent: dict[str, tuple[dict[str, Any], ...]] = {}
        refs_by_intent: dict[str, tuple[str, ...]] = {}
        query_results: list[dict[str, Any]] = []
        result_refs: list[str] = []
        failure_reasons: list[str] = []
        for contract in plan.query_contracts:
            envelope = self.executor.execute(
                contract,
                plan.snapshots,
                release_resolver=self.release_resolver,
            )
            envelopes.append(envelope)
            query_results.append(
                {
                    **envelope.to_dict(),
                    "intent": contract.query_intent,
                    "ok": envelope.execution_status == "succeeded",
                    "reason": envelope.failure_reason,
                    "result_refs": ((envelope.result_ref,) if envelope.result_ref else ()),
                    "contract_mode": "typed",
                }
            )
            if envelope.execution_status != "succeeded":
                failure_reasons.append(envelope.failure_reason)
                continue
            existing_rows = rows_by_intent.get(contract.query_intent, ())
            rows_by_intent[contract.query_intent] = (
                *existing_rows,
                *(dict(row) for row in envelope.rows),
            )
            existing_refs = refs_by_intent.get(contract.query_intent, ())
            refs_by_intent[contract.query_intent] = (
                *existing_refs,
                envelope.result_ref,
            )
            result_refs.append(envelope.result_ref)

        successful = tuple(
            envelope
            for envelope in envelopes
            if envelope.execution_status == "succeeded"
        )
        primary = successful[0] if successful else envelopes[0]
        return RevenueRowsResult(
            ok=not failure_reasons,
            rows=tuple(dict(row) for row in primary.rows),
            query_hash=primary.query_hash,
            query_id=primary.query_id,
            reason=";".join(dict.fromkeys(failure_reasons)),
            result_refs=tuple(dict.fromkeys(result_refs)),
            rows_by_intent=rows_by_intent,
            result_refs_by_intent=refs_by_intent,
            query_results=tuple(query_results),
            contract_mode="typed",
            query_envelopes=tuple(envelopes),
        )


def _query_plan_from_spec(spec: Mapping[str, Any]) -> RevenueQueryPlan:
    return RevenueQueryPlan(
        sql_text=str(spec.get("sql_text") or ""),
        query_id=str(spec.get("query_id") or ""),
        intent=str(spec.get("intent") or ""),
        required_fields=tuple(str(field) for field in spec.get("required_fields") or ()),
        dimension_keys=tuple(str(key) for key in spec.get("dimension_keys") or ()),
        reason=str(spec.get("reason") or ""),
        claim_use=str(spec.get("claim_use") or ""),
    )


def _describe_field_name(row: Any) -> str:
    if isinstance(row, Mapping):
        for key in ("name", "field", "column", "column_name"):
            value = row.get(key)
            if value not in ("", None):
                return str(value)
        for value in row.values():
            if value not in ("", None):
                return str(value)
        return ""
    if isinstance(row, Sequence) and not isinstance(row, (str, bytes)) and row:
        return str(row[0])
    return ""


def _query_plan_executable(plan: RevenueQueryPlan) -> bool:
    return bool(plan.sql_text) and not plan.reason


def _first_blocked_query(query_plans: Sequence[RevenueQueryPlan]) -> RevenueQueryPlan:
    for query_plan in query_plans:
        if query_plan.reason:
            return query_plan
    return query_plans[0]


def _blocked_query_result(plan: RevenueQueryPlan) -> dict[str, Any]:
    return _query_result_payload(
        plan,
        ok=False,
        reason=plan.reason or "no_executable_sql",
        query_hash="",
    )


def _query_result_payload(
    plan: RevenueQueryPlan,
    *,
    ok: bool,
    reason: str,
    query_hash: str,
    query_id: str | None = None,
    result_refs: tuple[str, ...] = (),
    row_count: int = 0,
) -> dict[str, Any]:
    refs = result_refs or ((query_hash,) if query_hash else ())
    return {
        "query_id": query_id or plan.query_id,
        "intent": plan.intent,
        "ok": ok,
        "reason": reason,
        "query_hash": query_hash,
        "result_refs": refs,
        "row_count": row_count,
        "dimension_keys": plan.dimension_keys,
        "claim_use": plan.claim_use,
        "contract_mode": "legacy",
    }


def _query_contracts(value: Any) -> tuple[QueryContract, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    contracts = []
    for item in value:
        if isinstance(item, QueryContract):
            contracts.append(item)
        elif isinstance(item, Mapping):
            contracts.append(_query_contract_from_mapping(item))
        else:
            raise TypeError("invalid_query_contract_projection")
    return tuple(contracts)


def _query_contract_from_mapping(item: Mapping[str, Any]) -> QueryContract:
    task5_fields = (
        "query_role_ref",
        "reconciliation_binding",
        "join_expectation",
    )
    missing_task5_fields = tuple(
        field for field in task5_fields if field not in item
    )
    if missing_task5_fields:
        raise ValueError(
            "legacy_query_contract_projection_unsupported:missing:"
            + ",".join(missing_task5_fields)
        )
    _require_keys(
        item,
        (
            "query_contract_id",
            "analysis_contract_ref",
            "query_intent",
            "dataset_snapshot_refs",
            "metric_bindings",
            "dimension_bindings",
            "window_refs",
            "resolved_windows",
            "filters",
            "result_shape",
            "completeness_assertions",
            "permission_scope",
            "workload_class",
            "contract_signature",
            "query_parameters",
            "query_role_ref",
            "reconciliation_binding",
            "join_expectation",
        ),
        path="query_contract",
    )
    return QueryContract(
        query_contract_id=_strict_string(
            item["query_contract_id"], path="query_contract.query_contract_id"
        ),
        analysis_contract_ref=_strict_string(
            item["analysis_contract_ref"],
            path="query_contract.analysis_contract_ref",
        ),
        query_intent=_strict_string(
            item["query_intent"], path="query_contract.query_intent"
        ),
        dataset_snapshot_refs=_strict_string_sequence(
            item["dataset_snapshot_refs"],
            path="query_contract.dataset_snapshot_refs",
        ),
        metric_bindings=tuple(
            binding
            if isinstance(binding, MetricBinding)
            else _metric_binding_from_mapping(binding, index=index)
            for index, binding in enumerate(
                _strict_sequence(
                    item["metric_bindings"],
                    path="query_contract.metric_bindings",
                )
            )
        ),
        dimension_bindings=tuple(
            binding
            if isinstance(binding, DimensionBinding)
            else _dimension_binding_from_mapping(binding, index=index)
            for index, binding in enumerate(
                _strict_sequence(
                    item["dimension_bindings"],
                    path="query_contract.dimension_bindings",
                )
            )
        ),
        window_refs=_strict_string_sequence(
            item["window_refs"], path="query_contract.window_refs"
        ),
        resolved_windows=tuple(
            window
            if isinstance(window, ResolvedWindow)
            else _resolved_window_from_mapping(window, index=index)
            for index, window in enumerate(
                _strict_sequence(
                    item["resolved_windows"],
                    path="query_contract.resolved_windows",
                )
            )
        ),
        filters=tuple(
            _filter_from_mapping(filter_item, index=index)
            for index, filter_item in enumerate(
                _strict_sequence(item["filters"], path="query_contract.filters")
            )
        ),
        result_shape=(
            item["result_shape"]
            if isinstance(item["result_shape"], ResultShape)
            else _result_shape_from_mapping(item["result_shape"])
        ),
        completeness_assertions=_strict_string_sequence(
            item["completeness_assertions"],
            path="query_contract.completeness_assertions",
        ),
        permission_scope=_strict_string(
            item["permission_scope"], path="query_contract.permission_scope"
        ),
        workload_class=_strict_string(
            item["workload_class"], path="query_contract.workload_class"
        ),
        contract_signature=_strict_string(
            item["contract_signature"], path="query_contract.contract_signature"
        ),
        query_parameters=dict(
            _strict_mapping(
                item["query_parameters"],
                path="query_contract.query_parameters",
            )
        ),
        query_role_ref=_strict_string(
            item["query_role_ref"],
            path="query_contract.query_role_ref",
            allow_empty=True,
        ),
        reconciliation_binding=_reconciliation_binding_from_value(
            item["reconciliation_binding"]
        ),
        join_expectation=_join_expectation_from_value(item["join_expectation"]),
    )


def _reconciliation_binding_from_value(
    value: Any,
) -> ReconciliationBinding | None:
    if value is None:
        return None
    path = "query_contract.reconciliation_binding"
    item = _strict_mapping(value, path=path)
    _require_keys(
        item,
        ("reference_query_role_ref", "reference_contract_signature"),
        path=path,
    )
    return ReconciliationBinding(
        reference_query_role_ref=_strict_string(
            item["reference_query_role_ref"],
            path=f"{path}.reference_query_role_ref",
        ),
        reference_contract_signature=_strict_string(
            item["reference_contract_signature"],
            path=f"{path}.reference_contract_signature",
        ),
    )


def _join_expectation_from_value(value: Any) -> JoinExpectation | None:
    if value is None:
        return None
    path = "query_contract.join_expectation"
    item = _strict_mapping(value, path=path)
    _require_keys(
        item,
        (
            "cardinality",
            "audit_fields",
            "max_duplicate_keys",
            "max_unmatched_rows",
        ),
        path=path,
    )
    return JoinExpectation(
        cardinality=_strict_string(
            item["cardinality"], path=f"{path}.cardinality"
        ),
        audit_fields=_strict_string_sequence(
            item["audit_fields"], path=f"{path}.audit_fields"
        ),
        max_duplicate_keys=_strict_int(
            item["max_duplicate_keys"], path=f"{path}.max_duplicate_keys"
        ),
        max_unmatched_rows=_strict_int(
            item["max_unmatched_rows"], path=f"{path}.max_unmatched_rows"
        ),
    )


def _metric_binding_from_mapping(value: Any, *, index: int) -> MetricBinding:
    path = f"query_contract.metric_bindings[{index}]"
    item = _strict_mapping(value, path=path)
    _require_keys(
        item,
        (
            "metric_id",
            "contract_ref",
            "dataset_id",
            "expression",
            "aggregation",
            "required_fields",
            "grain",
            "numerator_metric",
            "denominator_metric",
            "zero_denominator_policy",
            "claim_types",
            "reconciliation_tolerance",
            "reconciliation_strategy",
            "value_semantics",
            "display_format",
        ),
        path=path,
    )
    return MetricBinding(
        metric_id=_strict_string(item["metric_id"], path=f"{path}.metric_id"),
        contract_ref=_strict_string(
            item["contract_ref"], path=f"{path}.contract_ref"
        ),
        dataset_id=_strict_string(item["dataset_id"], path=f"{path}.dataset_id"),
        expression=_strict_string(item["expression"], path=f"{path}.expression"),
        aggregation=_strict_string(
            item["aggregation"], path=f"{path}.aggregation"
        ),
        required_fields=_strict_string_sequence(
            item["required_fields"], path=f"{path}.required_fields"
        ),
        grain=_strict_string_sequence(item["grain"], path=f"{path}.grain"),
        numerator_metric=_strict_string(
            item["numerator_metric"],
            path=f"{path}.numerator_metric",
            allow_empty=True,
        ),
        denominator_metric=_strict_string(
            item["denominator_metric"],
            path=f"{path}.denominator_metric",
            allow_empty=True,
        ),
        zero_denominator_policy=_strict_string(
            item["zero_denominator_policy"],
            path=f"{path}.zero_denominator_policy",
        ),
        claim_types=_strict_string_sequence(
            item["claim_types"], path=f"{path}.claim_types"
        ),
        reconciliation_tolerance=_strict_non_negative_number(
            item["reconciliation_tolerance"],
            path=f"{path}.reconciliation_tolerance",
        ),
        reconciliation_strategy=_strict_string(
            item["reconciliation_strategy"],
            path=f"{path}.reconciliation_strategy",
        ),
        value_semantics=_strict_string(
            item["value_semantics"],
            path=f"{path}.value_semantics",
        ),
        display_format=_strict_string(
            item["display_format"],
            path=f"{path}.display_format",
        ),
    )


def _dimension_binding_from_mapping(value: Any, *, index: int) -> DimensionBinding:
    path = f"query_contract.dimension_bindings[{index}]"
    item = _strict_mapping(value, path=path)
    _require_keys(
        item,
        (
            "dimension_id",
            "contract_ref",
            "dataset_id",
            "source_field",
            "allowed_grains",
            "null_bucket",
            "permission_scope",
        ),
        path=path,
    )
    return DimensionBinding(
        dimension_id=_strict_string(
            item["dimension_id"], path=f"{path}.dimension_id"
        ),
        contract_ref=_strict_string(
            item["contract_ref"], path=f"{path}.contract_ref"
        ),
        dataset_id=_strict_string(item["dataset_id"], path=f"{path}.dataset_id"),
        source_field=_strict_string(
            item["source_field"], path=f"{path}.source_field"
        ),
        allowed_grains=_strict_string_sequence(
            item["allowed_grains"], path=f"{path}.allowed_grains"
        ),
        null_bucket=_strict_string(
            item["null_bucket"], path=f"{path}.null_bucket"
        ),
        permission_scope=_strict_string(
            item["permission_scope"], path=f"{path}.permission_scope"
        ),
    )


def _resolved_window_from_mapping(value: Any, *, index: int) -> ResolvedWindow:
    path = f"query_contract.resolved_windows[{index}]"
    item = _strict_mapping(value, path=path)
    required = tuple(ResolvedWindow.__dataclass_fields__)
    _require_keys(item, required, path=path)
    return ResolvedWindow(
        window_id=_strict_string(item["window_id"], path=f"{path}.window_id"),
        role=_strict_string(item["role"], path=f"{path}.role"),
        label=_strict_string(item["label"], path=f"{path}.label"),
        start_inclusive=_strict_string(
            item["start_inclusive"], path=f"{path}.start_inclusive"
        ),
        end_exclusive=_strict_string(
            item["end_exclusive"], path=f"{path}.end_exclusive"
        ),
        timezone=_strict_string(item["timezone"], path=f"{path}.timezone"),
        aggregation=_strict_string(
            item["aggregation"], path=f"{path}.aggregation"
        ),
        required_complete_days=_strict_int(
            item["required_complete_days"],
            path=f"{path}.required_complete_days",
        ),
        source_watermark_requirement=_strict_string(
            item["source_watermark_requirement"],
            path=f"{path}.source_watermark_requirement",
        ),
        membership_policy=_strict_string(
            item["membership_policy"], path=f"{path}.membership_policy"
        ),
    )


def _filter_from_mapping(value: Any, *, index: int) -> dict[str, Any]:
    path = f"query_contract.filters[{index}]"
    item = _strict_mapping(value, path=path)
    _require_keys(
        item,
        ("field", "op"),
        path=path,
        allowed=("field", "op", "value"),
    )
    _strict_string(item["field"], path=f"{path}.field")
    _strict_string(item["op"], path=f"{path}.op")
    return dict(item)


def _result_shape_from_mapping(value: Any) -> ResultShape:
    path = "query_contract.result_shape"
    item = _strict_mapping(value, path=path)
    _require_keys(item, tuple(ResultShape.__dataclass_fields__), path=path)
    return ResultShape(
        required_fields=_strict_string_sequence(
            item["required_fields"], path=f"{path}.required_fields"
        ),
        unique_key=_strict_string_sequence(
            item["unique_key"], path=f"{path}.unique_key"
        ),
        grain=_strict_string_sequence(item["grain"], path=f"{path}.grain"),
        required_window_ids=_strict_string_sequence(
            item["required_window_ids"], path=f"{path}.required_window_ids"
        ),
        result_semantics=_strict_string(
            item["result_semantics"], path=f"{path}.result_semantics"
        ),
        dimension_presence_policy=_strict_string(
            item["dimension_presence_policy"],
            path=f"{path}.dimension_presence_policy",
        ),
    )


def _strict_mapping(value: Any, *, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{path}:mapping_required")
    return value


def _strict_sequence(value: Any, *, path: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{path}:sequence_required")
    return value


def _strict_string_sequence(value: Any, *, path: str) -> tuple[str, ...]:
    return tuple(
        _strict_string(item, path=f"{path}[{index}]")
        for index, item in enumerate(_strict_sequence(value, path=path))
    )


def _strict_string(value: Any, *, path: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError(f"{path}:string_required")
    return value


def _strict_int(value: Any, *, path: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{path}:integer_required")
    return value


def _strict_non_negative_number(value: Any, *, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path}:number_required")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{path}:non_negative_number_required")
    return number


def _require_keys(
    item: Mapping[str, Any],
    required: Sequence[str],
    *,
    path: str,
    allowed: Sequence[str] | None = None,
) -> None:
    missing = tuple(key for key in required if key not in item)
    if missing:
        raise ValueError(f"{path}:missing:{','.join(missing)}")
    accepted = frozenset(allowed or required)
    unexpected = tuple(str(key) for key in item if key not in accepted)
    if unexpected:
        raise ValueError(f"{path}:unexpected:{','.join(unexpected)}")


def _dataset_snapshots(value: Any) -> dict[str, DatasetSnapshot]:
    if isinstance(value, Mapping):
        items = value.items()
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        items = (("", item) for item in value)
    else:
        raise ValueError("dataset_snapshots:mapping_or_sequence_required")
    snapshots = {}
    for index, (key, item) in enumerate(items):
        path = f"dataset_snapshots[{index}]"
        snapshot = (
            item
            if isinstance(item, DatasetSnapshot)
            else _dataset_snapshot_from_mapping(item, path=path)
            if isinstance(item, Mapping)
            else None
        )
        if snapshot is None:
            raise ValueError(f"{path}:snapshot_required")
        mapping_key = str(key or snapshot.snapshot_ref)
        if mapping_key != snapshot.snapshot_ref:
            raise ValueError(f"{path}:snapshot_ref_key_mismatch")
        if mapping_key in snapshots:
            raise ValueError(f"{path}:duplicate_snapshot_ref:{mapping_key}")
        snapshots[mapping_key] = snapshot
    return snapshots


def trusted_active_dataset_snapshots(
    store: Any,
    *,
    dataset_id: str = "",
    purpose: str = "claim",
) -> dict[str, DatasetSnapshot]:
    allowed_evidence = {
        "claim": {"claim_ready"},
        "context": {"claim_ready", "context_only"},
    }
    if purpose not in allowed_evidence:
        raise ValueError(f"dataset_snapshot_purpose_invalid:{purpose}")
    listed = store.list_dataset_snapshots(dataset_id)
    fields = frozenset(DatasetSnapshot.__dataclass_fields__)
    projected = []
    for item in listed:
        if not isinstance(item, Mapping):
            raise ValueError("trusted_dataset_snapshot_mapping_required")
        if item.get("status") != "active":
            continue
        if str(item.get("evidence_state") or "claim_ready") not in allowed_evidence[purpose]:
            continue
        projected.append(
            {
                key: value
                for key, value in item.items()
                if key in fields
            }
        )
    return _dataset_snapshots(tuple(projected))


def _dataset_snapshot_from_mapping(
    value: Mapping[str, Any],
    *,
    path: str,
) -> DatasetSnapshot:
    fields = tuple(DatasetSnapshot.__dataclass_fields__)
    required_fields = fields[:10]
    allowed_fields = (
        *fields,
        "snapshot_id",
        "requires_release",
        "source_load_manifest_ref",
        "runtime_binding_ref",
        "source_checksums",
        "no_data_partitions",
        "no_data_partition_windows",
        "reconciliation",
        "row_count",
        "date_range",
    )
    _require_keys(value, required_fields, path=path, allowed=allowed_fields)
    for field_name in (
        "snapshot_id",
        "source_load_manifest_ref",
        "runtime_binding_ref",
    ):
        if field_name in value:
            _strict_string(value[field_name], path=f"{path}.{field_name}")
    for field_name in ("source_checksums", "reconciliation"):
        if field_name in value:
            _strict_mapping(value[field_name], path=f"{path}.{field_name}")
    for field_name in ("no_data_partitions", "no_data_partition_windows", "date_range"):
        if field_name in value:
            _strict_string_sequence(value[field_name], path=f"{path}.{field_name}")
    if "row_count" in value and (
        isinstance(value["row_count"], bool)
        or not isinstance(value["row_count"], int)
        or value["row_count"] < 0
    ):
        raise ValueError(f"{path}.row_count:non_negative_integer_required")
    for field_name in ("requires_release",):
        if field_name in value and not isinstance(value[field_name], bool):
            raise ValueError(f"{path}.{field_name}:boolean_required")
    return DatasetSnapshot(
        snapshot_ref=_strict_string(
            value["snapshot_ref"], path=f"{path}.snapshot_ref"
        ),
        dataset_id=_strict_string(value["dataset_id"], path=f"{path}.dataset_id"),
        physical_table=_strict_string(
            value["physical_table"], path=f"{path}.physical_table"
        ),
        watermark=_strict_string(value["watermark"], path=f"{path}.watermark"),
        schema_fingerprint=_strict_string(
            value["schema_fingerprint"], path=f"{path}.schema_fingerprint"
        ),
        schema_fields=_strict_string_sequence(
            value["schema_fields"], path=f"{path}.schema_fields"
        ),
        contract_ref=_strict_string(
            value["contract_ref"], path=f"{path}.contract_ref"
        ),
        permission_scopes=_strict_string_sequence(
            value["permission_scopes"], path=f"{path}.permission_scopes"
        ),
        loaded_at=_strict_string(value["loaded_at"], path=f"{path}.loaded_at"),
        status=_strict_string(value["status"], path=f"{path}.status"),
        evidence_state=_strict_string(
            value.get("evidence_state", "claim_ready"),
            path=f"{path}.evidence_state",
        ),
        reconciliation_status=_strict_string(
            value.get("reconciliation_status", "not_applicable"),
            path=f"{path}.reconciliation_status",
        ),
        reconciliation_ref=_strict_string(
            value.get("reconciliation_ref", ""),
            path=f"{path}.reconciliation_ref",
            allow_empty=True,
        ),
        logical_snapshot_id=_strict_string(
            value.get("logical_snapshot_id", ""),
            path=f"{path}.logical_snapshot_id",
            allow_empty=True,
        ),
        load_revision=_strict_string(
            value.get("load_revision", ""),
            path=f"{path}.load_revision",
            allow_empty=True,
        ),
        release_ref=_strict_string(
            value.get("release_ref", ""),
            path=f"{path}.release_ref",
            allow_empty=True,
        ),
        authority_record_ref=_strict_string(
            value.get("authority_record_ref", ""),
            path=f"{path}.authority_record_ref",
            allow_empty=True,
        ),
        rows_content_hash=_strict_string(
            value.get("rows_content_hash", ""),
            path=f"{path}.rows_content_hash",
            allow_empty=True,
        ),
        snapshot_id=_strict_string(
            value.get("snapshot_id", ""),
            path=f"{path}.snapshot_id",
            allow_empty=True,
        ),
        source_load_manifest_ref=_strict_string(
            value.get("source_load_manifest_ref", ""),
            path=f"{path}.source_load_manifest_ref",
            allow_empty=True,
        ),
        runtime_binding_ref=_strict_string(
            value.get("runtime_binding_ref", ""),
            path=f"{path}.runtime_binding_ref",
            allow_empty=True,
        ),
        source_checksums=tuple(
            sorted(
                (str(key), str(checksum))
                for key, checksum in (value.get("source_checksums") or {}).items()
            )
        ),
        row_count=value.get("row_count", -1),
        date_range=_strict_string_sequence(
            value.get("date_range", ()),
            path=f"{path}.date_range",
        ),
        no_data_partitions=_strict_string_sequence(
            value.get("no_data_partitions", ()),
            path=f"{path}.no_data_partitions",
        ),
        no_data_partition_windows=_strict_string_sequence(
            value.get("no_data_partition_windows", ()),
            path=f"{path}.no_data_partition_windows",
        ),
    )


def _request_dataset_snapshots(
    value: Any,
    trusted_snapshots: Mapping[str, DatasetSnapshot],
) -> dict[str, DatasetSnapshot]:
    if value in ((), [], {}, None):
        return {}
    if isinstance(value, Mapping):
        items = value.items()
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        items = (("", item) for item in value)
    else:
        raise ValueError("dataset_snapshots:mapping_or_sequence_required")
    selected: dict[str, DatasetSnapshot] = {}
    allowed = {"snapshot_ref", "dataset_id"}
    for index, (key, item) in enumerate(items):
        path = f"dataset_snapshots[{index}]"
        if not isinstance(item, Mapping):
            raise ValueError(f"{path}:request_selector_mapping_required")
        unexpected = tuple(str(field) for field in item if field not in allowed)
        if unexpected:
            raise ValueError(
                f"untrusted_dataset_snapshot_authority_fields:{','.join(unexpected)}"
            )
        snapshot_ref = _strict_string(
            item.get("snapshot_ref"),
            path=f"{path}.snapshot_ref",
        )
        mapping_key = str(key or snapshot_ref)
        if mapping_key != snapshot_ref:
            raise ValueError(f"{path}:snapshot_ref_key_mismatch")
        snapshot = trusted_snapshots.get(snapshot_ref)
        if snapshot is None:
            raise ValueError(f"dataset_snapshot_provider_missing:{snapshot_ref}")
        dataset_id = str(item.get("dataset_id") or snapshot.dataset_id)
        if dataset_id != snapshot.dataset_id:
            raise ValueError(f"dataset_snapshot_provider_request_conflict:{snapshot_ref}")
        selected[snapshot_ref] = snapshot
    return selected


def _row_query_intent(query_id: str, reason: str = "") -> str:
    if reason:
        return reason
    if ":" in query_id:
        return query_id.rsplit(":", 1)[-1]
    return query_id or "clickhouse_revenue_rows"


def _first_rows(
    rows_by_intent: Mapping[str, tuple[dict[str, Any], ...]]
) -> tuple[dict[str, Any], ...]:
    for rows in rows_by_intent.values():
        return rows
    return ()


def _primary_query_hash(
    query_results: Sequence[Mapping[str, Any]],
    *,
    primary_intent: str,
    fallback_hashes: tuple[str, ...],
) -> str:
    for item in query_results:
        if item.get("intent") == primary_intent and item.get("query_hash"):
            return str(item["query_hash"])
    return fallback_hashes[0] if fallback_hashes else ""


def _dimension_keys(
    accepted_graph: Sequence[str],
    request: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    row_shape = _compiler_row_shape(request or {})
    if row_shape:
        dimensions = row_shape.get("dimension_keys") or ()
        if isinstance(dimensions, Sequence) and not isinstance(dimensions, (str, bytes)):
            return tuple(str(item) for item in dimensions if item)
    graph = set(accepted_graph)
    if "joint_attribution" in graph:
        return JOINT_DIMENSIONS
    if "segment_bridge" in graph:
        return SEGMENT_DIMENSIONS
    return ()


def _required_fields(request: Mapping[str, Any]) -> tuple[str, ...]:
    row_shape = _compiler_row_shape(request)
    if row_shape:
        fields = row_shape.get("required_fields") or ()
        if isinstance(fields, Sequence) and not isinstance(fields, (str, bytes)):
            return tuple(str(item) for item in fields if item)
    return BASE_FIELDS


def _compiler_row_shape(request: Mapping[str, Any]) -> Mapping[str, Any] | None:
    plan = request.get("compiler_runtime_plan")
    if not isinstance(plan, Mapping):
        return None
    shapes = plan.get("row_shapes") or ()
    if not isinstance(shapes, Sequence) or isinstance(shapes, (str, bytes)):
        return None
    for shape in shapes:
        if isinstance(shape, Mapping) and shape.get("source") in (None, "clickhouse"):
            return shape
    return None


def _safe_identifier(value: str) -> bool:
    return isinstance(value, str) and IDENTIFIER_PATTERN.match(value) is not None


def _safe_rows(
    rows: Sequence[Any],
    required_fields: Sequence[str],
    dimension_keys: Sequence[str],
) -> Optional[tuple[dict[str, Any], ...]]:
    allowed = set(required_fields) | set(dimension_keys)
    safe_rows = []
    for row in rows:
        if not isinstance(row, Mapping):
            return None
        safe_rows.append({key: row[key] for key in allowed if key in row})
    return tuple(safe_rows)


def _select_query_spec(
    specs: Sequence[Mapping[str, Any]],
    accepted_graph: Sequence[str],
) -> Mapping[str, Any]:
    graph = set(accepted_graph)
    preferred_intents = _preferred_intents(graph)
    executable_specs = [spec for spec in specs if _query_spec_executable(spec)]
    for intent in preferred_intents:
        matching = [
            spec for spec in executable_specs if str(spec.get("intent") or "") == intent
        ]
        if matching:
            return max(matching, key=lambda spec: len(tuple(spec.get("dimension_keys") or ())))
    if executable_specs:
        return max(executable_specs, key=lambda spec: _fallback_spec_score(spec, graph))
    for intent in preferred_intents:
        matching = [spec for spec in specs if str(spec.get("intent") or "") == intent]
        if matching:
            return max(matching, key=lambda spec: len(tuple(spec.get("dimension_keys") or ())))
    return max(specs, key=lambda spec: _fallback_spec_score(spec, graph))


def _query_spec_executable(spec: Mapping[str, Any]) -> bool:
    return bool(spec.get("sql_text")) and not spec.get("reason")


def _preferred_intents(graph: set[str]) -> tuple[str, ...]:
    if "joint_attribution" in graph:
        return (
            "joint_candidate_scan",
            "dimension_scan",
            "dimension_scan_reuse",
            "daily_metric_baselines",
            "data_quality_probe",
        )
    if "segment_contribution" in graph or "segment_bridge" in graph:
        return (
            "dimension_scan_reuse",
            "dimension_scan",
            "joint_candidate_scan",
            "daily_metric_baselines",
            "data_quality_probe",
        )
    if "event_evidence" in graph:
        if (
            "compare_periods" in graph
            or "rolling_window_compare" in graph
            or "driver_decomposition" in graph
        ):
            return ("daily_metric_baselines", "event_context_probe", "data_quality_probe")
        return ("event_context_probe",)
    if "data_quality_profile" in graph and _metric_analysis_needs_baseline(graph):
        return ("daily_metric_baselines", "data_quality_probe")
    if "data_quality_profile" in graph:
        return ("data_quality_probe",)
    if "compare_periods" in graph or "rolling_window_compare" in graph:
        return ("daily_metric_baselines",)
    return ()


def _metric_analysis_needs_baseline(graph: set[str]) -> bool:
    return bool(
        graph
        & {
            "compare_periods",
            "rolling_window_compare",
            "driver_decomposition",
            "outlier_scan",
            "outlier_contribution",
            "pattern_scan",
        }
    )


def _fallback_spec_score(spec: Mapping[str, Any], graph: set[str]) -> tuple[int, int, int]:
    intent = str(spec.get("intent") or "")
    executable = 1 if spec.get("sql_text") and not spec.get("reason") else 0
    dimension_count = len(tuple(spec.get("dimension_keys") or ()))
    if intent == "daily_metric_baselines":
        intent_score = 3 if ("compare_periods" in graph or "rolling_window_compare" in graph) else 1
    elif intent == "data_quality_probe":
        intent_score = 2 if "data_quality_profile" in graph else 0
    else:
        intent_score = 1 if dimension_count else 0
    return intent_score, executable, dimension_count
