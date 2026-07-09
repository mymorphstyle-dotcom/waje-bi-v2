from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, Mapping, Optional, Sequence

from bi_agent.runtime.clickhouse_query_planner import build_clickhouse_query_specs
from bi_agent.runtime.clickhouse_runtime import ClickHouseRuntime, IDENTIFIER_PATTERN
from bi_agent.runtime.sql_safety import validate_select_only


DEFAULT_TABLE = "paid_order_success_clean_20240101_20260704"
MAX_ROWS = 5000
BASE_FIELDS = ("period", "group", "amount", "paid_users", "orders", "first_paid_users")
JOINT_DIMENSIONS = ("channel", "payment_method", "region", "device_brand")
SEGMENT_DIMENSIONS = ("channel",)


@dataclass(frozen=True)
class RevenueRowPlan:
    sql_text: str
    query_id: str
    required_fields: tuple[str, ...]
    dimension_keys: tuple[str, ...]
    reason: str = ""


@dataclass(frozen=True)
class RevenueRowsResult:
    ok: bool
    rows: tuple[dict[str, Any], ...] = ()
    reason: str = ""
    query_hash: str = ""
    query_id: str = ""
    result_refs: tuple[str, ...] = ()


class ClickHouseRevenueRows:
    def __init__(
        self, runtime: Optional[ClickHouseRuntime] = None, table: Optional[str] = None
    ) -> None:
        self.runtime = runtime or ClickHouseRuntime.from_env()
        self.table = table or os.environ.get("WAJE_CLICKHOUSE_PAYMENT_TABLE", DEFAULT_TABLE)

    @classmethod
    def from_env(cls) -> "ClickHouseRevenueRows":
        return cls(ClickHouseRuntime.from_env())

    def configured(self) -> bool:
        return self.runtime.configured()

    def binding_reason(self) -> str:
        return self.runtime.binding.reason

    def plan(
        self,
        request: Mapping[str, Any],
        intent: Mapping[str, Any],
        accepted_graph: Sequence[str],
    ) -> RevenueRowPlan:
        compiler_runtime_plan = request.get("compiler_runtime_plan")
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
                return RevenueRowPlan(
                    sql_text=str(first["sql_text"]),
                    query_id=str(first["query_id"]),
                    required_fields=tuple(str(field) for field in first["required_fields"]),
                    dimension_keys=tuple(str(key) for key in first["dimension_keys"]),
                    reason=str(first.get("reason") or ""),
                )
            return RevenueRowPlan(
                sql_text="",
                query_id=f"{request.get('run_id', 'run')}:compiler_runtime_plan",
                required_fields=_required_fields(request),
                dimension_keys=_dimension_keys(accepted_graph, request),
                reason="invalid_identifier" if not _safe_identifier(self.table) else "no_executable_query_spec",
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
        )

    def fetch(self, plan: RevenueRowPlan) -> RevenueRowsResult:
        if plan.reason:
            return RevenueRowsResult(ok=False, reason=plan.reason, query_id=plan.query_id)
        validation = validate_select_only(plan.sql_text, aggregate=True)
        if not validation.ok:
            return RevenueRowsResult(
                ok=False,
                reason=validation.reason,
                query_hash=validation.query_hash,
                query_id=plan.query_id,
            )

        result = self.runtime.aggregate(plan.sql_text, query_id=plan.query_id)
        if not result.ok:
            return RevenueRowsResult(
                ok=False,
                reason=result.reason,
                query_hash=result.query_hash or validation.query_hash,
                query_id=result.query_id or plan.query_id,
            )

        rows = _safe_rows(result.rows, plan.required_fields, plan.dimension_keys)
        if rows is None:
            return RevenueRowsResult(
                ok=False,
                reason="invalid_clickhouse_row_shape",
                query_hash=result.query_hash or validation.query_hash,
                query_id=result.query_id or plan.query_id,
            )
        query_hash = result.query_hash or validation.query_hash
        return RevenueRowsResult(
            ok=True,
            rows=rows,
            query_hash=query_hash,
            query_id=result.query_id or plan.query_id,
            result_refs=(query_hash,) if query_hash else (),
        )


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
        return ("joint_candidate_scan", "dimension_scan", "dimension_scan_reuse")
    if "segment_contribution" in graph or "segment_bridge" in graph:
        return ("dimension_scan_reuse", "dimension_scan", "joint_candidate_scan")
    if "event_evidence" in graph:
        if (
            "compare_periods" in graph
            or "rolling_window_compare" in graph
            or "driver_decomposition" in graph
        ):
            return ("daily_metric_baselines", "event_context_probe")
        return ("event_context_probe",)
    if "data_quality_profile" in graph:
        return ("data_quality_probe",)
    if "compare_periods" in graph or "rolling_window_compare" in graph:
        return ("daily_metric_baselines",)
    return ()


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
