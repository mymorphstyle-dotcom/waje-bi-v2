from __future__ import annotations

from collections.abc import Iterable, Mapping
import hashlib
import json
from typing import Any

from bi_agent.runtime.analysis_assets import (
    build_dimension_scan_reuse_contract,
    reusable_dimension_scan_inputs,
)


BASE_MEASURES = ("amount", "paid_users", "orders", "first_paid_users")
REQUIRED_FIELDS = ("period", "group", *BASE_MEASURES)
SEGMENT_DIMENSION_KEYS = ("channel",)
JOINT_DIMENSION_KEYS = ("channel", "payment_method", "region", "device_brand")
DIMENSION_CANDIDATES = (
    {"field": "channel", "business_name": "一级渠道", "required": True},
    {"field": "region", "business_name": "地区", "required": False},
    {"field": "device_brand", "business_name": "设备", "required": False},
    {"field": "package_name", "business_name": "包", "required": False},
    {"field": "payment_method", "business_name": "支付方式", "required": False},
    {"field": "gameplay_id", "business_name": "玩法", "required": False},
)
CONTRACT_GAP_DESCRIPTORS = {
    "high_value_user_contract_missing": {
        "required_fields": ("user_id", "paid_amount_ngn"),
    },
    "package_name_contract_missing": {
        "fields": ("package_name", "package_id", "bundle_id"),
    },
    "gameplay_contract_missing": {
        "fields": ("gameplay_id", "gameplay", "play_mode"),
    },
    "event_context_contract_missing": {
        "required_fields": ("event_id", "event_time", "campaign_id"),
    },
    "payment_status_contract_missing": {
        "fields": ("payment_status", "pay_status", "status"),
    },
    "duplicate_order_contract_missing": {
        "fields": ("order_id", "payment_order_id"),
    },
}


def build_revenue_runtime_plan(
    *,
    target_metric: str,
    accepted_graph: Iterable[str],
    diagnostic_axes: Iterable[str],
    question_text: str,
    bound_context: Mapping[str, Any] | None = None,
    prior_assets: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    graph = tuple(dict.fromkeys(str(node) for node in accepted_graph))
    axes = tuple(dict.fromkeys(str(axis) for axis in diagnostic_axes))
    normalized_context = dict(bound_context or {})
    windows = _windows(normalized_context)
    baselines = _baselines(normalized_context, axes, question_text)
    scope = _scope(normalized_context)
    permission_scope = _permission_scope(normalized_context)
    snapshot_version = _snapshot_version(normalized_context)
    contract_versions = _contract_versions(normalized_context)
    schema_fields = _schema_fields(normalized_context)
    dimensions = _dimension_candidates(graph, axes)
    row_shape = _row_shape(graph, axes, dimensions, schema_fields=schema_fields)
    schema_fingerprint = _schema_fingerprint(
        normalized_context,
        row_shape=row_shape,
        required_dimensions=_required_dimension_scan_dimensions(graph),
    )
    reusable_asset_rows = _reusable_asset_rows(
        graph,
        prior_assets,
        target_metric=target_metric,
        scope=scope,
        time_window=str(normalized_context.get("time_window") or ""),
        windows=windows,
        baselines=baselines,
        permission_scope=permission_scope,
        snapshot_version=snapshot_version,
        contract_versions=contract_versions,
        schema_fingerprint=schema_fingerprint,
        required_fields=tuple(row_shape["required_fields"]),
    )
    reusable_assets = tuple(item["query_ref"] for item in reusable_asset_rows)
    reuse_contract = build_dimension_scan_reuse_contract(
        target_metric=target_metric,
        scope=scope,
        time_window=str(normalized_context.get("time_window") or ""),
        windows=windows,
        baselines=baselines,
        permission_scope=permission_scope,
        snapshot_version=snapshot_version,
        dimensions=_required_dimension_scan_dimensions(graph),
        required_fields=tuple(row_shape["required_fields"]),
        contract_versions=contract_versions,
        schema_fingerprint=schema_fingerprint,
    )
    return {
        "target_metric": target_metric,
        "scope": scope,
        "time_window": str(normalized_context.get("time_window") or ""),
        "diagnostic_axes": axes,
        "windows": windows,
        "baselines": baselines,
        "permission_scope": permission_scope,
        "snapshot_version": snapshot_version,
        "contract_versions": contract_versions,
        "schema_fingerprint": schema_fingerprint,
        "dimension_candidates": dimensions,
        "measures": BASE_MEASURES,
        "capability_params": _capability_params(graph, baselines, dimensions, normalized_context),
        "query_intents": _query_intents(graph, axes, reusable_assets, row_shape),
        "row_shapes": (row_shape,),
        "contract_gaps": row_shape["contract_gaps"],
        "asset_inputs_used": reusable_assets,
        "asset_row_inputs": reusable_asset_rows,
        "asset_reuse_contract": reuse_contract,
    }


def _windows(bound_context: Mapping[str, Any]) -> dict[str, Any]:
    explicit = bound_context.get("windows")
    if isinstance(explicit, Mapping):
        normalized = {str(key): value for key, value in explicit.items() if value not in ("", None)}
        if normalized:
            return normalized

    pattern_params = bound_context.get("pattern_params")
    if isinstance(pattern_params, Mapping):
        pattern_params = dict(pattern_params)
    else:
        pattern_params = {}
    target = _label_from_bound_item(bound_context.get("target")) or pattern_params.get("target_window")
    baseline = _label_from_bound_item(bound_context.get("baseline")) or pattern_params.get(
        "baseline_window"
    )
    time_window = bound_context.get("time_window")
    normalized = {
        "target": target,
        "baseline": baseline,
        "time_window": time_window,
    }
    normalized = {key: value for key, value in normalized.items() if value not in ("", None)}
    if normalized:
        return normalized
    return {"target": "yesterday", "history_days": 36}


def _baselines(
    bound_context: Mapping[str, Any],
    axes: tuple[str, ...],
    question_text: str,
) -> tuple[str, ...]:
    explicit = bound_context.get("baselines")
    if isinstance(explicit, Iterable) and not isinstance(explicit, (str, bytes, Mapping)):
        normalized = tuple(dict.fromkeys(str(item) for item in explicit if item))
        if normalized:
            return normalized
    if str(bound_context.get("pattern_family") or "") == "custom_baseline":
        if _label_from_bound_item(bound_context.get("baseline")) or _label_from_bound_item(
            bound_context.get("target")
        ):
            return ("custom_baseline",)
    if "multi_baseline" in axes:
        return ("previous_day", "rolling_7_day_baseline", "same_weekday_last_week")
    if any(token in question_text for token in ("前一天", "昨天", "上涨", "下跌", "变化")):
        return ("previous_day",)
    return ()


def _dimension_candidates(
    graph: tuple[str, ...],
    axes: tuple[str, ...],
) -> tuple[dict[str, Any], ...]:
    if "joint_attribution" in graph or "factor_topk" in axes or "pattern_attribution" in axes:
        return DIMENSION_CANDIDATES
    if "segment_contribution" in graph or "segment_bridge" in graph:
        return (DIMENSION_CANDIDATES[0],)
    return ()


def _row_shape(
    graph: tuple[str, ...],
    axes: tuple[str, ...],
    dimensions: tuple[dict[str, Any], ...],
    *,
    schema_fields: tuple[str, ...] = (),
) -> dict[str, Any]:
    graph_set = set(graph)
    schema = set(schema_fields)
    optional_fields: list[str] = []
    contract_gaps: list[dict[str, Any]] = []
    if "joint_attribution" in graph_set:
        dimension_keys = list(JOINT_DIMENSION_KEYS)
    elif "segment_contribution" in graph_set or "segment_bridge" in graph_set:
        dimension_keys = list(SEGMENT_DIMENSION_KEYS)
    else:
        dimension_keys = []

    if schema:
        dimension_keys = [field for field in dimension_keys if field in schema]
        for item in dimensions:
            field = item["field"]
            if field in schema and field not in dimension_keys:
                dimension_keys.append(field)

    if "user_mix_contribution" in graph_set:
        optional_fields.append("user_mix_bucket")
    if "high_value_user_contribution" in graph_set:
        for field in ("high_value_amount", "high_value_paid_users", "value_percentile"):
            if not schema or field in schema:
                optional_fields.append(field)
        _append_contract_gap(contract_gaps, "high_value_user_contract_missing")
    if "evidence_quality" in axes:
        for field in ("payment_status", "pay_status", "status", "order_id", "payment_order_id"):
            if field in schema:
                optional_fields.append(field)
    for field in (item["field"] for item in dimensions):
        gap_id = DIMENSION_CONTRACT_GAPS.get(field)
        if gap_id:
            _append_contract_gap(contract_gaps, gap_id)
    if "event_impact" in axes:
        _append_contract_gap(contract_gaps, "event_context_contract_missing")
    if "evidence_quality" in axes:
        _append_contract_gap(contract_gaps, "payment_status_contract_missing")
        _append_contract_gap(contract_gaps, "duplicate_order_contract_missing")

    return {
        "shape_id": "revenue_daily_diagnostic",
        "source": "clickhouse",
        "grain": "business_date_lagos_by_group",
        "required_fields": REQUIRED_FIELDS,
        "optional_fields": tuple(dict.fromkeys(optional_fields)),
        "dimension_keys": tuple(dimension_keys),
        "contract_gaps": tuple(contract_gaps),
        **({"schema_fields": schema_fields} if schema_fields else {}),
    }


def _append_contract_gap(contract_gaps: list[dict[str, Any]], gap_id: str) -> None:
    descriptor = {"gap_id": gap_id, **CONTRACT_GAP_DESCRIPTORS[gap_id]}
    if descriptor not in contract_gaps:
        contract_gaps.append(descriptor)


def _capability_params(
    graph: tuple[str, ...],
    baselines: tuple[str, ...],
    dimensions: tuple[dict[str, Any], ...],
    bound_context: Mapping[str, Any],
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    pattern_params = bound_context.get("pattern_params")
    if isinstance(pattern_params, Mapping):
        pattern_params = dict(pattern_params)
    else:
        pattern_params = {}
    if "rolling_window_compare" in graph:
        params["rolling_window_compare"] = {
            "window_days": int(pattern_params.get("window_days") or 7),
            "baseline": "rolling_7_day_baseline",
        }
    if "segment_contribution" in graph:
        params["segment_contribution"] = {"top_k": 5, "min_sample_size": 10}
    if "joint_attribution" in graph:
        params["joint_attribution"] = {
            "max_dimension_count": 2,
            "candidate_dimensions": tuple(item["field"] for item in dimensions),
            "min_sample_size": 10,
        }
    if "compare_periods" in graph:
        params["compare_periods"] = {"baselines": baselines or ("previous_day",)}
    return params


def _reusable_asset_rows(
    graph: tuple[str, ...],
    prior_assets: Iterable[Mapping[str, Any]],
    *,
    target_metric: str,
    scope: str,
    time_window: str,
    windows: Mapping[str, Any],
    baselines: tuple[str, ...],
    permission_scope: str,
    snapshot_version: str,
    contract_versions: Mapping[str, str],
    schema_fingerprint: str,
    required_fields: tuple[str, ...],
) -> tuple[dict[str, Any], ...]:
    needed_dimensions = _required_dimension_scan_dimensions(graph)
    if not needed_dimensions:
        return ()
    return reusable_dimension_scan_inputs(
        prior_assets,
        target_metric=target_metric,
        scope=scope,
        time_window=time_window,
        windows=windows,
        baselines=baselines,
        permission_scope=permission_scope,
        snapshot_version=snapshot_version,
        required_dimensions=tuple(needed_dimensions),
        required_fields=required_fields,
        contract_versions=contract_versions,
        schema_fingerprint=schema_fingerprint,
    )


def _required_dimension_scan_dimensions(graph: tuple[str, ...]) -> frozenset[str]:
    if "segment_contribution" in graph or "segment_bridge" in graph:
        return frozenset(("channel",))
    return frozenset()


def _query_intents(
    graph: tuple[str, ...],
    axes: tuple[str, ...],
    reusable_assets: tuple[str, ...],
    row_shape: Mapping[str, Any],
) -> tuple[str, ...]:
    intents = ["daily_metric_baselines"]
    if reusable_assets:
        intents.append("dimension_scan_reuse")
    if "segment_contribution" in graph and not reusable_assets:
        intents.append("dimension_scan")
    if "joint_attribution" in graph:
        intents.append("joint_candidate_scan")
    if "high_value_user_contribution" in graph and _has_all_optional_fields(
        row_shape, ("high_value_amount", "high_value_paid_users")
    ):
        intents.append("high_value_scan")
    if "data_quality_profile" in graph:
        intents.append("data_quality_probe")
    if "event_impact" in axes:
        intents.append("event_context_probe")
    return tuple(dict.fromkeys(intents))


def _has_all_optional_fields(row_shape: Mapping[str, Any], fields: tuple[str, ...]) -> bool:
    optional = set(row_shape.get("optional_fields") or ())
    return all(field in optional for field in fields)


DIMENSION_CONTRACT_GAPS = {
    "package_name": "package_name_contract_missing",
    "gameplay_id": "gameplay_contract_missing",
}


def _label_from_bound_item(value: Any) -> str:
    if isinstance(value, Mapping):
        label = value.get("label") or value.get("name") or value.get("value")
        return str(label or "")
    return str(value or "")


def _scope(bound_context: Mapping[str, Any]) -> str:
    return str(bound_context.get("scope") or "full_sample")


def _permission_scope(bound_context: Mapping[str, Any]) -> str:
    return str(bound_context.get("permission_scope") or "analyst")


def _snapshot_version(bound_context: Mapping[str, Any]) -> str:
    return str(bound_context.get("snapshot_version") or "")


def _contract_versions(bound_context: Mapping[str, Any]) -> dict[str, str]:
    source = bound_context.get("contract_versions") or bound_context.get("contract_version")
    if isinstance(source, Mapping):
        return {
            str(key): str(value)
            for key, value in source.items()
            if key not in ("", None) and value not in ("", None)
        }
    if source not in ("", None):
        return {"runtime": str(source)}
    return {}


def _schema_fields(bound_context: Mapping[str, Any]) -> tuple[str, ...]:
    schema_fields = bound_context.get("schema_fields") or bound_context.get("clickhouse_schema_fields")
    if isinstance(schema_fields, Iterable) and not isinstance(schema_fields, (str, bytes, Mapping)):
        return tuple(str(field) for field in schema_fields if field)
    return ()


def _schema_fingerprint(
    bound_context: Mapping[str, Any],
    *,
    row_shape: Mapping[str, Any],
    required_dimensions: frozenset[str],
) -> str:
    explicit = bound_context.get("schema_fingerprint")
    if explicit not in ("", None):
        return str(explicit)
    schema_fields = bound_context.get("schema_fields") or bound_context.get("clickhouse_schema_fields")
    if isinstance(schema_fields, Iterable) and not isinstance(schema_fields, (str, bytes, Mapping)):
        fields = tuple(str(field) for field in schema_fields if field)
    else:
        fields = tuple(str(field) for field in row_shape.get("required_fields") or ())
    payload = {
        "fields": fields,
        "required_dimensions": tuple(sorted(required_dimensions)),
        "grain": row_shape.get("grain"),
        "source": row_shape.get("source"),
    }
    return hashlib.sha1(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
