from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any

from bi_agent.runtime.artifacts import to_jsonable


MAX_TOPIC_ANALYSIS_ASSETS = 20
MAX_REUSABLE_DIMENSION_SCAN_ROWS = 200
DIMENSION_SCAN_ASSET_TTL = timedelta(hours=12)
REUSABLE_VERIFIER_STATUSES = frozenset(("passed", "verified"))
REUSABLE_BUSINESS_TRUTH_WORDING_LIMITS = frozenset(("supported", "quantified", "stable_pattern"))
NON_REUSABLE_METADATA_MARKERS = frozenset(("missing", "unknown"))
CONTEXT_ONLY_EVIDENCE_TYPES = frozenset(
    (
        "candidate_mechanism",
        "contextual_evidence",
        "insufficient",
        "insufficient_evidence",
        "permission_limited",
        "data_gap",
    )
)
CONTEXT_ONLY_WORDING_LIMITS = frozenset(("candidate", "contextual", "insufficient"))


def build_analysis_assets(answer_package: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    admin = _admin_audit(answer_package)
    assets: list[dict[str, Any]] = []
    source_run_id = str(answer_package.get("run_id") or "")
    plan = admin.get("compiler_runtime_plan")
    if isinstance(plan, Mapping) and plan:
        assets.append(
            {
                "asset_type": "compiler_runtime_plan",
                "status": "context_only",
                "source_run_id": source_run_id,
                "payload": dict(plan),
            }
        )
    scan_asset = _dimension_scan_asset(answer_package, admin, source_run_id)
    if scan_asset:
        assets.append(scan_asset)
    for item in admin.get("contract_gap_diagnostics") or ():
        if isinstance(item, Mapping):
            assets.append(
                {
                    "asset_type": "contract_gap_diagnostic",
                    "status": str(item.get("status") or "unknown"),
                    "source_run_id": source_run_id,
                    "payload": dict(item),
                }
            )
    for claim in _summary_claim_groups(answer_package):
        evidence_refs = tuple(str(ref) for ref in claim.get("evidence_refs") or ())
        limitations = tuple(str(item) for item in claim.get("limitations") or ())
        verifier_status = str(claim.get("verifier_status") or "")
        strength = _metadata_value(claim.get("strength"))
        evidence_type = _metadata_value(claim.get("evidence_type"))
        wording_limit = _metadata_value(claim.get("wording_limit"))
        evidence_types = _metadata_list(claim.get("evidence_types"))
        strengths = _metadata_list(claim.get("strengths"))
        wording_limits = _metadata_list(claim.get("wording_limits"))
        effective_evidence_types = evidence_types or (evidence_type,)
        effective_strengths = strengths or (strength,)
        effective_wording_limits = wording_limits or (wording_limit,)
        can_support_business_truth = (
            verifier_status in REUSABLE_VERIFIER_STATUSES
            and bool(evidence_refs)
            and bool(effective_strengths)
            and all(item in {"high", "medium"} for item in effective_strengths)
            and all(item not in NON_REUSABLE_METADATA_MARKERS for item in effective_evidence_types)
            and all(item not in CONTEXT_ONLY_EVIDENCE_TYPES for item in effective_evidence_types)
            and bool(effective_wording_limits)
            and all(
                item not in NON_REUSABLE_METADATA_MARKERS
                and item in REUSABLE_BUSINESS_TRUTH_WORDING_LIMITS
                and item not in CONTEXT_ONLY_WORDING_LIMITS
                for item in effective_wording_limits
            )
            and not limitations
        )
        assets.append(
            {
                "asset_type": "claim_context_slot",
                "status": "claim_supported" if can_support_business_truth else "context_only",
                "source_run_id": source_run_id,
                "text": str(claim.get("text") or ""),
                "evidence_refs": evidence_refs,
                "strength": strength,
                "strengths": effective_strengths,
                "evidence_type": evidence_type,
                "evidence_types": effective_evidence_types,
                "limitations": limitations,
                "verifier_status": verifier_status,
                "wording_limit": wording_limit,
                "wording_limits": effective_wording_limits,
                "can_support_business_truth": can_support_business_truth,
                "target_metric": str(claim.get("target_metric") or ""),
                "scope": str(claim.get("scope") or ""),
                "time_window": str(claim.get("time_window") or ""),
            }
        )
    return normalize_analysis_assets(assets)


def merge_analysis_assets(
    *asset_groups: Iterable[Mapping[str, Any]],
    limit: int = MAX_TOPIC_ANALYSIS_ASSETS,
) -> tuple[dict[str, Any], ...]:
    merged: list[Mapping[str, Any]] = []
    for group in asset_groups:
        merged.extend(group or ())
    return normalize_analysis_assets(merged, limit=limit)


def normalize_analysis_assets(
    assets: Iterable[Mapping[str, Any]],
    *,
    limit: int = MAX_TOPIC_ANALYSIS_ASSETS,
) -> tuple[dict[str, Any], ...]:
    if limit <= 0:
        return ()
    normalized = [_normalized_asset(asset) for asset in assets if isinstance(asset, Mapping)]
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for asset in reversed(normalized):
        key = asset_dedup_key(asset)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(asset)
        if len(deduped) >= limit:
            break
    deduped.reverse()
    return tuple(deduped)


def _metadata_value(value: Any) -> str:
    text = str(value or "").strip()
    return text or "missing"


def _metadata_list(value: Any) -> tuple[str, ...]:
    values = _string_list(value)
    if values:
        return tuple(item if item else "missing" for item in values)
    return ()


def asset_dedup_key(asset: Mapping[str, Any]) -> str:
    payload = _asset_identity_payload(_normalized_asset(asset))
    encoded = json.dumps(to_jsonable(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()


def asset_dimensions(asset: Mapping[str, Any]) -> tuple[str, ...]:
    dimensions = asset.get("dimensions")
    if isinstance(dimensions, Sequence) and not isinstance(dimensions, (str, bytes)):
        values = tuple(str(item) for item in dimensions if item)
        if values:
            return values
    dimension = str(asset.get("dimension") or "")
    return (dimension,) if dimension else ()


def _admin_audit(answer_package: Mapping[str, Any]) -> Mapping[str, Any]:
    admin = answer_package.get("admin_audit")
    if isinstance(admin, Mapping):
        return admin
    return _section_payload(answer_package, "admin_audit")


def _summary_claim_groups(answer_package: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    payload = _section_payload(answer_package, "summary")
    groups = payload.get("claim_groups") or ()
    return tuple(item for item in groups if isinstance(item, Mapping))


def _section_payload(answer_package: Mapping[str, Any], section_id: str) -> Mapping[str, Any]:
    for section in answer_package.get("sections") or ():
        if not isinstance(section, Mapping):
            continue
        current_id = section.get("section_id") or section.get("id")
        if current_id == section_id:
            payload = section.get("payload")
            if isinstance(payload, Mapping):
                return payload
            return {}
    return {}


def _dimension_scan_asset(
    answer_package: Mapping[str, Any],
    admin: Mapping[str, Any],
    source_run_id: str,
) -> dict[str, Any] | None:
    plan = admin.get("compiler_runtime_plan")
    if not isinstance(plan, Mapping):
        return None
    query_intent = _query_intent(admin, plan)
    if query_intent not in {"dimension_scan", "dimension_scan_reuse"}:
        return None
    dimensions = _dimension_keys(admin, plan)
    query_ref = _query_ref(answer_package, admin)
    if not dimensions or not query_ref:
        return None
    created_at = _created_at(answer_package)
    expires_at = _expires_at(created_at)
    row_payload = _dimension_scan_row_payload(admin, query_intent)
    result_refs = _result_refs(admin, query_ref)
    query_identity = _query_identity(admin, query_ref, result_refs)
    reuse_contract = build_dimension_scan_reuse_contract(
        target_metric=plan.get("target_metric"),
        scope=plan.get("scope"),
        time_window=plan.get("time_window"),
        windows=plan.get("windows"),
        baselines=plan.get("baselines"),
        permission_scope=answer_package.get("permission_scope"),
        snapshot_version=answer_package.get("snapshot_id") or answer_package.get("snapshot"),
        dimensions=dimensions,
        required_fields=_required_fields(plan),
        contract_versions=plan.get("contract_versions"),
        schema_fingerprint=plan.get("schema_fingerprint"),
    )
    reusable = _dimension_scan_rows_complete(row_payload)
    asset: dict[str, Any] = {
        "asset_type": "dimension_scan",
        "status": "usable" if reusable else "context_only",
        "source_run_id": source_run_id,
        "dimensions": dimensions,
        "query_ref": query_ref,
        "query_intent": "dimension_scan",
        "created_at": created_at,
        "expires_at": expires_at,
        "snapshot_version": str(answer_package.get("snapshot_id") or answer_package.get("snapshot") or ""),
        "permission_scope": str(answer_package.get("permission_scope") or answer_package.get("visibility") or ""),
        "result_refs": result_refs,
        "query_identity": query_identity,
        "reuse_contract": reuse_contract,
        "contract_versions": reuse_contract.get("contract_versions", {}),
        "schema_fingerprint": reuse_contract.get("schema_fingerprint", ""),
        "row_payload": row_payload,
        "applicable_scans": ("dimension_scan",),
    }
    if len(dimensions) == 1:
        asset["dimension"] = dimensions[0]
    return asset


def _query_intent(admin: Mapping[str, Any], plan: Mapping[str, Any]) -> str:
    row_query_plan = admin.get("row_query_plan")
    if isinstance(row_query_plan, Mapping):
        explicit = str(row_query_plan.get("query_intent") or "")
        if explicit:
            return explicit
        query_id = str(row_query_plan.get("query_id") or "")
        if ":" in query_id:
            candidate = query_id.rsplit(":", 1)[-1]
            if candidate:
                return candidate
        reason = str(row_query_plan.get("reason") or "")
        if reason:
            return reason
    query_intents = plan.get("query_intents")
    if isinstance(query_intents, Sequence) and not isinstance(query_intents, (str, bytes)):
        if "dimension_scan" in query_intents:
            return "dimension_scan"
    return ""


def _dimension_keys(admin: Mapping[str, Any], plan: Mapping[str, Any]) -> tuple[str, ...]:
    row_query_plan = admin.get("row_query_plan")
    if isinstance(row_query_plan, Mapping):
        dimensions = row_query_plan.get("dimension_keys")
        if isinstance(dimensions, Sequence) and not isinstance(dimensions, (str, bytes)):
            keys = tuple(str(item) for item in dimensions if item)
            if keys:
                return keys
    row_shapes = plan.get("row_shapes") or ()
    if isinstance(row_shapes, Sequence) and not isinstance(row_shapes, (str, bytes)):
        for row_shape in row_shapes:
            if not isinstance(row_shape, Mapping):
                continue
            dimensions = row_shape.get("dimension_keys") or ()
            if isinstance(dimensions, Sequence) and not isinstance(dimensions, (str, bytes)):
                keys = tuple(str(item) for item in dimensions if item)
                if keys:
                    return keys
    return ()


def _query_ref(answer_package: Mapping[str, Any], admin: Mapping[str, Any]) -> str:
    row_query_plan = admin.get("row_query_plan")
    if isinstance(row_query_plan, Mapping):
        refs = row_query_plan.get("result_refs")
        if isinstance(refs, Sequence) and not isinstance(refs, (str, bytes)):
            for ref in refs:
                value = str(ref or "")
                if value:
                    return value
        for key in ("query_hash", "query_ref", "query_id"):
            value = str(row_query_plan.get(key) or "")
            if value:
                return value
    for section in answer_package.get("sections") or ():
        if not isinstance(section, Mapping):
            continue
        payload = section.get("payload")
        if not isinstance(payload, Mapping):
            continue
        evidence = payload.get("evidence") or ()
        if not isinstance(evidence, Sequence) or isinstance(evidence, (str, bytes)):
            continue
        for item in evidence:
            if not isinstance(item, Mapping):
                continue
            refs = item.get("result_refs") or ()
            if isinstance(refs, Sequence) and not isinstance(refs, (str, bytes)):
                for ref in refs:
                    value = str(ref or "")
                    if value:
                        return value
    return ""


def _result_refs(admin: Mapping[str, Any], query_ref: str) -> list[str]:
    row_query_plan = admin.get("row_query_plan")
    if isinstance(row_query_plan, Mapping):
        refs = row_query_plan.get("result_refs")
        if isinstance(refs, Sequence) and not isinstance(refs, (str, bytes)):
            values = [str(ref) for ref in refs if ref]
            if values:
                return values
    return [query_ref] if query_ref else []


def _query_identity(
    admin: Mapping[str, Any],
    query_ref: str,
    result_refs: Sequence[str],
) -> dict[str, Any]:
    row_query_plan = admin.get("row_query_plan")
    if not isinstance(row_query_plan, Mapping):
        row_query_plan = {}
    return {
        "query_ref": query_ref,
        "query_id": str(row_query_plan.get("query_id") or ""),
        "query_hash": str(row_query_plan.get("query_hash") or ""),
        "result_refs": [str(ref) for ref in result_refs if ref],
    }


def _dimension_scan_row_payload(
    admin: Mapping[str, Any],
    query_intent: str,
) -> dict[str, Any]:
    row_query_plan = admin.get("row_query_plan")
    if not isinstance(row_query_plan, Mapping):
        return {"rows": [], "row_count": 0, "truncated": False}
    rows_by_intent = row_query_plan.get("rows_by_intent")
    rows: Sequence[Any] = ()
    if isinstance(rows_by_intent, Mapping):
        intent_rows = rows_by_intent.get(query_intent)
        if isinstance(intent_rows, Sequence) and not isinstance(intent_rows, (str, bytes)):
            rows = intent_rows
    if not rows:
        raw_rows = row_query_plan.get("rows")
        if isinstance(raw_rows, Sequence) and not isinstance(raw_rows, (str, bytes)):
            rows = raw_rows
    return bounded_row_payload(rows)


def bounded_row_payload(rows: Sequence[Any], *, limit: int = MAX_REUSABLE_DIMENSION_SCAN_ROWS) -> dict[str, Any]:
    bounded_rows = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        bounded_rows.append(dict(to_jsonable(row)))
        if len(bounded_rows) >= limit:
            break
    row_count = len(rows)
    return {
        "rows": bounded_rows,
        "row_count": row_count,
        "truncated": row_count > len(bounded_rows),
    }


def build_dimension_scan_reuse_contract(
    *,
    target_metric: Any,
    scope: Any,
    time_window: Any,
    windows: Any,
    baselines: Any,
    permission_scope: Any,
    snapshot_version: Any,
    dimensions: Sequence[str],
    required_fields: Sequence[str],
    contract_versions: Any = None,
    schema_fingerprint: Any = None,
    query_intent: str = "dimension_scan",
) -> dict[str, Any]:
    contract = {
        "target_metric": str(target_metric or ""),
        "scope": str(scope or ""),
        "time_window": str(time_window or ""),
        "windows": _mapping_jsonable(windows),
        "baselines": list(_string_list(baselines)),
        "permission_scope": str(permission_scope or ""),
        "snapshot_version": str(snapshot_version or ""),
        "dimensions": [str(item) for item in dimensions if item],
        "required_fields": [str(item) for item in required_fields if item],
        "contract_versions": _mapping_jsonable(contract_versions),
        "schema_fingerprint": str(schema_fingerprint or ""),
        "query_intent": str(query_intent or "dimension_scan"),
    }
    signature_payload = {
        key: contract[key]
        for key in (
            "target_metric",
            "scope",
            "time_window",
            "windows",
            "baselines",
            "dimensions",
            "required_fields",
            "contract_versions",
            "schema_fingerprint",
            "query_intent",
        )
    }
    contract["contract_signature"] = hashlib.sha1(
        json.dumps(
            signature_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return contract


def reusable_dimension_scan_inputs(
    prior_assets: Iterable[Mapping[str, Any]],
    *,
    target_metric: str,
    scope: str,
    time_window: str,
    windows: Mapping[str, Any] | None,
    baselines: Sequence[str],
    permission_scope: str,
    snapshot_version: str,
    required_dimensions: Sequence[str],
    required_fields: Sequence[str],
    contract_versions: Any = None,
    schema_fingerprint: Any = None,
    now: datetime | None = None,
) -> tuple[dict[str, Any], ...]:
    expected_contract = build_dimension_scan_reuse_contract(
        target_metric=target_metric,
        scope=scope,
        time_window=time_window,
        windows=windows,
        baselines=baselines,
        permission_scope=permission_scope,
        snapshot_version=snapshot_version,
        dimensions=required_dimensions,
        required_fields=required_fields,
        contract_versions=contract_versions,
        schema_fingerprint=schema_fingerprint,
    )
    required = set(str(item) for item in required_dimensions if item)
    covered: set[str] = set()
    matched: list[dict[str, Any]] = []
    for asset in prior_assets:
        if not _is_reusable_dimension_scan_candidate(
            asset,
            expected_contract=expected_contract,
            now=now,
        ):
            continue
        dimensions = set(asset_dimensions(asset))
        overlap = dimensions.intersection(required)
        if not overlap:
            continue
        row_payload = asset.get("row_payload")
        if not isinstance(row_payload, Mapping):
            continue
        rows = row_payload.get("rows")
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            continue
        normalized_rows = [dict(row) for row in rows if isinstance(row, Mapping)]
        if not _rows_cover_required_fields(
            normalized_rows,
            required_fields=required_fields,
            required_dimensions=required,
        ):
            continue
        matched.append(
            {
                "query_ref": str(asset.get("query_ref") or ""),
                "result_refs": [str(ref) for ref in (asset.get("result_refs") or ()) if ref],
                "dimensions": list(asset_dimensions(asset)),
                "rows": normalized_rows,
                "row_count": int(row_payload.get("row_count") or 0),
                "created_at": str(asset.get("created_at") or ""),
                "expires_at": str(asset.get("expires_at") or ""),
            }
        )
        covered.update(overlap)
    if required and not required.issubset(covered):
        return ()
    return tuple(
        item for item in matched if item.get("query_ref") and item.get("rows")
    )


def _is_reusable_dimension_scan_asset(asset: Mapping[str, Any]) -> bool:
    if str(asset.get("asset_type") or "") != "dimension_scan":
        return False
    if str(asset.get("status") or "") != "usable":
        return False
    if not asset.get("query_ref"):
        return False
    applicable = asset.get("applicable_scans") or asset.get("applicable_scan") or "dimension_scan"
    if isinstance(applicable, str):
        applicable_scans = {applicable}
    elif isinstance(applicable, Iterable) and not isinstance(applicable, (str, bytes, Mapping)):
        applicable_scans = {str(item) for item in applicable if item}
    else:
        applicable_scans = set()
    return "dimension_scan" in applicable_scans


def _is_reusable_dimension_scan_candidate(
    asset: Mapping[str, Any],
    *,
    expected_contract: Mapping[str, Any],
    now: datetime | None,
) -> bool:
    if not _is_reusable_dimension_scan_asset(asset):
        return False
    if not _dimension_scan_asset_fresh(asset, now=now):
        return False
    contract = asset.get("reuse_contract")
    if not isinstance(contract, Mapping):
        return False
    for key in ("permission_scope", "snapshot_version", "scope", "target_metric", "time_window"):
        if str(contract.get(key) or "") != str(expected_contract.get(key) or ""):
            return False
    if _mapping_jsonable(contract.get("windows")) != _mapping_jsonable(expected_contract.get("windows")):
        return False
    if list(_string_list(contract.get("baselines"))) != list(_string_list(expected_contract.get("baselines"))):
        return False
    if _mapping_jsonable(contract.get("contract_versions")) != _mapping_jsonable(expected_contract.get("contract_versions")):
        return False
    if str(contract.get("schema_fingerprint") or "") != str(expected_contract.get("schema_fingerprint") or ""):
        return False
    query_intent = str(contract.get("query_intent") or "dimension_scan")
    if query_intent not in {"dimension_scan", "dimension_scan_reuse"}:
        return False
    row_payload = asset.get("row_payload")
    if not isinstance(row_payload, Mapping):
        return False
    return _dimension_scan_rows_complete(row_payload)


def _dimension_scan_asset_fresh(
    asset: Mapping[str, Any],
    *,
    now: datetime | None,
) -> bool:
    created_at = _parse_datetime(asset.get("created_at"))
    expires_at = _parse_datetime(asset.get("expires_at"))
    if created_at is None or expires_at is None:
        return False
    current = now or datetime.now(timezone.utc)
    return created_at <= current <= expires_at


def _dimension_scan_rows_complete(row_payload: Mapping[str, Any]) -> bool:
    rows = row_payload.get("rows")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return False
    row_count = int(row_payload.get("row_count") or 0)
    if row_count <= 0:
        return False
    if bool(row_payload.get("truncated")):
        return False
    return row_count == len(rows)


def _rows_cover_required_fields(
    rows: Sequence[Mapping[str, Any]],
    *,
    required_fields: Sequence[str],
    required_dimensions: set[str],
) -> bool:
    if not rows:
        return False
    expected_fields = {str(field) for field in required_fields if field}
    expected_fields.update(required_dimensions)
    if not expected_fields:
        return True
    for row in rows:
        if not expected_fields.issubset(set(str(key) for key in row.keys())):
            return False
    return True


def _created_at(answer_package: Mapping[str, Any]) -> str:
    candidate = _parse_datetime(answer_package.get("created_at"))
    if candidate is None:
        candidate = datetime.now(timezone.utc)
    return candidate.isoformat()


def _expires_at(created_at: str) -> str:
    parsed = _parse_datetime(created_at)
    if parsed is None:
        parsed = datetime.now(timezone.utc)
    return (parsed + DIMENSION_SCAN_ASSET_TTL).isoformat()


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _required_fields(plan: Mapping[str, Any]) -> tuple[str, ...]:
    row_shapes = plan.get("row_shapes") or ()
    if not isinstance(row_shapes, Sequence) or isinstance(row_shapes, (str, bytes)):
        return ()
    for row_shape in row_shapes:
        if not isinstance(row_shape, Mapping):
            continue
        required_fields = row_shape.get("required_fields")
        if isinstance(required_fields, Sequence) and not isinstance(required_fields, (str, bytes)):
            values = tuple(str(item) for item in required_fields if item)
            if values:
                return values
    return ()


def _mapping_jsonable(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): to_jsonable(item)
        for key, item in value.items()
        if item not in ("", None, (), [], {})
    }


def _normalized_asset(asset: Mapping[str, Any]) -> dict[str, Any]:
    normalized = to_jsonable(dict(asset))
    if not isinstance(normalized, dict):
        return {}
    dimensions = asset_dimensions(normalized)
    if dimensions:
        normalized["dimensions"] = list(dimensions)
        if len(dimensions) == 1:
            normalized["dimension"] = dimensions[0]
    return normalized


def _string_list(values: Any) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return ()
    return tuple(str(item) for item in values if item)


def _asset_identity_payload(asset: Mapping[str, Any]) -> dict[str, Any]:
    asset_type = str(asset.get("asset_type") or "")
    if asset_type == "dimension_scan":
        payload = {
            "asset_type": asset_type,
            "dimensions": list(asset_dimensions(asset)),
        }
        for key in ("query_ref", "query_intent", "snapshot_version", "permission_scope"):
            value = asset.get(key)
            if value:
                payload[key] = value
        contract = asset.get("reuse_contract")
        if isinstance(contract, Mapping):
            signature = str(contract.get("contract_signature") or "")
            if signature:
                payload["contract_signature"] = signature
        return payload
    if asset_type == "claim_context_slot":
        payload = {
            "asset_type": asset_type,
            "text": str(asset.get("text") or ""),
            "evidence_refs": list(asset.get("evidence_refs") or ()),
        }
        for key in ("target_metric", "scope", "time_window"):
            value = asset.get(key)
            if value:
                payload[key] = value
        return payload
    return {
        key: value
        for key, value in asset.items()
        if key not in {"asset_id", "created_at", "recorded_at", "source_run_id"}
    }
