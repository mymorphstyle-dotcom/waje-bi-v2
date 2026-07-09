from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any

from bi_agent.runtime.clickhouse_runtime import IDENTIFIER_PATTERN


MAX_ROWS = 5000
DEFAULT_HISTORY_DAYS = 36
EXECUTABLE_INTENTS = frozenset(
    (
        "daily_metric_baselines",
        "dimension_scan",
        "joint_candidate_scan",
        "data_quality_probe",
    )
)
NON_EXECUTABLE_INTENT_REASONS = {
    "dimension_scan_reuse": "dimension_scan_reuse",
    "event_context_probe": "event_context_probe_unbound",
}
MEASURE_SQL = {
    "amount": "sum(paid_amount_ngn) AS amount",
    "paid_users": "uniqExact(user_id) AS paid_users",
    "orders": "count() AS orders",
    "first_paid_users": "countIf(is_first_payment = '1') AS first_paid_users",
}
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATE_RANGE_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$")


def build_clickhouse_query_specs(
    plan: Mapping[str, Any],
    *,
    table: str,
    run_id: str,
) -> tuple[dict[str, Any], ...]:
    if not _safe_identifier(table):
        return ()
    row_shape = _first_row_shape(plan)
    required_fields = _string_tuple(row_shape.get("required_fields")) or (
        "period",
        "group",
        "amount",
    )
    query_intents = _string_tuple(plan.get("query_intents")) or ("daily_metric_baselines",)
    history_days = _history_days(plan)
    group_expression, where_clause, reason = _query_window_context(
        plan,
        baselines=_string_tuple(plan.get("baselines")),
        history_days=history_days,
    )
    specs: list[dict[str, Any]] = []
    for intent in query_intents:
        if intent in NON_EXECUTABLE_INTENT_REASONS:
            specs.append(
                _blocked_spec(
                    run_id=run_id,
                    intent=intent,
                    required_fields=required_fields,
                    dimension_keys=_intent_dimension_keys(intent, plan, row_shape),
                    reason=NON_EXECUTABLE_INTENT_REASONS[intent],
                )
            )
            continue
        if intent not in EXECUTABLE_INTENTS:
            specs.append(
                _blocked_spec(
                    run_id=run_id,
                    intent=intent,
                    required_fields=required_fields,
                    dimension_keys=_intent_dimension_keys(intent, plan, row_shape),
                    reason="unsupported_query_intent",
                )
            )
            continue
        if reason:
            specs.append(
                _blocked_spec(
                    run_id=run_id,
                    intent=intent,
                    required_fields=required_fields,
                    dimension_keys=_intent_dimension_keys(intent, plan, row_shape),
                    reason=reason,
                )
            )
            continue
        if intent == "daily_metric_baselines":
            spec = _grouped_metric_query(
                table=table,
                run_id=run_id,
                intent=intent,
                required_fields=required_fields,
                dimension_keys=(),
                group_expression=group_expression,
                where_clause=where_clause,
                claim_use="baseline_metric",
            )
        elif intent == "dimension_scan":
            spec = _grouped_metric_query(
                table=table,
                run_id=run_id,
                intent=intent,
                required_fields=required_fields,
                dimension_keys=_dimension_keys(plan, row_shape),
                group_expression=group_expression,
                where_clause=where_clause,
                claim_use="segment_or_factor_attribution",
            )
        elif intent == "joint_candidate_scan":
            spec = _grouped_metric_query(
                table=table,
                run_id=run_id,
                intent=intent,
                required_fields=required_fields,
                dimension_keys=_joint_dimension_keys(plan, row_shape),
                group_expression=group_expression,
                where_clause=where_clause,
                claim_use="joint_attribution_candidates",
            )
        else:
            spec = _data_quality_probe(
                table=table,
                run_id=run_id,
                group_expression=group_expression,
                where_clause=where_clause,
            )
        if spec:
            specs.append(spec)
    return tuple(specs)


def _grouped_metric_query(
    *,
    table: str,
    run_id: str,
    intent: str,
    required_fields: tuple[str, ...],
    dimension_keys: tuple[str, ...],
    group_expression: str,
    where_clause: str,
    claim_use: str,
) -> dict[str, Any] | None:
    raw_dimensions = tuple(str(key) for key in dimension_keys if key)
    safe_dimensions = tuple(key for key in raw_dimensions if _safe_identifier(key))
    if intent != "daily_metric_baselines":
        if not raw_dimensions:
            return _blocked_spec(
                run_id=run_id,
                intent=intent,
                required_fields=required_fields,
                dimension_keys=(),
                reason="missing_dimension_keys",
            )
        if len(safe_dimensions) != len(raw_dimensions):
            return _blocked_spec(
                run_id=run_id,
                intent=intent,
                required_fields=required_fields,
                dimension_keys=(),
                reason="unsafe_dimension_keys",
            )

    select_parts = [
        "business_date_lagos AS period",
        group_expression,
    ]
    select_parts.extend(safe_dimensions)
    select_parts.extend(_measure_sql(required_fields))
    group_by_parts = ["period", "group", *safe_dimensions]
    return {
        "query_id": f"{run_id}:{intent}",
        "intent": intent,
        "sql_text": "\n".join(
            (
                "SELECT",
                _indented(select_parts),
                f"FROM {table}",
                where_clause,
                f"GROUP BY {', '.join(group_by_parts)}",
                f"LIMIT {MAX_ROWS}",
            )
        ),
        "required_fields": required_fields,
        "dimension_keys": safe_dimensions,
        "claim_use": claim_use,
        "reason": "",
    }


def _data_quality_probe(
    *,
    table: str,
    run_id: str,
    group_expression: str,
    where_clause: str,
) -> dict[str, Any]:
    return {
        "query_id": f"{run_id}:data_quality_probe",
        "intent": "data_quality_probe",
        "sql_text": "\n".join(
            (
                "SELECT",
                _indented(
                    (
                        "business_date_lagos AS period",
                        group_expression,
                        "min(business_date_lagos) AS min_period",
                        "max(business_date_lagos) AS max_period",
                        "uniqExact(user_id) AS paid_users",
                        "count() AS orders",
                    )
                ),
                f"FROM {table}",
                where_clause,
                "GROUP BY period, group",
                "LIMIT 1",
            )
        ),
        "required_fields": ("period", "group", "orders", "paid_users", "min_period", "max_period"),
        "dimension_keys": (),
        "claim_use": "data_quality_context",
        "reason": "",
    }


def _measure_sql(required_fields: Sequence[str]) -> tuple[str, ...]:
    fields = []
    for field in required_fields:
        expression = MEASURE_SQL.get(str(field))
        if expression and expression not in fields:
            fields.append(expression)
    if fields:
        return tuple(fields)
    return (MEASURE_SQL["amount"],)


def _blocked_spec(
    *,
    run_id: str,
    intent: str,
    required_fields: tuple[str, ...],
    dimension_keys: tuple[str, ...],
    reason: str,
) -> dict[str, Any]:
    return {
        "query_id": f"{run_id}:{intent}",
        "intent": intent,
        "sql_text": "",
        "required_fields": required_fields,
        "dimension_keys": tuple(key for key in dimension_keys if _safe_identifier(key)),
        "claim_use": "",
        "reason": reason,
    }


def _query_window_context(
    plan: Mapping[str, Any],
    *,
    baselines: tuple[str, ...],
    history_days: int,
) -> tuple[str, str, str]:
    if "custom_baseline" not in set(baselines):
        return _group_expression(baselines), _where_clause(history_days), ""
    target_range = _window_date_range(plan, "target")
    baseline_range = _window_date_range(plan, "baseline")
    if not target_range or not baseline_range:
        return "", "", "custom_baseline_window_unbound"
    target_predicate = _date_range_predicate(*target_range)
    baseline_predicate = _date_range_predicate(*baseline_range)
    return (
        "multiIf(\n"
        + _indented(
            (
                f"{target_predicate}, 'target'",
                f"{baseline_predicate}, 'baseline'",
                "'history'",
            )
        )
        + "\n) AS group",
        "WHERE (\n"
        + _indented((target_predicate, f"OR {baseline_predicate}"))
        + "\n)",
        "",
    )


def _group_expression(baselines: Sequence[str]) -> str:
    clauses = ["business_date_lagos = toDate(now('Africa/Lagos')) - 1, 'target'"]
    baseline_set = {str(item) for item in baselines if item}
    if "previous_day" in baseline_set:
        clauses.append("business_date_lagos = toDate(now('Africa/Lagos')) - 2, 'previous_day'")
    if "same_weekday_last_week" in baseline_set:
        clauses.append(
            "business_date_lagos = toDate(now('Africa/Lagos')) - 8, 'same_weekday_last_week'"
        )
    if "rolling_7_day_baseline" in baseline_set:
        clauses.append(
            "business_date_lagos BETWEEN toDate(now('Africa/Lagos')) - 8 "
            "AND toDate(now('Africa/Lagos')) - 2, 'rolling_7_day_baseline'"
        )
    clauses.append("'history'")
    return "multiIf(\n" + _indented(clauses) + "\n) AS group"


def _where_clause(history_days: int) -> str:
    bounded_history = max(history_days, 8)
    return (
        "WHERE business_date_lagos >= toDate(now('Africa/Lagos')) - "
        f"{bounded_history}\n"
        "  AND business_date_lagos <= toDate(now('Africa/Lagos')) - 1"
    )


def _window_date_range(
    plan: Mapping[str, Any],
    prefix: str,
) -> tuple[str, str] | None:
    windows = plan.get("windows")
    if not isinstance(windows, Mapping):
        return None
    start = _date_literal(windows.get(f"{prefix}_start"))
    end = _date_literal(windows.get(f"{prefix}_end"))
    if start and end:
        return start, end
    value = windows.get(prefix)
    if isinstance(value, Mapping):
        start = _date_literal(value.get("start"))
        end = _date_literal(value.get("end"))
        if start and end:
            return start, end
        return None
    if not isinstance(value, str):
        return None
    match = DATE_RANGE_PATTERN.match(value.strip())
    if match is None:
        return None
    return match.group(1), match.group(2)


def _date_literal(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    stripped = value.strip()
    if DATE_PATTERN.match(stripped) is None:
        return ""
    return stripped


def _date_range_predicate(start: str, end: str) -> str:
    return f"business_date_lagos BETWEEN toDate('{start}') AND toDate('{end}')"


def _dimension_keys(plan: Mapping[str, Any], row_shape: Mapping[str, Any]) -> tuple[str, ...]:
    dimensions = _string_tuple(row_shape.get("dimension_keys"))
    if dimensions:
        return dimensions
    candidates = plan.get("dimension_candidates") or ()
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        return ()
    return tuple(
        str(item.get("field"))
        for item in candidates
        if isinstance(item, Mapping) and item.get("field") and item.get("required")
    )


def _joint_dimension_keys(plan: Mapping[str, Any], row_shape: Mapping[str, Any]) -> tuple[str, ...]:
    max_dimension_count = 2
    capability_params = plan.get("capability_params")
    if isinstance(capability_params, Mapping):
        joint_params = capability_params.get("joint_attribution")
        if isinstance(joint_params, Mapping):
            raw_count = joint_params.get("max_dimension_count")
            if isinstance(raw_count, int) and raw_count > 0:
                max_dimension_count = raw_count
    return _dimension_keys(plan, row_shape)[:max_dimension_count]


def _intent_dimension_keys(
    intent: str,
    plan: Mapping[str, Any],
    row_shape: Mapping[str, Any],
) -> tuple[str, ...]:
    if intent == "joint_candidate_scan":
        return _joint_dimension_keys(plan, row_shape)
    if intent in ("dimension_scan", "dimension_scan_reuse"):
        return _dimension_keys(plan, row_shape)
    return ()


def _history_days(plan: Mapping[str, Any]) -> int:
    windows = plan.get("windows")
    if isinstance(windows, Mapping):
        raw_value = windows.get("history_days")
        if isinstance(raw_value, int) and raw_value > 0:
            return raw_value
        if isinstance(raw_value, str) and raw_value.isdigit():
            return int(raw_value)
    return DEFAULT_HISTORY_DAYS


def _first_row_shape(plan: Mapping[str, Any]) -> Mapping[str, Any]:
    row_shapes = plan.get("row_shapes") or ()
    if not isinstance(row_shapes, Sequence) or isinstance(row_shapes, (str, bytes)):
        return {}
    for row_shape in row_shapes:
        if isinstance(row_shape, Mapping) and row_shape.get("source") in (None, "clickhouse"):
            return row_shape
    return {}


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(str(item) for item in value if item)


def _safe_identifier(value: str) -> bool:
    return isinstance(value, str) and IDENTIFIER_PATTERN.match(value) is not None


def _indented(parts: Sequence[str]) -> str:
    return ",\n".join(f"    {part}" for part in parts)
