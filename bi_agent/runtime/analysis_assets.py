from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import hashlib
import json
from typing import Any

from bi_agent.runtime.artifacts import to_jsonable


MAX_TOPIC_ANALYSIS_ASSETS = 20
REUSABLE_VERIFIER_STATUSES = frozenset(("passed", "verified"))
REUSABLE_BUSINESS_TRUTH_WORDING_LIMITS = frozenset(("supported", "quantified", "stable_pattern"))
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
        strength = str(claim.get("strength") or "")
        evidence_type = str(claim.get("evidence_type") or "")
        wording_limit = str(claim.get("wording_limit") or "")
        evidence_types = _string_list(claim.get("evidence_types"))
        strengths = _string_list(claim.get("strengths"))
        wording_limits = _string_list(claim.get("wording_limits"))
        effective_evidence_types = evidence_types or ((evidence_type,) if evidence_type else ())
        effective_strengths = strengths or ((strength,) if strength else ())
        effective_wording_limits = wording_limits or ((wording_limit,) if wording_limit else ())
        can_support_business_truth = (
            verifier_status in REUSABLE_VERIFIER_STATUSES
            and bool(evidence_refs)
            and bool(effective_strengths)
            and all(item in {"high", "medium"} for item in effective_strengths)
            and all(item not in CONTEXT_ONLY_EVIDENCE_TYPES for item in effective_evidence_types)
            and bool(effective_wording_limits)
            and all(
                item in REUSABLE_BUSINESS_TRUTH_WORDING_LIMITS
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
    if query_intent != "dimension_scan":
        return None
    dimensions = _dimension_keys(admin, plan)
    query_ref = _query_ref(answer_package, admin)
    if not dimensions or not query_ref:
        return None
    asset: dict[str, Any] = {
        "asset_type": "dimension_scan",
        "status": "usable",
        "source_run_id": source_run_id,
        "dimensions": dimensions,
        "query_ref": query_ref,
        "query_intent": "dimension_scan",
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
        for key in ("query_ref", "query_intent"):
            value = asset.get(key)
            if value:
                payload[key] = value
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
