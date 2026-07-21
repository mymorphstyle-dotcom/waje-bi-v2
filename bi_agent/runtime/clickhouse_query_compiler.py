from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
import math
from pathlib import Path
import re
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bi_agent.runtime.analysis_contracts import (
    DIMENSION_PRESENCE_POLICIES,
    DimensionBinding,
    JoinExpectation,
    MetricBinding,
    QueryContract,
    ReconciliationBinding,
    ResolvedWindow,
    ResultShape,
    query_contract_signature,
)
from bi_agent.runtime.dataset_catalog import (
    DatasetReleaseResolver,
    DatasetSnapshot,
    dataset_release_authority_integrity_errors,
    snapshot_matches_release_authority,
)
from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry


_RUNTIME_BINDINGS_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "runtime"
    / "clickhouse-analysis-bindings.yaml"
)
_PHYSICAL_IDENTIFIER = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?$"
)
_LOGICAL_IDENTIFIER = re.compile(r"^[^\W\d]\w*$", re.UNICODE)
_AGGREGATE_EXPRESSION = re.compile(
    r"\b(?:avg|count|countIf|max|min|nullIf|quantile|quantileExact|"
    r"sum|sumIf|uniq|uniqExact|uniqExactIf)\s*\(",
    re.IGNORECASE,
)
_STRUCTURAL_KEYWORDS = frozenset(
    {
        "alter",
        "array",
        "attach",
        "create",
        "delete",
        "detach",
        "drop",
        "format",
        "from",
        "grant",
        "insert",
        "join",
        "kill",
        "outfile",
        "revoke",
        "select",
        "settings",
        "system",
        "truncate",
        "union",
        "update",
        "with",
    }
)
_METRIC_FUNCTIONS = frozenset(
    {
        "avg",
        "count",
        "countif",
        "max",
        "min",
        "nullif",
        "sum",
        "sumif",
        "uniqexact",
        "uniqexactif",
    }
)
_DATE_FUNCTIONS = frozenset(
    {
        "todate",
        "totimezone",
        "fromunixtimestamp64milli",
        "toint64orzero",
    }
)


_WINDOW_AGGREGATE_QUERY_INTENTS = frozenset(
    {
        "component_driver_scan",
        "dimension_contribution_scan",
        "joint_candidate_scan",
        "payment_success_scan",
    }
)
_WINDOW_AGGREGATE_NORMALIZED_METRIC_KINDS = frozenset(
    {"sum", "distinct_count", "distinct_count_if"}
)


@dataclass(frozen=True)
class CompiledQuery:
    sql_text: str
    parameters: Mapping[str, Any]
    settings: Mapping[str, Any]
    query_contract_ref: str
    max_context_rows: int = 0


def compile_clickhouse_query(
    contract: QueryContract,
    snapshots: Mapping[str, DatasetSnapshot],
    *,
    registry: RuntimeContractRegistry | None = None,
    release_resolver: DatasetReleaseResolver | None = None,
) -> CompiledQuery:
    return _compile_clickhouse_query_with_registry(
        contract,
        snapshots,
        registry=registry or _runtime_registry(),
        release_resolver=release_resolver,
    )


def validate_clickhouse_query_contract(
    contract: QueryContract,
    snapshots: Mapping[str, DatasetSnapshot],
    *,
    registry: RuntimeContractRegistry,
    release_resolver: DatasetReleaseResolver | None = None,
) -> None:
    """Validate one persisted query against an explicitly trusted registry."""
    _compile_clickhouse_query_with_registry(
        contract,
        snapshots,
        registry=registry,
        release_resolver=release_resolver,
    )


def _compile_clickhouse_query_with_registry(
    contract: QueryContract,
    snapshots: Mapping[str, DatasetSnapshot],
    *,
    registry: RuntimeContractRegistry,
    release_resolver: DatasetReleaseResolver | None,
) -> CompiledQuery:
    _validate_runtime_types(contract, snapshots)
    _verify_contract_signature(contract)
    _verify_window_consistency(contract)
    _verify_reviewed_query_shape(contract, registry)
    snapshot = _single_snapshot(contract, snapshots)
    _verify_dataset_snapshot_binding(
        snapshot,
        registry=registry,
        release_resolver=release_resolver,
    )
    date_expression = _date_expression(snapshot, registry=registry)
    _verify_reviewed_bindings(contract, snapshot, registry=registry)
    parameters = _window_parameters(contract.resolved_windows)
    filter_sql, filter_parameters = _compile_filters(
        contract.filters,
        snapshot,
        registry=registry,
    )
    parameters.update(filter_parameters)
    physical_filters, physical_parameters = _physical_snapshot_filters(
        snapshot,
        requires_physical_revision=bool(
            registry.dataset(snapshot.dataset_id).get("requires_physical_revision")
        ),
    )
    filter_sql = (*filter_sql, *physical_filters)
    parameters.update(physical_parameters)

    if contract.query_intent == "event_context_probe":
        query_shape = registry.query_shape(contract.query_intent)
        context_row_bound = query_shape.get("max_context_rows")
        if (
            isinstance(context_row_bound, bool)
            or not isinstance(context_row_bound, int)
            or context_row_bound <= 0
        ):
            raise ValueError(
                f"reviewed_context_row_bound_invalid:{contract.query_intent}"
            )
        source_fields = query_shape.get("source_fields")
        if (
            not isinstance(source_fields, list)
            or not source_fields
            or any(type(field) is not str or not field for field in source_fields)
        ):
            raise ValueError(f"reviewed_source_fields_invalid:{contract.query_intent}")
        sql_text = _compile_event_context_query(
            contract,
            snapshot,
            filter_sql=filter_sql,
            max_context_rows=context_row_bound,
            required_fields=tuple(source_fields),
        )
    elif contract.query_intent == "high_value_scan":
        _verify_high_value_semantics(contract)
        parameters["threshold_quantile"] = contract.query_parameters[
            "threshold_quantile"
        ]
        sql_text = _compile_high_value_query(
            contract,
            snapshot,
            date_expression=date_expression,
            filter_sql=filter_sql,
        )
    elif contract.query_intent in _WINDOW_AGGREGATE_QUERY_INTENTS:
        sql_text = _compile_window_aggregate_query(
            contract,
            snapshot,
            date_expression=date_expression,
            filter_sql=filter_sql,
            parameters=parameters,
        )
    else:
        sql_text = _compile_grouped_query(
            contract,
            snapshot,
            date_expression=date_expression,
            filter_sql=filter_sql,
            parameters=parameters,
        )

    settings = {
        "result_overflow_mode": "throw",
        "readonly": 2,
        # Metric contracts describe physical source columns.  Prefer those
        # columns when an output metric alias has the same name, otherwise
        # ClickHouse can substitute the alias inside a sibling aggregate and
        # turn a valid derived ratio into a nested aggregate.
        "prefer_column_name_to_alias": 1,
    }
    if contract.result_shape.result_semantics == "complete_context_rows":
        reviewed_shape = registry.query_shape(contract.query_intent)
        max_context_rows = reviewed_shape.get("max_context_rows")
        if (
            isinstance(max_context_rows, bool)
            or not isinstance(max_context_rows, int)
            or max_context_rows <= 0
        ):
            raise ValueError(
                f"reviewed_context_row_bound_invalid:{contract.query_intent}"
            )
        settings["max_result_rows"] = max_context_rows + 1
    if contract.join_expectation is not None:
        settings["join_use_nulls"] = 1
    return CompiledQuery(
        sql_text=sql_text,
        parameters=parameters,
        settings=settings,
        query_contract_ref=contract.query_contract_id,
        max_context_rows=(
            context_row_bound
            if contract.result_shape.result_semantics == "complete_context_rows"
            else 0
        ),
    )


def _verify_high_value_semantics(contract: QueryContract) -> None:
    if contract.dimension_bindings:
        raise ValueError("high_value_dimension_bindings_unsupported")
    threshold_reference = contract.query_parameters.get("threshold_reference")
    if threshold_reference != "within_window_user_paid_amount":
        raise ValueError(
            f"high_value_threshold_reference_unsupported:{threshold_reference}"
        )
    aggregation_grain = _string_tuple(
        contract.query_parameters.get("aggregation_grain")
    )
    supported_grain = ("window_id", "user_id")
    if aggregation_grain != supported_grain:
        raise ValueError(
            "high_value_aggregation_grain_unsupported:" + ",".join(aggregation_grain)
        )
    threshold_quantile = contract.query_parameters.get("threshold_quantile")
    if (
        isinstance(threshold_quantile, bool)
        or not isinstance(threshold_quantile, (int, float))
        or not 0 < threshold_quantile < 1
    ):
        raise ValueError(f"high_value_threshold_quantile_invalid:{threshold_quantile}")


def _compile_grouped_query(
    contract: QueryContract,
    snapshot: DatasetSnapshot,
    *,
    date_expression: str,
    filter_sql: tuple[str, ...],
    parameters: dict[str, Any],
) -> str:
    dimensions = _dimension_selects(
        contract,
        snapshot,
        parameters=parameters,
    )
    metrics = _metric_selects(contract, snapshot)
    intent_selects, intent_groups = _intent_selects(
        contract.query_intent,
        date_expression=date_expression,
    )
    select_parts = (
        "tupleElement(analysis_window, 1) AS `window_id`",
        "tupleElement(analysis_window, 2) AS `window_role`",
        f"toString({date_expression}) AS `observation_key`",
        *(item[0] for item in dimensions),
        *intent_selects,
        *metrics,
    )
    if not metrics and contract.query_intent not in {
        "event_context_probe",
        "data_quality_probe",
    }:
        raise ValueError(f"query_contract_metrics_required:{contract.query_intent}")

    group_parts = (
        "`window_id`",
        "`window_role`",
        "`observation_key`",
        *(item[1] for item in dimensions),
        *intent_groups,
    )
    predicates = _window_predicates(date_expression, filter_sql)
    return "\n".join(
        (
            f"WITH [{_window_tuples(contract.resolved_windows)}] AS analysis_windows",
            "SELECT",
            _indented(select_parts),
            f"FROM {_quote_physical_table(snapshot.physical_table)}",
            "ARRAY JOIN analysis_windows AS analysis_window",
            "WHERE " + "\n  AND ".join(predicates),
            "GROUP BY " + ", ".join(group_parts),
        )
    )


def _compile_window_aggregate_query(
    contract: QueryContract,
    snapshot: DatasetSnapshot,
    *,
    date_expression: str,
    filter_sql: tuple[str, ...],
    parameters: dict[str, Any],
) -> str:
    """Compile exact window-grain metrics while retaining complete-day audit.

    Formula, contribution, and payment-rate capabilities consume one metric
    aggregate per resolved window (and per requested dimension member).  Their
    distinct counts and ratios cannot be reconstructed from daily result rows.
    The companion ``source_complete_days`` field keeps physical day coverage
    independently verifiable without changing the business metric grain.
    """

    if any(
        window.aggregation
        not in {
            "daily_total",
            "sum_of_complete_days",
            "mean_of_complete_days",
        }
        for window in contract.resolved_windows
    ):
        raise ValueError(
            f"window_aggregate_aggregation_unsupported:{contract.query_intent}"
        )
    dimensions = _dimension_selects(
        contract,
        snapshot,
        parameters=parameters,
    )
    metrics = _window_aggregate_metric_selects(contract)
    if not metrics:
        raise ValueError(f"query_contract_metrics_required:{contract.query_intent}")
    predicates = _window_predicates(date_expression, filter_sql)
    aggregate_selects = (
        "`__window_id` AS `window_id`",
        "`__window_role` AS `window_role`",
        "`__window_id` AS `observation_key`",
        *(item[0] for item in dimensions),
        *metrics,
    )
    aggregate_groups = (
        "`__window_id`",
        "`__window_role`",
        "`__window_aggregation`",
        "`__window_start`",
        "`__window_end`",
        *(item[1] for item in dimensions),
    )
    return "\n".join(
        (
            f"WITH [{_window_tuples(contract.resolved_windows)}] AS analysis_windows,",
            "matched_rows AS (",
            "  SELECT",
            _indented(
                (
                    "source.*",
                    "tupleElement(analysis_window, 1) AS `__window_id`",
                    "tupleElement(analysis_window, 2) AS `__window_role`",
                    "tupleElement(analysis_window, 3) AS `__window_start`",
                    "tupleElement(analysis_window, 4) AS `__window_end`",
                    "tupleElement(analysis_window, 5) AS `__window_aggregation`",
                    f"{date_expression} AS `__observation_date`",
                ),
                spaces=4,
            ),
            f"  FROM {_quote_physical_table(snapshot.physical_table)} AS source",
            "  ARRAY JOIN analysis_windows AS analysis_window",
            "  WHERE " + "\n    AND ".join(predicates),
            "),",
            "window_coverage AS (",
            "  SELECT",
            "    `__window_id` AS `window_id`,",
            "    `__window_role` AS `window_role`,",
            "    uniqExact(`__observation_date`) AS `source_complete_days`",
            "  FROM matched_rows",
            "  GROUP BY `__window_id`, `__window_role`",
            "),",
            "window_aggregates AS (",
            "  SELECT",
            _indented(aggregate_selects, spaces=4),
            "  FROM matched_rows",
            "  GROUP BY " + ", ".join(aggregate_groups),
            ")",
            "SELECT",
            "  window_aggregates.* ,",
            "  window_coverage.`source_complete_days` AS `source_complete_days`",
            "FROM window_aggregates",
            "INNER JOIN window_coverage USING (`window_id`, `window_role`)",
        )
    )


def _window_aggregate_metric_selects(
    contract: QueryContract,
) -> tuple[str, ...]:
    selected = []
    seen: set[str] = set()
    complete_days = "dateDiff('day', `__window_start`, `__window_end`)"
    for binding in contract.metric_bindings:
        if binding.metric_id in seen:
            raise ValueError(f"duplicate_metric_binding:{binding.metric_id}")
        seen.add(binding.metric_id)
        if binding.aggregation in _WINDOW_AGGREGATE_NORMALIZED_METRIC_KINDS:
            normalized_expression = f"toFloat64({binding.expression})"
            expression = (
                "if(`__window_aggregation` = 'mean_of_complete_days', "
                f"{normalized_expression} / nullIf({complete_days}, 0), "
                f"{normalized_expression})"
            )
        elif binding.aggregation == "ratio":
            expression = binding.expression
        else:
            raise ValueError(
                "window_aggregate_metric_aggregation_unsupported:"
                f"{binding.metric_id}:{binding.aggregation}"
            )
        selected.append(f"{expression} AS {_quote_identifier(binding.metric_id)}")
    return tuple(selected)


def _compile_event_context_query(
    contract: QueryContract,
    snapshot: DatasetSnapshot,
    *,
    filter_sql: tuple[str, ...],
    max_context_rows: int,
    required_fields: tuple[str, ...],
) -> str:
    if contract.metric_bindings or contract.dimension_bindings:
        raise ValueError("event_context_probe_bindings_unsupported")
    missing = tuple(
        field for field in required_fields if field not in snapshot.schema_fields
    )
    if missing:
        raise ValueError("event_context_fields_missing:" + ",".join(missing))
    window_id = "tupleElement(analysis_window, 1)"
    matched_selects = (
        f"{window_id} AS `window_id`",
        "tupleElement(analysis_window, 2) AS `window_role`",
        "`event_id` AS `observation_key`",
        "toUInt64(1) AS `event_count`",
        "`source_family` AS `source_family`",
        "`event_id` AS `event_id`",
        "`event_type` AS `event_type`",
        "`event_start_date` AS `event_start_date`",
        "`event_end_date` AS `event_end_date`",
        "`affected_scope` AS `affected_scope`",
        "`authority` AS `authority`",
        "`evidence_level` AS `evidence_level`",
        "`wording_limit` AS `wording_limit`",
        "`recurrence_kind` AS `recurrence_kind`",
        "`recurrence_month_start` AS `recurrence_month_start`",
        "`recurrence_day_start` AS `recurrence_day_start`",
        "`recurrence_month_end` AS `recurrence_month_end`",
        "`recurrence_day_end` AS `recurrence_day_end`",
        "`payload` AS `payload`",
    )
    overlap_and_filters = (
        "`event_start_date` < tupleElement(analysis_window, 4)",
        "`event_end_date` >= tupleElement(analysis_window, 3)",
        _event_recurrence_overlap_predicate(),
        *filter_sql,
    )
    sentinel_selects = (
        f"{window_id} AS `window_id`",
        "tupleElement(analysis_window, 2) AS `window_role`",
        f"concat('__no_event__:', {window_id}) AS `observation_key`",
        "toUInt64(0) AS `event_count`",
        "'' AS `source_family`",
        f"concat('__no_event__:', {window_id}) AS `event_id`",
        "'' AS `event_type`",
        "CAST(NULL AS Nullable(Date)) AS `event_start_date`",
        "CAST(NULL AS Nullable(Date)) AS `event_end_date`",
        "'' AS `affected_scope`",
        "'' AS `authority`",
        "'' AS `evidence_level`",
        "'context' AS `wording_limit`",
        "'' AS `recurrence_kind`",
        "toUInt8(0) AS `recurrence_month_start`",
        "toUInt8(0) AS `recurrence_day_start`",
        "toUInt8(0) AS `recurrence_month_end`",
        "toUInt8(0) AS `recurrence_day_end`",
        "'{}' AS `payload`",
    )
    return "\n".join(
        (
            f"WITH [{_window_tuples(contract.resolved_windows)}] AS analysis_windows,",
            "matched_events AS (",
            "SELECT",
            _indented(matched_selects),
            f"FROM {_quote_physical_table(snapshot.physical_table)}",
            "ARRAY JOIN analysis_windows AS analysis_window",
            "WHERE " + "\n  AND ".join(overlap_and_filters),
            "),",
            "context_rows AS (",
            "  SELECT * FROM matched_events",
            "  UNION ALL",
            "  SELECT",
            _indented(sentinel_selects, spaces=4),
            "  FROM (SELECT arrayJoin(analysis_windows) AS analysis_window)",
            f"  WHERE {window_id} NOT IN (SELECT `window_id` FROM matched_events)",
            ")",
            "SELECT * FROM context_rows",
            "ORDER BY `window_id`, `event_id`",
            f"LIMIT {max_context_rows + 1}",
        )
    )


def _event_recurrence_overlap_predicate() -> str:
    window_start = "tupleElement(analysis_window, 3)"
    window_end = "tupleElement(analysis_window, 4)"
    occurrence_day = f"addDays({window_start}, recurrence_day_offset)"
    occurrence_code = (
        f"toMonth({occurrence_day}) * 100 + toDayOfMonth({occurrence_day})"
    )
    start_code = "`recurrence_month_start` * 100 + `recurrence_day_start`"
    end_code = "`recurrence_month_end` * 100 + `recurrence_day_end`"
    return (
        "(`recurrence_kind` = '' OR arrayExists(recurrence_day_offset -> ("
        "(`recurrence_kind` = 'monthly_day_range' "
        "AND `recurrence_month_start` = 0 AND `recurrence_month_end` = 0 "
        "AND `recurrence_day_start` BETWEEN 1 AND 31 "
        "AND `recurrence_day_end` BETWEEN `recurrence_day_start` AND 31 "
        f"AND toDayOfMonth({occurrence_day}) BETWEEN `recurrence_day_start` "
        "AND `recurrence_day_end`) OR "
        "(`recurrence_kind` = 'annual_month_day_range' "
        "AND `recurrence_month_start` BETWEEN 1 AND 12 "
        "AND `recurrence_month_end` BETWEEN 1 AND 12 "
        "AND `recurrence_day_start` BETWEEN 1 AND 31 "
        "AND `recurrence_day_end` BETWEEN 1 AND 31 "
        f"AND (({start_code} <= {end_code} AND {occurrence_code} BETWEEN {start_code} AND {end_code}) "
        f"OR ({start_code} > {end_code} AND ({occurrence_code} >= {start_code} OR {occurrence_code} <= {end_code}))))"
        f"), range(toUInt32(dateDiff('day', {window_start}, {window_end})))))"
    )


def _compile_high_value_query(
    contract: QueryContract,
    snapshot: DatasetSnapshot,
    *,
    date_expression: str,
    filter_sql: tuple[str, ...],
) -> str:
    if snapshot.dataset_id != "paid_order_success":
        raise ValueError(f"high_value_scan_unsupported_dataset:{snapshot.dataset_id}")
    if "user_id" not in snapshot.schema_fields:
        raise ValueError("high_value_scan_requires_field:user_id")
    if len(contract.metric_bindings) != 1:
        raise ValueError("high_value_scan_requires_single_metric")
    binding = contract.metric_bindings[0]
    if any(
        window.aggregation
        not in {
            "daily_total",
            "sum_of_complete_days",
            "mean_of_complete_days",
        }
        for window in contract.resolved_windows
    ):
        raise ValueError("high_value_window_aggregation_unsupported")
    predicates = _window_predicates(date_expression, filter_sql)
    partition_fields = (
        "`__window_id`",
        "`__window_role`",
        "`__window_aggregation`",
        "`__window_start`",
        "`__window_end`",
    )
    partition = ", ".join(partition_fields)
    threshold_join = " AND ".join(
        f"user_totals.{field} = thresholds.{field}" for field in partition_fields
    )
    right_key_join = " AND ".join(
        f"user_totals.{field} = right_key_audit.{field}" for field in partition_fields
    )
    pre_join_audit_join = " AND ".join(
        f"joined_rows.{field} = pre_join_audit.{field}" for field in partition_fields
    )
    complete_days = "dateDiff('day', `__window_start`, `__window_end`)"
    normalized_total = (
        "if(`__window_aggregation` = 'mean_of_complete_days', "
        f"sum(`user_metric_value`) / nullIf({complete_days}, 0), "
        "sum(`user_metric_value`))"
    )
    normalized_threshold = (
        "if(`__window_aggregation` = 'mean_of_complete_days', "
        f"max(`threshold_cutoff`) / nullIf({complete_days}, 0), "
        "max(`threshold_cutoff`))"
    )
    normalized_high_value = (
        "if(`__window_aggregation` = 'mean_of_complete_days', "
        f"sumIf(`user_metric_value`, `is_high_value`) / nullIf({complete_days}, 0), "
        "sumIf(`user_metric_value`, `is_high_value`))"
    )
    normalized_high_value_users = (
        "if(`__window_aggregation` = 'mean_of_complete_days', "
        f"toFloat64(countIf(`is_high_value`)) / nullIf({complete_days}, 0), "
        "toFloat64(countIf(`is_high_value`)))"
    )
    final_select = (
        "`__window_id` AS `window_id`",
        "`__window_role` AS `window_role`",
        "`__window_id` AS `observation_key`",
        f"{normalized_total} AS {_quote_identifier(binding.metric_id)}",
        f"{normalized_threshold} AS `high_value_threshold`",
        f"{normalized_high_value} AS `high_value_amount`",
        f"{normalized_high_value_users} AS `high_value_paid_users`",
        "max(`source_complete_days`) AS `source_complete_days`",
        "max(`join_input_rows`) AS `__join_input_rows`",
        "count() AS `__join_output_rows`",
        "sum(greatest(toInt64(`right_key_multiplicity`) - 1, 0)) "
        "AS `__join_duplicate_keys`",
        "countIf(`threshold_cutoff` IS NULL) AS `__join_unmatched_rows`",
    )
    return "\n".join(
        (
            f"WITH [{_window_tuples(contract.resolved_windows)}] AS analysis_windows,",
            "matched_rows AS (",
            "  SELECT",
            _indented(
                (
                    "source.*",
                    "tupleElement(analysis_window, 1) AS `__window_id`",
                    "tupleElement(analysis_window, 2) AS `__window_role`",
                    "tupleElement(analysis_window, 3) AS `__window_start`",
                    "tupleElement(analysis_window, 4) AS `__window_end`",
                    "tupleElement(analysis_window, 5) AS `__window_aggregation`",
                    f"{date_expression} AS `__observation_date`",
                ),
                spaces=4,
            ),
            f"  FROM {_quote_physical_table(snapshot.physical_table)} AS source",
            "  ARRAY JOIN analysis_windows AS analysis_window",
            "  WHERE " + "\n    AND ".join(predicates),
            "),",
            "window_coverage AS (",
            "  SELECT",
            "    `__window_id`,",
            "    `__window_role`,",
            "    uniqExact(`__observation_date`) AS `source_complete_days`",
            "  FROM matched_rows",
            "  GROUP BY `__window_id`, `__window_role`",
            "),",
            "user_totals AS (",
            "  SELECT",
            _indented(
                (
                    *partition_fields,
                    "`user_id` AS `user_id`",
                    f"{binding.expression} AS `user_metric_value`",
                ),
                spaces=4,
            ),
            "  FROM matched_rows",
            "  GROUP BY " + ", ".join((*partition_fields, "`user_id`")),
            "),",
            "pre_join_audit AS (",
            "  SELECT",
            "    " + ",\n    ".join(partition_fields) + ",",
            "    count() AS `join_input_rows`",
            "  FROM user_totals",
            "  GROUP BY " + partition,
            "),",
            "thresholds AS (",
            "  SELECT",
            "    " + ",\n    ".join(partition_fields) + ",",
            "    quantileExact(%(threshold_quantile)s)(`user_metric_value`) "
            "AS `threshold_cutoff`",
            "  FROM user_totals",
            "  GROUP BY " + partition,
            "),",
            "right_key_audit AS (",
            "  SELECT",
            "    " + ",\n    ".join(partition_fields) + ",",
            "    count() AS `right_key_multiplicity`",
            "  FROM thresholds",
            "  GROUP BY " + partition,
            "),",
            "joined_rows AS (",
            "  SELECT",
            "    "
            + ",\n    ".join(
                f"user_totals.{field} AS {field}" for field in partition_fields
            )
            + ",",
            "    user_totals.`user_id` AS `user_id`,",
            "    user_totals.`user_metric_value` AS `user_metric_value`,",
            "    thresholds.`threshold_cutoff` AS `threshold_cutoff`,",
            "    coalesce(right_key_audit.`right_key_multiplicity`, toUInt64(0)) "
            "AS `right_key_multiplicity`,",
            "    user_totals.`user_metric_value` >= "
            "thresholds.`threshold_cutoff` AS `is_high_value`",
            "  FROM user_totals",
            "  LEFT JOIN thresholds ON " + threshold_join,
            "  LEFT JOIN right_key_audit ON " + right_key_join,
            "),",
            "audited_rows AS (",
            "  SELECT",
            "    "
            + ",\n    ".join(
                f"joined_rows.{field} AS {field}" for field in partition_fields
            )
            + ",",
            "    joined_rows.`user_id` AS `user_id`,",
            "    joined_rows.`user_metric_value` AS `user_metric_value`,",
            "    joined_rows.`threshold_cutoff` AS `threshold_cutoff`,",
            "    joined_rows.`right_key_multiplicity` AS `right_key_multiplicity`,",
            "    joined_rows.`is_high_value` AS `is_high_value`,",
            "    pre_join_audit.`join_input_rows` AS `join_input_rows`,",
            "    window_coverage.`source_complete_days` AS `source_complete_days`",
            "  FROM joined_rows",
            "  LEFT JOIN pre_join_audit ON " + pre_join_audit_join,
            "  LEFT JOIN window_coverage ON "
            "joined_rows.`__window_id` = window_coverage.`__window_id` AND "
            "joined_rows.`__window_role` = window_coverage.`__window_role`",
            ")",
            "SELECT",
            _indented(final_select),
            "FROM audited_rows",
            "GROUP BY " + partition,
        )
    )


def _intent_selects(
    query_intent: str,
    *,
    date_expression: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if query_intent == "time_bucket_scan":
        return (
            (
                f"toMonday({date_expression}) AS `calendar_week`",
                f"toDayOfWeek({date_expression}) AS `weekday`",
                "multiIf("
                f"toDayOfMonth({date_expression}) <= 10, 'start', "
                f"toDayOfMonth({date_expression}) <= 20, 'mid', 'end'"
                ") AS `month_phase`",
            ),
            ("`calendar_week`", "`weekday`", "`month_phase`"),
        )
    if query_intent == "data_quality_probe":
        return (("count() AS `source_row_count`",), ())
    if query_intent == "event_context_probe":
        return (("count() AS `event_count`",), ())
    return (), ()


def _validate_runtime_types(
    contract: QueryContract,
    snapshots: Mapping[str, DatasetSnapshot],
) -> None:
    if not isinstance(contract, QueryContract):
        raise TypeError("invalid_query_contract_runtime_type:query_contract")
    for field_name in (
        "query_contract_id",
        "analysis_contract_ref",
        "query_intent",
        "workload_class",
        "contract_signature",
    ):
        _require_runtime_string(getattr(contract, field_name), field_name)
    _require_runtime_string_tuple(
        contract.dataset_snapshot_refs,
        "dataset_snapshot_refs",
    )
    _require_runtime_instances(
        contract.metric_bindings,
        MetricBinding,
        "metric_bindings",
    )
    for binding in contract.metric_bindings:
        _validate_metric_binding_types(binding)
    _require_runtime_instances(
        contract.dimension_bindings,
        DimensionBinding,
        "dimension_bindings",
    )
    for binding in contract.dimension_bindings:
        _validate_dimension_binding_types(binding)
    _require_runtime_string_tuple(contract.window_refs, "window_refs")
    _require_runtime_instances(
        contract.resolved_windows,
        ResolvedWindow,
        "resolved_windows",
    )
    for window in contract.resolved_windows:
        _validate_window_types(window)
    if not isinstance(contract.filters, tuple) or any(
        not isinstance(item, Mapping) for item in contract.filters
    ):
        raise TypeError("invalid_query_contract_runtime_type:filters")
    if not isinstance(contract.result_shape, ResultShape):
        raise TypeError("invalid_query_contract_runtime_type:result_shape")
    _validate_result_shape_types(contract.result_shape)
    _require_runtime_string_tuple(
        contract.completeness_assertions,
        "completeness_assertions",
    )
    if not isinstance(contract.query_parameters, Mapping):
        raise TypeError("invalid_query_contract_runtime_type:query_parameters")
    if not isinstance(contract.query_role_ref, str):
        raise TypeError("invalid_query_contract_runtime_type:query_role_ref")
    if contract.reconciliation_binding is not None:
        if not isinstance(contract.reconciliation_binding, ReconciliationBinding):
            raise TypeError(
                "invalid_query_contract_runtime_type:reconciliation_binding"
            )
        _require_runtime_string(
            contract.reconciliation_binding.reference_query_role_ref,
            "reconciliation_binding.reference_query_role_ref",
        )
        _require_runtime_string(
            contract.reconciliation_binding.reference_contract_signature,
            "reconciliation_binding.reference_contract_signature",
        )
    if contract.join_expectation is not None:
        if not isinstance(contract.join_expectation, JoinExpectation):
            raise TypeError("invalid_query_contract_runtime_type:join_expectation")
        if contract.join_expectation.cardinality not in {
            "one_to_one",
            "many_to_one",
        }:
            raise ValueError("invalid_join_expectation_cardinality")
        _require_runtime_string_tuple(
            contract.join_expectation.audit_fields,
            "join_expectation.audit_fields",
        )
        for field_name in ("max_duplicate_keys", "max_unmatched_rows"):
            value = getattr(contract.join_expectation, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"invalid_join_expectation:{field_name}")

    if not isinstance(snapshots, Mapping):
        raise TypeError("invalid_snapshots_runtime_type")
    for snapshot_ref, snapshot in snapshots.items():
        if not isinstance(snapshot_ref, str) or not isinstance(
            snapshot,
            DatasetSnapshot,
        ):
            raise TypeError("invalid_snapshot_runtime_type")
        _validate_snapshot_types(snapshot)
        if snapshot_ref != snapshot.snapshot_ref:
            raise ValueError(f"dataset_snapshot_ref_key_mismatch:{snapshot_ref}")


def _validate_metric_binding_types(binding: MetricBinding) -> None:
    for field_name in (
        "metric_id",
        "contract_ref",
        "dataset_id",
        "expression",
        "aggregation",
        "numerator_metric",
        "denominator_metric",
        "zero_denominator_policy",
        "value_semantics",
        "display_format",
    ):
        _require_runtime_string(
            getattr(binding, field_name),
            f"metric_bindings.{field_name}",
        )
    _require_runtime_string_tuple(
        binding.required_fields,
        "metric_bindings.required_fields",
    )
    _require_runtime_string_tuple(binding.grain, "metric_bindings.grain")
    _require_runtime_string_tuple(
        binding.claim_types,
        "metric_bindings.claim_types",
    )
    tolerance = binding.reconciliation_tolerance
    if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)):
        raise TypeError(
            "invalid_query_contract_runtime_type:"
            "metric_bindings.reconciliation_tolerance"
        )
    if not math.isfinite(float(tolerance)) or tolerance < 0:
        raise ValueError(
            "invalid_query_contract_runtime_type:"
            "metric_bindings.reconciliation_tolerance"
        )
    if binding.reconciliation_strategy not in {
        "additive_sum",
        "exact_additive_count",
        "ratio_from_components",
        "unsupported_non_additive",
    }:
        raise ValueError(
            "invalid_query_contract_runtime_type:"
            "metric_bindings.reconciliation_strategy"
        )
    if binding.reconciliation_strategy == "ratio_from_components" and (
        not binding.numerator_metric or not binding.denominator_metric
    ):
        raise ValueError("ratio_reconciliation_components_required")


def _validate_dimension_binding_types(binding: DimensionBinding) -> None:
    for field_name in (
        "dimension_id",
        "contract_ref",
        "dataset_id",
        "source_field",
        "null_bucket",
    ):
        _require_runtime_string(
            getattr(binding, field_name),
            f"dimension_bindings.{field_name}",
        )
    _require_runtime_string_tuple(
        binding.allowed_grains,
        "dimension_bindings.allowed_grains",
    )


def _validate_window_types(window: ResolvedWindow) -> None:
    for field_name in (
        "window_id",
        "role",
        "label",
        "start_inclusive",
        "end_exclusive",
        "timezone",
        "aggregation",
        "source_watermark_requirement",
        "membership_policy",
    ):
        _require_runtime_string(
            getattr(window, field_name),
            f"resolved_windows.{field_name}",
        )
        if not getattr(window, field_name).strip():
            raise ValueError(f"invalid_resolved_window_field:{field_name}")
    if isinstance(window.required_complete_days, bool) or not isinstance(
        window.required_complete_days, int
    ):
        raise TypeError(
            "invalid_query_contract_runtime_type:"
            "resolved_windows.required_complete_days"
        )
    _require_runtime_string_tuple(
        window.capability_refs,
        "resolved_windows.capability_refs",
    )


def _validate_result_shape_types(result_shape: ResultShape) -> None:
    for field_name in (
        "required_fields",
        "unique_key",
        "grain",
        "required_window_ids",
    ):
        _require_runtime_string_tuple(
            getattr(result_shape, field_name),
            f"result_shape.{field_name}",
        )
    _require_runtime_string(
        result_shape.result_semantics,
        "result_shape.result_semantics",
    )
    _require_runtime_string(
        result_shape.dimension_presence_policy,
        "result_shape.dimension_presence_policy",
    )
    if result_shape.dimension_presence_policy not in DIMENSION_PRESENCE_POLICIES:
        raise ValueError(
            "dimension_presence_policy_invalid:"
            f"{result_shape.dimension_presence_policy or 'missing'}"
        )


def _validate_snapshot_types(snapshot: DatasetSnapshot) -> None:
    for field_name in (
        "snapshot_ref",
        "dataset_id",
        "physical_table",
        "watermark",
        "schema_fingerprint",
        "contract_ref",
        "loaded_at",
        "status",
        "evidence_state",
        "reconciliation_status",
        "reconciliation_ref",
        "logical_snapshot_id",
        "load_revision",
        "release_ref",
        "authority_record_ref",
        "rows_content_hash",
    ):
        value = getattr(snapshot, field_name)
        if not isinstance(value, str):
            raise TypeError("invalid_snapshot_runtime_type")
        if not value.strip() and field_name not in {
            "reconciliation_ref",
            "logical_snapshot_id",
            "load_revision",
            "release_ref",
            "authority_record_ref",
            "rows_content_hash",
        }:
            raise ValueError(f"invalid_snapshot_metadata:{field_name}")
    if not isinstance(snapshot.schema_fields, tuple) or any(
        not isinstance(item, str) for item in snapshot.schema_fields
    ):
        raise TypeError("invalid_snapshot_runtime_type")
    try:
        date.fromisoformat(snapshot.watermark)
    except ValueError as exc:
        raise ValueError("invalid_snapshot_metadata:watermark") from exc
    try:
        loaded_at = datetime.fromisoformat(snapshot.loaded_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid_snapshot_metadata:loaded_at") from exc
    if loaded_at.tzinfo is None or loaded_at.utcoffset() is None:
        raise ValueError("invalid_snapshot_metadata:loaded_at")
    if snapshot.evidence_state not in {"claim_ready", "context_only", "blocked"}:
        raise ValueError("invalid_snapshot_metadata:evidence_state")
    if snapshot.reconciliation_status not in {
        "matched",
        "mismatch",
        "incomplete",
        "not_comparable",
        "not_applicable",
    }:
        raise ValueError("invalid_snapshot_metadata:reconciliation_status")
    if bool(snapshot.logical_snapshot_id) != bool(snapshot.load_revision):
        raise ValueError("invalid_snapshot_metadata:physical_revision")


def _require_runtime_instances(
    value: Any,
    item_type: type,
    field_name: str,
) -> None:
    if not isinstance(value, tuple) or any(
        not isinstance(item, item_type) for item in value
    ):
        raise TypeError(f"invalid_query_contract_runtime_type:{field_name}")


def _require_runtime_string(value: Any, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"invalid_query_contract_runtime_type:{field_name}")


def _require_runtime_string_tuple(value: Any, field_name: str) -> None:
    if not isinstance(value, tuple) or any(not isinstance(item, str) for item in value):
        raise TypeError(f"invalid_query_contract_runtime_type:{field_name}")


def _verify_window_consistency(contract: QueryContract) -> None:
    resolved_ids = tuple(window.window_id for window in contract.resolved_windows)
    if len(resolved_ids) != len(set(resolved_ids)):
        raise ValueError("query_contract_window_refs_not_unique")
    if resolved_ids != contract.window_refs:
        raise ValueError("query_contract_window_refs_mismatch")
    if contract.result_shape.required_window_ids != contract.window_refs:
        raise ValueError("query_contract_result_window_refs_mismatch")
    for window in contract.resolved_windows:
        _verify_window_boundary(window)


def _verify_window_boundary(window: ResolvedWindow) -> None:
    try:
        start = date.fromisoformat(window.start_inclusive)
        end = date.fromisoformat(window.end_exclusive)
        watermark = date.fromisoformat(window.source_watermark_requirement)
        ZoneInfo(window.timezone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError(
            f"invalid_resolved_window_boundary:{window.window_id}"
        ) from exc
    duration_days = (end - start).days
    if (
        duration_days <= 0
        or window.required_complete_days <= 0
        or window.required_complete_days > duration_days
        or not start <= watermark < end
    ):
        raise ValueError(f"invalid_resolved_window_boundary:{window.window_id}")


def _single_snapshot(
    contract: QueryContract,
    snapshots: Mapping[str, DatasetSnapshot],
) -> DatasetSnapshot:
    if len(contract.dataset_snapshot_refs) != 1:
        raise ValueError("query_contract_requires_single_snapshot")
    snapshot_ref = contract.dataset_snapshot_refs[0]
    snapshot = snapshots.get(snapshot_ref)
    if snapshot is None:
        raise ValueError(f"dataset_snapshot_missing:{snapshot_ref}")
    if snapshot.status != "active":
        raise ValueError(f"dataset_snapshot_inactive:{snapshot_ref}")
    if snapshot.evidence_state != "claim_ready" and contract.query_intent not in {
        "data_quality_probe",
        "event_context_probe",
        "association_outcome_timeseries",
        "association_candidate_timeseries",
        "channel_context_probe",
        "channel_context_total_probe",
        "source_reconciliation_probe",
    }:
        raise ValueError(
            f"dataset_evidence_state_not_claim_ready:{snapshot_ref}:{snapshot.evidence_state}"
        )
    if (
        contract.dimension_bindings
        and contract.query_intent
        not in {
            "data_quality_probe",
            "association_outcome_timeseries",
            "association_candidate_timeseries",
            "channel_context_probe",
            "source_reconciliation_probe",
        }
        and snapshot.reconciliation_ref
        and snapshot.reconciliation_status != "matched"
    ):
        raise ValueError(
            "dataset_reconciliation_not_matched:"
            f"{snapshot_ref}:{snapshot.reconciliation_status}"
        )
    for binding in (*contract.metric_bindings, *contract.dimension_bindings):
        if binding.dataset_id != snapshot.dataset_id:
            raise ValueError(
                f"binding_dataset_mismatch:{binding.dataset_id}:{snapshot.dataset_id}"
            )
    _quote_physical_table(snapshot.physical_table)
    return snapshot


def _verify_dataset_snapshot_binding(
    snapshot: DatasetSnapshot,
    *,
    registry: RuntimeContractRegistry,
    release_resolver: DatasetReleaseResolver | None,
) -> None:
    dataset = registry.dataset(snapshot.dataset_id)
    if dataset.get("requires_physical_revision"):
        if not snapshot.logical_snapshot_id or not snapshot.load_revision:
            raise ValueError(
                f"dataset_physical_revision_required:{snapshot.dataset_id}"
            )
        if not {"snapshot_id", "load_revision"}.issubset(snapshot.schema_fields):
            raise ValueError("dataset_physical_revision_fields_missing")
    if dataset.get("requires_release"):
        if not snapshot.release_ref or not snapshot.authority_record_ref:
            raise ValueError(f"dataset_release_required:{snapshot.dataset_id}")
        if not snapshot.rows_content_hash:
            raise ValueError(
                f"dataset_rows_content_hash_required:{snapshot.dataset_id}"
            )
        if release_resolver is None:
            raise ValueError(f"dataset_release_resolver_required:{snapshot.dataset_id}")
        try:
            authority = release_resolver.resolve_dataset_release(snapshot.release_ref)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"dataset_release_authority_unavailable:{snapshot.dataset_id}"
            ) from exc
        if dataset_release_authority_integrity_errors(authority):
            raise ValueError(
                f"dataset_release_authority_integrity:{snapshot.dataset_id}"
            )
        if not snapshot_matches_release_authority(snapshot, authority):
            raise ValueError(
                f"dataset_release_authority_member_mismatch:{snapshot.dataset_id}"
            )
    prefix = str(dataset.get("physical_table_prefix") or "")
    if prefix:
        expected = f"{prefix}{snapshot.schema_fingerprint[:16]}"
        if snapshot.physical_table != expected:
            raise ValueError(
                f"dataset_physical_table_unreviewed:{snapshot.physical_table}"
            )


def _date_expression(
    snapshot: DatasetSnapshot,
    *,
    registry: RuntimeContractRegistry,
) -> str:
    try:
        adapter = registry.dataset(snapshot.dataset_id)
    except KeyError as exc:
        raise ValueError(f"unsupported_dataset_adapter:{snapshot.dataset_id}") from exc
    required_fields = tuple(str(item) for item in adapter.get("required_fields") or ())
    missing = tuple(
        field for field in required_fields if field not in snapshot.schema_fields
    )
    if missing:
        raise ValueError(
            f"dataset_date_binding_fields_missing:{snapshot.dataset_id}:{','.join(missing)}"
        )
    date_field = str(adapter.get("date_field") or "")
    if date_field:
        if date_field not in required_fields:
            raise ValueError(f"dataset_date_binding_invalid:{snapshot.dataset_id}")
        return _quote_identifier(date_field)
    date_expression = str(adapter.get("date_expression") or "")
    if not date_expression or not _safe_contract_expression(
        date_expression,
        allowed_functions=_DATE_FUNCTIONS,
        allowed_fields=frozenset(required_fields),
    ):
        raise ValueError(f"dataset_date_binding_invalid:{snapshot.dataset_id}")
    return date_expression


@lru_cache(maxsize=1)
def _runtime_registry() -> RuntimeContractRegistry:
    return RuntimeContractRegistry.from_path(_RUNTIME_BINDINGS_PATH)


def _window_parameters(windows: Sequence[ResolvedWindow]) -> dict[str, Any]:
    if not windows:
        raise ValueError("query_contract_windows_required")
    parameters: dict[str, Any] = {}
    seen: set[str] = set()
    for index, window in enumerate(windows):
        if window.window_id in seen:
            raise ValueError(f"duplicate_window_id:{window.window_id}")
        seen.add(window.window_id)
        if window.membership_policy != "allow_overlap":
            raise ValueError(
                f"unsupported_window_membership_policy:{window.membership_policy}"
            )
        parameters[f"window_id_{index}"] = window.window_id
        parameters[f"window_role_{index}"] = window.role
        parameters[f"start_{index}"] = window.start_inclusive
        parameters[f"end_{index}"] = window.end_exclusive
        parameters[f"window_aggregation_{index}"] = window.aggregation
    return parameters


def _window_tuples(windows: Sequence[ResolvedWindow]) -> str:
    return ", ".join(
        "("
        f"%(window_id_{index})s, %(window_role_{index})s, "
        f"toDate(%(start_{index})s), toDate(%(end_{index})s), "
        f"%(window_aggregation_{index})s"
        ")"
        for index, _ in enumerate(windows)
    )


def _window_predicates(
    date_expression: str,
    filter_sql: Sequence[str],
) -> tuple[str, ...]:
    return (
        f"{date_expression} >= tupleElement(analysis_window, 3)",
        f"{date_expression} < tupleElement(analysis_window, 4)",
        *filter_sql,
    )


def _compile_filters(
    filters: Sequence[Mapping[str, Any]],
    snapshot: DatasetSnapshot,
    *,
    registry: RuntimeContractRegistry,
) -> tuple[tuple[str, ...], dict[str, Any]]:
    clauses: list[str] = []
    parameters: dict[str, Any] = {}
    scalar_operators = {
        "eq": "=",
        "ne": "!=",
        "gt": ">",
        "gte": ">=",
        "lt": "<",
        "lte": "<=",
    }
    customer_safe_fields = set(
        registry.customer_safe_filter_fields(snapshot.dataset_id)
    )
    for index, filter_item in enumerate(filters):
        field = str(filter_item.get("field") or "")
        if field not in customer_safe_fields:
            raise ValueError(f"customer_safe_filter_field_unapproved:{field}")
        if field not in snapshot.schema_fields:
            raise ValueError(f"unsupported_filter_field:{field}")
        quoted_field = _quote_identifier(field)
        operator = str(filter_item.get("op") or "").casefold()
        parameter_name = f"filter_{index}"
        if operator in scalar_operators:
            value = _filter_scalar(filter_item.get("value"), operator=operator)
            clauses.append(
                f"{quoted_field} {scalar_operators[operator]} %({parameter_name})s"
            )
            parameters[parameter_name] = value
        elif operator in {"in", "not_in"}:
            raw_values = filter_item.get("value")
            if not isinstance(raw_values, Sequence) or isinstance(
                raw_values, (str, bytes)
            ):
                raise ValueError(f"invalid_filter_value:{operator}")
            values = tuple(
                _filter_scalar(value, operator=operator) for value in raw_values
            )
            if not values:
                raise ValueError(f"invalid_filter_value:{operator}")
            placeholders = []
            for value_index, value in enumerate(values):
                item_name = f"{parameter_name}_{value_index}"
                placeholders.append(f"%({item_name})s")
                parameters[item_name] = value
            keyword = "NOT IN" if operator == "not_in" else "IN"
            clauses.append(f"{quoted_field} {keyword} ({', '.join(placeholders)})")
        elif operator in {"is_null", "is_not_null"}:
            clauses.append(
                f"{quoted_field} IS {'NOT ' if operator == 'is_not_null' else ''}NULL"
            )
        elif operator == "between":
            raw_values = filter_item.get("value")
            if (
                not isinstance(raw_values, Sequence)
                or isinstance(raw_values, (str, bytes))
                or len(raw_values) != 2
            ):
                raise ValueError("invalid_filter_value:between")
            start_name = f"{parameter_name}_start"
            end_name = f"{parameter_name}_end"
            parameters[start_name] = _filter_scalar(raw_values[0], operator=operator)
            parameters[end_name] = _filter_scalar(raw_values[1], operator=operator)
            clauses.append(
                f"{quoted_field} BETWEEN %({start_name})s AND %({end_name})s"
            )
        else:
            raise ValueError(f"unsupported_filter_operator:{operator or 'missing'}")
    return tuple(clauses), parameters


def _physical_snapshot_filters(
    snapshot: DatasetSnapshot,
    *,
    requires_physical_revision: bool,
) -> tuple[tuple[str, ...], dict[str, Any]]:
    if not requires_physical_revision:
        return (), {}
    required = {"snapshot_id", "load_revision"}
    if not required.issubset(snapshot.schema_fields):
        raise ValueError("dataset_physical_revision_fields_missing")
    return (
        (
            "`snapshot_id` = %(physical_snapshot_id)s",
            "`load_revision` = %(load_revision)s",
        ),
        {
            "physical_snapshot_id": snapshot.logical_snapshot_id,
            "load_revision": snapshot.load_revision,
        },
    )


def _filter_scalar(value: Any, *, operator: str) -> Any:
    if value is None or isinstance(value, (Mapping, list, tuple, set)):
        raise ValueError(f"invalid_filter_value:{operator}")
    if not isinstance(value, (str, int, float, bool)):
        raise ValueError(f"invalid_filter_value:{operator}")
    return value


def _dimension_selects(
    contract: QueryContract,
    snapshot: DatasetSnapshot,
    *,
    parameters: dict[str, Any],
) -> tuple[tuple[str, str], ...]:
    selected = []
    seen: set[str] = set()
    for index, binding in enumerate(contract.dimension_bindings):
        if binding.dimension_id in seen:
            raise ValueError(f"duplicate_dimension_binding:{binding.dimension_id}")
        seen.add(binding.dimension_id)
        if binding.source_field not in snapshot.schema_fields:
            raise ValueError(f"dimension_field_missing:{binding.source_field}")
        source = _quote_identifier(binding.source_field)
        alias = _quote_identifier(binding.dimension_id)
        parameter_name = f"dimension_null_bucket_{index}"
        parameters[parameter_name] = binding.null_bucket
        normalized = (
            f"ifNull(nullIf(trim(toString({source})), ''), %({parameter_name})s)"
        )
        selected.append((f"{normalized} AS {alias}", alias))
    return tuple(selected)


def _metric_selects(
    contract: QueryContract,
    snapshot: DatasetSnapshot,
) -> tuple[str, ...]:
    selected = []
    seen: set[str] = set()
    for binding in contract.metric_bindings:
        if binding.metric_id in seen:
            raise ValueError(f"duplicate_metric_binding:{binding.metric_id}")
        seen.add(binding.metric_id)
        selected.append(
            f"{binding.expression} AS {_quote_identifier(binding.metric_id)}"
        )
    return tuple(selected)


def _safe_expression(expression: str) -> bool:
    if not isinstance(expression, str) or not expression.strip():
        return False
    if any(token in expression for token in (";", "--", "/*", "*/", "#")):
        return False
    return re.fullmatch(r"[\w\s`'().,+*/%<>=!\-]+", expression, re.UNICODE) is not None


def _verify_contract_signature(contract: QueryContract) -> None:
    expected = query_contract_signature(contract)
    if contract.contract_signature != expected:
        raise ValueError(
            f"query_contract_signature_mismatch:{contract.query_contract_id}"
        )


def _verify_reviewed_query_shape(
    contract: QueryContract,
    registry: RuntimeContractRegistry,
) -> None:
    try:
        reviewed = registry.query_shape(contract.query_intent)
    except KeyError as exc:
        raise ValueError(
            f"reviewed_query_shape_missing:{contract.query_intent}"
        ) from exc
    reviewed_parameters = _freeze_contract_value(reviewed.get("query_parameters") or {})
    source_field_policy = str(reviewed.get("source_field_policy") or "")
    if source_field_policy not in {"", "metric_bindings"}:
        raise ValueError(
            f"reviewed_source_field_policy_invalid:{contract.query_intent}"
        )
    if _freeze_contract_value(contract.query_parameters) != reviewed_parameters:
        raise ValueError(f"reviewed_query_parameters_mismatch:{contract.query_intent}")
    expected_join = _reviewed_join_expectation(reviewed)
    if contract.join_expectation != expected_join:
        raise ValueError(f"reviewed_join_expectation_mismatch:{contract.query_intent}")
    dimension_ids = tuple(item.dimension_id for item in contract.dimension_bindings)
    expected_shape = ResultShape(
        required_fields=_dedupe(
            (
                *_string_tuple(reviewed.get("required_fields")),
                *(item.metric_id for item in contract.metric_bindings),
                *dimension_ids,
            )
        ),
        unique_key=_dedupe(
            (*_string_tuple(reviewed.get("unique_key")), *dimension_ids)
        ),
        grain=_dedupe((*_string_tuple(reviewed.get("grain")), *dimension_ids)),
        required_window_ids=contract.window_refs,
        result_semantics=str(reviewed.get("result_semantics") or "complete_aggregate"),
        dimension_presence_policy=str(reviewed["dimension_presence_policy"]),
    )
    if contract.result_shape != expected_shape:
        raise ValueError(f"reviewed_result_shape_mismatch:{contract.query_intent}")


def _verify_reviewed_bindings(
    contract: QueryContract,
    snapshot: DatasetSnapshot,
    *,
    registry: RuntimeContractRegistry,
) -> None:
    for binding in contract.metric_bindings:
        try:
            reviewed = registry.metric(
                binding.metric_id,
                dataset_id=snapshot.dataset_id,
            )
        except KeyError as exc:
            raise ValueError(
                f"reviewed_metric_binding_mismatch:{binding.metric_id}"
            ) from exc
        expected = MetricBinding(
            metric_id=binding.metric_id,
            contract_ref=str(reviewed.get("contract_ref") or ""),
            dataset_id=str(reviewed.get("dataset_id") or ""),
            expression=str(reviewed.get("expression") or ""),
            aggregation=str(reviewed.get("aggregation") or ""),
            required_fields=_string_tuple(reviewed.get("required_fields")),
            grain=_string_tuple(reviewed.get("grain")),
            numerator_metric=str(reviewed.get("numerator_metric") or ""),
            denominator_metric=str(reviewed.get("denominator_metric") or ""),
            zero_denominator_policy=str(
                reviewed.get("zero_denominator_policy") or "null"
            ),
            claim_types=_string_tuple(reviewed.get("claim_types")),
            reconciliation_tolerance=_reviewed_reconciliation_tolerance(reviewed),
            reconciliation_strategy=str(
                reviewed.get("reconciliation_strategy") or "unsupported_non_additive"
            ),
            value_semantics=str(reviewed.get("value_semantics") or "raw_scalar"),
            display_format=str(reviewed.get("display_format") or "number"),
        )
        if not _safe_contract_expression(
            binding.expression,
            allowed_functions=_METRIC_FUNCTIONS,
            allowed_fields=frozenset(binding.required_fields),
        ) or not _AGGREGATE_EXPRESSION.search(binding.expression):
            raise ValueError(f"unsafe_metric_expression:{binding.metric_id}")
        if binding != expected:
            raise ValueError(f"reviewed_metric_binding_mismatch:{binding.metric_id}")
        missing = tuple(
            field
            for field in binding.required_fields
            if field not in snapshot.schema_fields
        )
        if missing:
            raise ValueError(
                f"metric_binding_fields_missing:{binding.metric_id}:{','.join(missing)}"
            )

    for binding in contract.dimension_bindings:
        try:
            reviewed = registry.dimension(
                binding.dimension_id,
                dataset_id=snapshot.dataset_id,
            )
        except KeyError as exc:
            raise ValueError(
                f"reviewed_dimension_binding_mismatch:{binding.dimension_id}"
            ) from exc
        expected = DimensionBinding(
            dimension_id=binding.dimension_id,
            contract_ref=str(reviewed.get("contract_ref") or ""),
            dataset_id=str(reviewed.get("dataset_id") or ""),
            source_field=str(reviewed.get("source_field") or ""),
            allowed_grains=_string_tuple(reviewed.get("allowed_grains")),
            null_bucket=str(reviewed.get("null_bucket") or "Unknown"),
        )
        if binding != expected:
            raise ValueError(
                f"reviewed_dimension_binding_mismatch:{binding.dimension_id}"
            )


def _reviewed_reconciliation_tolerance(reviewed: Mapping[str, Any]) -> float:
    value = reviewed.get("reconciliation_tolerance", 0.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid_metric_reconciliation_tolerance")
    tolerance = float(value)
    if not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError("invalid_metric_reconciliation_tolerance")
    return tolerance


def _reviewed_join_expectation(
    reviewed: Mapping[str, Any],
) -> JoinExpectation | None:
    value = reviewed.get("join_expectation")
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("invalid_reviewed_join_expectation")
    try:
        expectation = JoinExpectation(
            cardinality=str(value["cardinality"]),
            audit_fields=_string_tuple(value["audit_fields"]),
            max_duplicate_keys=value["max_duplicate_keys"],
            max_unmatched_rows=value["max_unmatched_rows"],
        )
    except KeyError as exc:
        raise ValueError("invalid_reviewed_join_expectation") from exc
    if (
        expectation.cardinality not in {"one_to_one", "many_to_one"}
        or not expectation.audit_fields
        or isinstance(expectation.max_duplicate_keys, bool)
        or not isinstance(expectation.max_duplicate_keys, int)
        or expectation.max_duplicate_keys < 0
        or isinstance(expectation.max_unmatched_rows, bool)
        or not isinstance(expectation.max_unmatched_rows, int)
        or expectation.max_unmatched_rows < 0
    ):
        raise ValueError("invalid_reviewed_join_expectation")
    return expectation


def _safe_contract_expression(
    expression: str,
    *,
    allowed_functions: frozenset[str],
    allowed_fields: frozenset[str],
) -> bool:
    if not _safe_expression(expression):
        return False
    quoted_fields = frozenset(re.findall(r"`([^`]+)`", expression))
    if not quoted_fields.issubset(allowed_fields):
        return False
    masked = _mask_expression_quotes(expression)
    functions = frozenset(
        item.casefold()
        for item in re.findall(r"\b([^\W\d]\w*)\s*\(", masked, re.UNICODE)
    )
    if not functions.issubset(allowed_functions):
        return False
    identifiers = frozenset(
        item
        for item in re.findall(r"\b[^\W\d]\w*\b", masked, re.UNICODE)
        if item.casefold() not in functions
    )
    if {item.casefold() for item in identifiers} & _STRUCTURAL_KEYWORDS:
        return False
    return identifiers.issubset(allowed_fields)


def _mask_expression_quotes(expression: str) -> str:
    output = []
    index = 0
    quote = ""
    while index < len(expression):
        char = expression[index]
        if quote:
            output.append(" ")
            if char == quote:
                if (
                    quote == "'"
                    and index + 1 < len(expression)
                    and expression[index + 1] == "'"
                ):
                    output.append(" ")
                    index += 2
                    continue
                quote = ""
            index += 1
            continue
        if char in {"'", "`"}:
            quote = char
            output.append(" ")
        else:
            output.append(char)
        index += 1
    return "".join(output)


def _freeze_contract_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _freeze_contract_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_contract_value(item) for item in value)
    return value


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(str(item) for item in value)


def _dedupe(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(item) for item in values))


def _quote_physical_table(value: str) -> str:
    if not isinstance(value, str) or _PHYSICAL_IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"invalid_physical_table:{value}")
    return ".".join(f"`{part}`" for part in value.split("."))


def _quote_identifier(value: str) -> str:
    if not isinstance(value, str) or _LOGICAL_IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"invalid_identifier:{value}")
    return f"`{value}`"


def _indented(parts: Sequence[str], *, spaces: int = 2) -> str:
    prefix = " " * spaces
    return ",\n".join(f"{prefix}{part}" for part in parts)
