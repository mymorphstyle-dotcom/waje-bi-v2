from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any

from bi_agent.runtime.analysis_assets import (
    build_dimension_scan_reuse_contract,
    reusable_dimension_scan_inputs,
)


BASE_MEASURES = ("amount", "paid_users", "orders", "first_paid_users")
REQUIRED_FIELDS = ("period", "group", *BASE_MEASURES)
PAYMENT_STATUS_FIELDS = ("payment_status", "pay_status", "status")
ORDER_ID_FIELDS = ("order_id", "payment_order_id")
REVENUE_COMPONENT_FIELDS = (
    "paid_frequency",
    "avg_order_amount",
    "first_pay_user_share",
    "payment_success_rate",
)
SEGMENT_DIMENSION_KEYS = ("channel",)
JOINT_DIMENSION_KEYS = ("channel", "payment_method", "region", "device_brand")
_INVALID_REFERENCE_TIME = object()
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
    analysis_contract, query_contracts, capability_execution_plans = _contract_projection(
        normalized_context
    )
    reuse_signature = _reuse_signature_context(
        normalized_context,
        analysis_contract=analysis_contract,
        query_contracts=query_contracts,
        capability_execution_plans=capability_execution_plans,
    )
    windows = _windows(normalized_context)
    baselines = _baselines(normalized_context, axes, question_text)
    scope = _scope(normalized_context)
    permission_scope = _permission_scope(normalized_context)
    snapshot_version = _snapshot_version(normalized_context)
    contract_versions = _contract_versions(normalized_context)
    schema_fields = _schema_fields(normalized_context)
    dimensions = _dimension_candidates(graph, axes)
    row_shape = _row_shape(graph, axes, dimensions, schema_fields=schema_fields)
    reuse_required_fields = _dimension_scan_required_fields(
        query_contracts,
        fallback=tuple(row_shape["required_fields"]),
    )
    time_bucket_contracts = _time_bucket_contracts(
        graph,
        pattern_family=str(normalized_context.get("pattern_family") or ""),
    )
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
        required_fields=reuse_required_fields,
        now=_reference_time(normalized_context),
        **reuse_signature,
    )
    exact_reuse_rows = tuple(
        item
        for item in reusable_asset_rows
        if item.get("reuse_decision", {}).get("decision") == "reuse"
        and item.get("rows")
    )
    reusable_assets = tuple(item["query_ref"] for item in exact_reuse_rows)
    reuse_decisions = tuple(
        dict(item["reuse_decision"])
        for item in reusable_asset_rows
        if isinstance(item.get("reuse_decision"), Mapping)
    )
    delta_query_descriptors = tuple(
        dict(item["delta_query_descriptor"])
        for item in reusable_asset_rows
        if isinstance(item.get("delta_query_descriptor"), Mapping)
    )
    query_intents = _query_intents(
        graph,
        axes,
        reusable_assets,
        row_shape,
        has_dimension_delta=bool(delta_query_descriptors),
        time_bucket_contracts=time_bucket_contracts,
    )
    reuse_contract = build_dimension_scan_reuse_contract(
        target_metric=target_metric,
        scope=scope,
        time_window=str(normalized_context.get("time_window") or ""),
        windows=windows,
        baselines=baselines,
        permission_scope=permission_scope,
        snapshot_version=snapshot_version,
        dimensions=_required_dimension_scan_dimensions(graph),
        required_fields=reuse_required_fields,
        contract_versions=contract_versions,
        schema_fingerprint=schema_fingerprint,
        **reuse_signature,
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
        "metric_component_contracts": _metric_component_contracts(row_shape),
        "capability_params": _capability_params(graph, baselines, dimensions, normalized_context),
        "capability_inputs": _capability_inputs(
            graph,
            row_shape,
            query_intents,
            time_bucket_contracts=time_bucket_contracts,
        ),
        "query_intents": query_intents,
        "time_bucket_contracts": time_bucket_contracts,
        "row_shapes": (row_shape,),
        "contract_gaps": row_shape["contract_gaps"],
        "asset_inputs_used": reusable_assets,
        "asset_row_inputs": exact_reuse_rows,
        "reuse_decisions": reuse_decisions,
        "delta_query_descriptors": delta_query_descriptors,
        "asset_reuse_contract": reuse_contract,
        "analysis_contract": analysis_contract,
        "query_contracts": query_contracts,
        "capability_execution_plans": capability_execution_plans,
    }


def project_reviewed_contract_gaps(
    plan: Mapping[str, Any],
    stable_business_axes: Iterable[str],
) -> dict[str, Any]:
    """Merge reviewed gap descriptors without changing executable plan structure."""

    projected = dict(plan)
    axes = tuple(dict.fromkeys(str(item) for item in stable_business_axes if item))
    gap_ids: list[str] = []
    if "event_impact" in axes:
        gap_ids.append("event_context_contract_missing")
    if "evidence_quality" in axes:
        gap_ids.extend(
            ("payment_status_contract_missing", "duplicate_order_contract_missing")
        )
    rows = []
    for raw in plan.get("row_shapes") or ():
        row = dict(raw)
        gaps = [dict(item) for item in row.get("contract_gaps") or ()]
        for gap_id in gap_ids:
            _append_contract_gap(gaps, gap_id)
        row["contract_gaps"] = tuple(gaps)
        rows.append(row)
    projected["row_shapes"] = tuple(rows)
    projected["contract_gaps"] = tuple(
        gap
        for row in rows
        for gap in row.get("contract_gaps") or ()
    )
    return projected


def _contract_projection(
    bound_context: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    outcome = bound_context.get("analysis_compile_outcome")
    if outcome is not None:
        analysis = _projection_dict(getattr(outcome, "analysis_contract", None))
        queries = _projection_rows(getattr(outcome, "query_contracts", ()))
        plans = _projection_rows(getattr(outcome, "capability_plans", ()))
        return analysis, queries, plans
    return (
        _projection_dict(bound_context.get("analysis_contract")),
        _projection_rows(bound_context.get("query_contracts") or ()),
        _projection_rows(bound_context.get("capability_execution_plans") or ()),
    )


def _projection_rows(value: Any) -> tuple[dict[str, Any], ...]:
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping)):
        return tuple(
            projected
            for item in value
            if (projected := _projection_dict(item))
        )
    return ()


def _projection_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        projected = to_dict()
        return dict(projected) if isinstance(projected, Mapping) else {}
    if is_dataclass(value):
        return asdict(value)
    return {}


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
    derived_fields: list[str] = []
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
        for field in (*PAYMENT_STATUS_FIELDS, *ORDER_ID_FIELDS):
            if field in schema:
                optional_fields.append(field)
    if "driver_decomposition" in graph_set:
        derived_fields.extend(("paid_frequency", "avg_order_amount", "first_pay_user_share"))
        if _supported_field(schema, PAYMENT_STATUS_FIELDS):
            derived_fields.append("payment_success_rate")
            status_field = _supported_field(schema, PAYMENT_STATUS_FIELDS)
            if status_field and status_field not in optional_fields:
                optional_fields.append(status_field)
        elif "driver_components" in axes or "evidence_quality" in axes:
            _append_contract_gap(contract_gaps, "payment_status_contract_missing")
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
        "derived_fields": tuple(dict.fromkeys(derived_fields)),
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
    now: datetime | None,
    resolved_windows: Any,
    query_contract_signatures: Mapping[str, Any],
    completeness_digest: str,
    completeness_status: str,
    capability_contract_version: str,
    source_snapshot_refs: tuple[str, ...],
    completeness_reports: tuple[Mapping[str, Any], ...],
    result_provenance: tuple[Mapping[str, Any], ...],
    completeness_record_refs: tuple[str, ...],
    completeness_record_digests: tuple[str, ...],
    row_payload: Mapping[str, Any] | None,
    unique_key_fields: tuple[str, ...],
    row_payload_rows_ref: str,
    binding_manifest_ref: str,
    binding_manifest_digest: str,
    evidence_resolver: Any,
    release_resolver: Any,
    rows_loader: Any,
    runtime_registry: Any,
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
        now=now,
        resolved_windows=resolved_windows,
        query_contract_signatures=query_contract_signatures,
        completeness_digest=completeness_digest,
        completeness_status=completeness_status,
        capability_contract_version=capability_contract_version,
        source_snapshot_refs=source_snapshot_refs,
        completeness_reports=completeness_reports,
        result_provenance=result_provenance,
        completeness_record_refs=completeness_record_refs,
        completeness_record_digests=completeness_record_digests,
        row_payload=row_payload,
        unique_key_fields=unique_key_fields,
        row_payload_rows_ref=row_payload_rows_ref,
        binding_manifest_ref=binding_manifest_ref,
        binding_manifest_digest=binding_manifest_digest,
        evidence_resolver=evidence_resolver,
        release_resolver=release_resolver,
        rows_loader=rows_loader,
        runtime_registry=runtime_registry,
    )


def _reuse_signature_context(
    bound_context: Mapping[str, Any],
    *,
    analysis_contract: Mapping[str, Any],
    query_contracts: tuple[Mapping[str, Any], ...],
    capability_execution_plans: tuple[Mapping[str, Any], ...],
) -> dict[str, Any]:
    explicit_query_signatures = bound_context.get("query_contract_signatures")
    if isinstance(explicit_query_signatures, Mapping):
        query_signatures = {
            str(key): str(value)
            for key, value in explicit_query_signatures.items()
            if key and value
        }
    else:
        query_signatures = {
            str(query.get("query_contract_id") or ""): str(
                query.get("contract_signature") or ""
            )
            for query in query_contracts
            if query.get("query_contract_id") and query.get("contract_signature")
        }
    explicit_snapshots = bound_context.get("source_snapshot_refs")
    if isinstance(explicit_snapshots, Iterable) and not isinstance(
        explicit_snapshots,
        (str, bytes, Mapping),
    ):
        snapshot_refs = tuple(
            dict.fromkeys(str(ref) for ref in explicit_snapshots if ref)
        )
    else:
        snapshot_refs = tuple(
            dict.fromkeys(
                str(ref)
                for query in query_contracts
                for ref in query.get("dataset_snapshot_refs") or ()
                if ref
            )
        )
    capability_version = str(
        bound_context.get("capability_contract_version") or ""
    )
    if not capability_version:
        segment_plan = next(
            (
                plan
                for plan in capability_execution_plans
                if plan.get("capability_id") == "segment_contribution"
            ),
            {},
        )
        capability_version = str(
            segment_plan.get("capability_contract_ref") or ""
        )
    return {
        "resolved_windows": (
            bound_context.get("resolved_windows")
            or analysis_contract.get("resolved_windows")
            or ()
        ),
        "query_contract_signatures": query_signatures,
        "completeness_digest": str(
            bound_context.get("completeness_digest") or ""
        ),
        "completeness_status": str(
            bound_context.get("completeness_status") or ""
        ),
        "capability_contract_version": capability_version,
        "source_snapshot_refs": snapshot_refs,
        "completeness_reports": tuple(
            item
            for item in (bound_context.get("completeness_reports") or ())
            if isinstance(item, Mapping)
        ),
        "result_provenance": tuple(
            item
            for item in (bound_context.get("result_provenance") or ())
            if isinstance(item, Mapping)
        ),
        "completeness_record_refs": tuple(
            str(item)
            for item in (bound_context.get("completeness_record_refs") or ())
            if item
        ),
        "completeness_record_digests": tuple(
            str(item)
            for item in (bound_context.get("completeness_record_digests") or ())
            if item
        ),
        "row_payload": (
            bound_context.get("row_payload")
            if isinstance(bound_context.get("row_payload"), Mapping)
            else None
        ),
        "unique_key_fields": tuple(
            str(item)
            for item in (bound_context.get("unique_key_fields") or ())
            if item
        ),
        "row_payload_rows_ref": str(
            bound_context.get("row_payload_rows_ref") or ""
        ),
        "binding_manifest_ref": str(
            bound_context.get("binding_manifest_ref") or ""
        ),
        "binding_manifest_digest": str(
            bound_context.get("binding_manifest_digest") or ""
        ),
        "evidence_resolver": bound_context.get("evidence_resolver"),
        "release_resolver": bound_context.get("release_resolver"),
        "rows_loader": bound_context.get("rows_loader"),
        "runtime_registry": bound_context.get("runtime_registry"),
    }


def _reference_time(bound_context: Mapping[str, Any]) -> Any:
    value = (
        bound_context.get("reference_time")
        or bound_context.get("now")
        or bound_context.get("as_of")
    )
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return _INVALID_REFERENCE_TIME
        return value.astimezone(timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return _INVALID_REFERENCE_TIME
    if parsed.tzinfo is None:
        return _INVALID_REFERENCE_TIME
    return parsed.astimezone(timezone.utc)


def _required_dimension_scan_dimensions(graph: tuple[str, ...]) -> frozenset[str]:
    if "segment_contribution" in graph or "segment_bridge" in graph:
        return frozenset(("channel",))
    return frozenset()


def _dimension_scan_required_fields(
    query_contracts: tuple[Mapping[str, Any], ...],
    *,
    fallback: tuple[str, ...],
) -> tuple[str, ...]:
    for contract in query_contracts:
        if str(contract.get("query_intent") or "") != "dimension_contribution_scan":
            continue
        shape = contract.get("result_shape")
        if not isinstance(shape, Mapping):
            continue
        fields = tuple(str(item) for item in shape.get("required_fields") or () if item)
        if fields:
            return fields
    return fallback


def _query_intents(
    graph: tuple[str, ...],
    axes: tuple[str, ...],
    reusable_assets: tuple[str, ...],
    row_shape: Mapping[str, Any],
    *,
    has_dimension_delta: bool = False,
    time_bucket_contracts: tuple[dict[str, Any], ...] = (),
) -> tuple[str, ...]:
    intents = ["daily_metric_baselines"]
    if "pattern_scan" in graph and any(
        item.get("status") == "supported" for item in time_bucket_contracts
    ):
        intents.append("time_bucket_scan")
    if reusable_assets:
        intents.append("dimension_scan_reuse")
    if has_dimension_delta:
        intents.append("dimension_scan_delta")
    if "segment_contribution" in graph and not reusable_assets and not has_dimension_delta:
        intents.append("dimension_scan")
    if "joint_attribution" in graph:
        intents.append("joint_candidate_scan")
    if "driver_decomposition" in graph and row_shape.get("derived_fields"):
        intents.append("component_driver_scan")
    if "high_value_user_contribution" in graph and _has_all_optional_fields(
        row_shape, ("high_value_amount", "high_value_paid_users")
    ):
        intents.append("high_value_scan")
    if "data_quality_profile" in graph:
        intents.append("data_quality_probe")
    if "event_impact" in axes:
        intents.append("event_context_probe")
    return tuple(dict.fromkeys(intents))


def _capability_inputs(
    graph: tuple[str, ...],
    row_shape: Mapping[str, Any],
    query_intents: tuple[str, ...],
    *,
    time_bucket_contracts: tuple[dict[str, Any], ...] = (),
) -> dict[str, dict[str, Any]]:
    inputs: dict[str, dict[str, Any]] = {}
    required_fields = tuple(row_shape.get("required_fields") or ())
    derived_fields = tuple(row_shape.get("derived_fields") or ())
    dimension_keys = tuple(row_shape.get("dimension_keys") or ())
    for capability in graph:
        if capability == "driver_decomposition":
            inputs[capability] = {
                "preferred_query_intents": _available_intents(
                    query_intents,
                    ("component_driver_scan", "daily_metric_baselines"),
                ),
                "required_fields": tuple(dict.fromkeys((*required_fields, *derived_fields))),
                "dimension_keys": (),
                "gap_policy": "degrade_to_available_components",
            }
        elif capability in {"segment_contribution", "segment_bridge", "user_mix_contribution"}:
            inputs[capability] = {
                "preferred_query_intents": _available_intents(
                    query_intents,
                    ("dimension_scan_reuse", "dimension_scan_delta", "dimension_scan", "joint_candidate_scan", "daily_metric_baselines"),
                ),
                "required_fields": required_fields,
                "dimension_keys": dimension_keys[:1],
                "gap_policy": "degrade_to_available_dimensions",
            }
        elif capability == "joint_attribution":
            inputs[capability] = {
                "preferred_query_intents": _available_intents(
                    query_intents,
                    ("joint_candidate_scan", "dimension_scan", "daily_metric_baselines"),
                ),
                "required_fields": required_fields,
                "dimension_keys": dimension_keys,
                "gap_policy": "degrade_to_available_dimensions",
            }
        elif capability == "high_value_user_contribution":
            inputs[capability] = {
                "preferred_query_intents": _available_intents(
                    query_intents,
                    ("high_value_scan", "dimension_scan", "daily_metric_baselines"),
                ),
                "required_fields": tuple(
                    dict.fromkeys((*required_fields, "high_value_amount", "high_value_paid_users"))
                ),
                "dimension_keys": (),
                "gap_policy": "degrade_to_total_revenue_only",
            }
        elif capability in {"data_quality_profile", "data_quality_check"}:
            inputs[capability] = {
                "preferred_query_intents": _available_intents(
                    query_intents,
                    ("data_quality_probe", "daily_metric_baselines"),
                ),
                "required_fields": required_fields,
                "dimension_keys": (),
                "gap_policy": "report_data_quality_limitations",
            }
        elif capability == "pattern_scan":
            bucket_fields = tuple(
                field
                for contract in time_bucket_contracts
                if contract.get("status") == "supported"
                for field in contract.get("required_fields", ())
            )
            inputs[capability] = {
                "preferred_query_intents": _available_intents(
                    query_intents,
                    ("time_bucket_scan", "daily_metric_baselines"),
                ),
                "required_fields": bucket_fields or required_fields,
                "dimension_keys": (),
                "gap_policy": "degrade_to_available_time_buckets",
            }
    return inputs


def _available_intents(
    query_intents: tuple[str, ...],
    preferred: tuple[str, ...],
) -> tuple[str, ...]:
    available = set(query_intents)
    selected = tuple(intent for intent in preferred if intent in available)
    return selected or preferred


def _has_all_optional_fields(row_shape: Mapping[str, Any], fields: tuple[str, ...]) -> bool:
    optional = set(row_shape.get("optional_fields") or ())
    return all(field in optional for field in fields)


def _supported_field(schema: set[str], candidates: tuple[str, ...]) -> str:
    if not schema:
        return ""
    return next((field for field in candidates if field in schema), "")


def _metric_component_contracts(row_shape: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    derived = set(row_shape.get("derived_fields") or ())
    optional = set(row_shape.get("optional_fields") or ())
    contracts = [
        {
            "component_id": "paid_users",
            "business_name": "付费人数",
            "source_fields": ("user_id",),
            "status": "supported",
        },
        {
            "component_id": "orders",
            "business_name": "付费频次分母订单数",
            "source_fields": (),
            "status": "supported",
        },
        {
            "component_id": "first_paid_users",
            "business_name": "首充人数",
            "source_fields": ("is_first_payment",),
            "status": "supported",
        },
        {
            "component_id": "paid_frequency",
            "business_name": "付费频次",
            "source_fields": ("paid_users", "orders"),
            "status": "supported" if "paid_frequency" in derived else "missing_contract",
        },
        {
            "component_id": "avg_order_amount",
            "business_name": "单笔付费金额",
            "source_fields": ("paid_amount_ngn",),
            "status": "supported" if "avg_order_amount" in derived else "missing_contract",
        },
        {
            "component_id": "first_pay_user_share",
            "business_name": "首充用户占比",
            "source_fields": ("first_paid_users", "paid_users"),
            "status": "supported" if "first_pay_user_share" in derived else "missing_contract",
        },
    ]
    status_field = next((field for field in PAYMENT_STATUS_FIELDS if field in optional), "")
    contracts.append(
        {
            "component_id": "payment_success_rate",
            "business_name": "支付成功率",
            "source_fields": (status_field,) if status_field else PAYMENT_STATUS_FIELDS,
            "status": "supported" if "payment_success_rate" in derived else "missing_contract",
        }
    )
    return tuple(contracts)


def _time_bucket_contracts(
    graph: tuple[str, ...],
    *,
    pattern_family: str,
) -> tuple[dict[str, Any], ...]:
    if "pattern_scan" not in graph:
        return ()
    if pattern_family == "weekly":
        return (
            {
                "bucket_family": "weekly",
                "required_fields": ("week", "weekday", "amount"),
                "status": "supported",
                "date_basis": "business_date_lagos",
            },
        )
    if pattern_family == "intra_period":
        return (
            {
                "bucket_family": "month_phase",
                "required_fields": ("month", "phase", "amount"),
                "status": "supported",
                "date_basis": "business_date_lagos",
            },
        )
    if pattern_family in {"hourly", "intra_day"}:
        return (
            {
                "bucket_family": "hour",
                "required_fields": ("day", "hour", "amount"),
                "status": "missing_contract",
                "gap_id": "hourly_time_contract_missing",
            },
        )
    return ()


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
