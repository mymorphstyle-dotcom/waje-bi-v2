from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any

from bi_agent.runtime.artifacts import to_jsonable
from bi_agent.runtime.authoritative_query_chain import (
    AuthoritativeQueryChainError,
    validate_authoritative_query_chain,
)
from bi_agent.runtime.evidence_authority import (
    EvidenceIntegrityError,
    RowsPayloadLoader,
    RuntimeEvidenceResolver,
    canonical_digest,
    canonical_rows_hash,
    runtime_evidence_record_integrity_errors,
)
from bi_agent.runtime.runtime_contract_registry import (
    RuntimeContractRegistry,
    runtime_registry_integrity_error,
)
from bi_agent.runtime.dataset_catalog import DatasetReleaseResolver


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


def build_analysis_assets(
    answer_package: Mapping[str, Any],
    *,
    evidence_resolver: RuntimeEvidenceResolver | None = None,
    rows_loader: RowsPayloadLoader | None = None,
    runtime_registry: RuntimeContractRegistry | None = None,
    release_resolver: DatasetReleaseResolver | None = None,
) -> tuple[dict[str, Any], ...]:
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
    scan_asset = _dimension_scan_asset(
        answer_package,
        admin,
        source_run_id,
        evidence_resolver=evidence_resolver,
        rows_loader=rows_loader,
        runtime_registry=runtime_registry,
        release_resolver=release_resolver,
    )
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
    *,
    evidence_resolver: RuntimeEvidenceResolver | None,
    rows_loader: RowsPayloadLoader | None,
    runtime_registry: RuntimeContractRegistry | None,
    release_resolver: DatasetReleaseResolver | None,
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
    prior_reuse_contract = plan.get("asset_reuse_contract")
    if not isinstance(prior_reuse_contract, Mapping):
        prior_reuse_contract = {}
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
        resolved_windows=(
            prior_reuse_contract.get("resolved_windows")
            or plan.get("resolved_windows")
        ),
        query_contract_signatures=(
            prior_reuse_contract.get("query_contract_signatures")
            or plan.get("query_contract_signatures")
        ),
        completeness_digest=(
            prior_reuse_contract.get("completeness_digest")
            or plan.get("completeness_digest")
        ),
        completeness_status=(
            prior_reuse_contract.get("completeness_status")
            or plan.get("completeness_status")
        ),
        capability_contract_version=(
            prior_reuse_contract.get("capability_contract_version")
            or plan.get("capability_contract_version")
        ),
        source_snapshot_refs=(
            prior_reuse_contract.get("source_snapshot_refs")
            or plan.get("source_snapshot_refs")
            or ()
        ),
        completeness_reports=(
            prior_reuse_contract.get("completeness_reports")
            or plan.get("completeness_reports")
            or ()
        ),
        result_provenance=(
            prior_reuse_contract.get("result_provenance")
            or plan.get("result_provenance")
            or ()
        ),
        completeness_record_refs=(
            prior_reuse_contract.get("completeness_record_refs")
            or plan.get("completeness_record_refs")
            or ()
        ),
        completeness_record_digests=(
            prior_reuse_contract.get("completeness_record_digests")
            or plan.get("completeness_record_digests")
            or ()
        ),
        row_payload=row_payload,
        unique_key_fields=(
            prior_reuse_contract.get("unique_key_fields")
            or plan.get("unique_key_fields")
            or ()
        ),
        row_payload_rows_ref=str(
            prior_reuse_contract.get("row_payload_rows_ref")
            or plan.get("row_payload_rows_ref")
            or ""
        ),
        binding_manifest_ref=str(
            prior_reuse_contract.get("binding_manifest_ref")
            or plan.get("binding_manifest_ref")
            or ""
        ),
        binding_manifest_digest=str(
            prior_reuse_contract.get("binding_manifest_digest")
            or plan.get("binding_manifest_digest")
            or ""
        ),
        evidence_resolver=evidence_resolver,
        rows_loader=rows_loader,
        runtime_registry=runtime_registry,
        release_resolver=release_resolver,
    )
    reusable = (
        _dimension_scan_rows_complete(row_payload)
        and not _missing_exact_reuse_signature_field(reuse_contract)
        and reuse_contract.get("completeness_status") == "complete"
    )
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
        "rows_refs": reuse_contract.get("rows_refs", ()),
        "completeness_report_refs": reuse_contract.get(
            "completeness_report_refs", ()
        ),
        "completeness_record_refs": reuse_contract.get(
            "completeness_record_refs", ()
        ),
        "completeness_record_digests": reuse_contract.get(
            "completeness_record_digests", ()
        ),
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
    resolved_windows: Any = None,
    query_contract_signatures: Any = None,
    completeness_digest: Any = None,
    completeness_status: Any = None,
    capability_contract_version: Any = None,
    source_snapshot_refs: Sequence[str] = (),
    completeness_reports: Sequence[Any] = (),
    result_provenance: Sequence[Mapping[str, Any]] = (),
    completeness_record_refs: Sequence[str] = (),
    completeness_record_digests: Sequence[str] = (),
    row_payload: Mapping[str, Any] | None = None,
    unique_key_fields: Sequence[str] = (),
    row_payload_rows_ref: str = "",
    binding_manifest_ref: str = "",
    binding_manifest_digest: str = "",
    evidence_resolver: RuntimeEvidenceResolver | None = None,
    rows_loader: RowsPayloadLoader | None = None,
    runtime_registry: RuntimeContractRegistry | None = None,
    release_resolver: DatasetReleaseResolver | None = None,
) -> dict[str, Any]:
    normalized_reports = tuple(
        normalized
        for report in completeness_reports
        if (normalized := _normalized_completeness_report(report))
    )
    normalized_results = tuple(
        _mapping_jsonable(item)
        for item in result_provenance
        if isinstance(item, Mapping)
    )
    normalized_row_payload = _normalized_row_payload(row_payload)
    try:
        rows_digest = canonical_rows_hash(
            tuple(normalized_row_payload.get("rows") or ()),
            unique_key_fields,
        ) if normalized_row_payload else ""
    except EvidenceIntegrityError:
        rows_digest = ""
    authoritative_binding_ref = ""
    authoritative_binding_digest = ""
    if (
        evidence_resolver is not None
        and rows_loader is not None
        and not runtime_registry_integrity_error(runtime_registry)
        and binding_manifest_ref
    ):
        try:
            binding_record = evidence_resolver.resolve_capability_binding(
                binding_manifest_ref
            )
            chain = validate_authoritative_query_chain(
                binding_record,
                resolver=evidence_resolver,
                rows_loader=rows_loader,
                runtime_registry=runtime_registry,
                release_resolver=release_resolver,
            )
        except Exception:
            binding_record = None
            chain = None
        if (
            binding_record is not None
            and chain is not None
            and not runtime_evidence_record_integrity_errors(binding_record)
            and binding_record.binding_digest == binding_manifest_digest
        ):
            authoritative_binding_ref = binding_manifest_ref
            authoritative_binding_digest = binding_manifest_digest
    derived_status = (
        "complete"
        if normalized_reports
        and all(
            report.get("completeness_status") == "complete"
            and report.get("analysis_readiness") == "ready"
            for report in normalized_reports
        )
        else ""
    )
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
        "resolved_windows": _resolved_window_map(resolved_windows),
        "query_contract_signatures": _mapping_jsonable(
            query_contract_signatures
        ),
        "completeness_digest": (
            _canonical_sha256(normalized_reports) if normalized_reports else ""
        ),
        "completeness_status": derived_status,
        "capability_contract_version": str(
            capability_contract_version or ""
        ),
        "source_snapshot_refs": list(_string_list(source_snapshot_refs)),
        "completeness_reports": list(normalized_reports),
        "result_provenance": list(normalized_results),
        "result_refs": list(
            _string_list(
                tuple(item.get("result_ref") for item in normalized_results)
            )
        ),
        "rows_refs": list(
            _string_list(
                tuple(item.get("rows_ref") for item in normalized_results)
            )
        ),
        "completeness_report_refs": list(
            _string_list(
                tuple(
                    item.get("completeness_report_ref")
                    for item in normalized_results
                )
            )
        ),
        "completeness_record_refs": list(_string_list(completeness_record_refs)),
        "completeness_record_digests": list(
            _string_list(completeness_record_digests)
        ),
        "row_payload_digest": rows_digest,
        "row_payload_rows_ref": str(
            row_payload_rows_ref
            or (
                normalized_results[0].get("rows_ref")
                if len(normalized_results) == 1
                else ""
            )
        ),
        "unique_key_fields": list(_string_list(unique_key_fields)),
        "binding_manifest_ref": authoritative_binding_ref,
        "binding_manifest_digest": authoritative_binding_digest,
    }
    signature_payload = {
        key: contract[key]
        for key in contract
        if key != "contract_signature"
    }
    contract["contract_signature"] = _canonical_sha256(signature_payload)
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
    resolved_windows: Any = None,
    query_contract_signatures: Any = None,
    completeness_digest: Any = None,
    completeness_status: Any = None,
    capability_contract_version: Any = None,
    source_snapshot_refs: Sequence[str] = (),
    completeness_reports: Sequence[Any] = (),
    result_provenance: Sequence[Mapping[str, Any]] = (),
    completeness_record_refs: Sequence[str] = (),
    completeness_record_digests: Sequence[str] = (),
    row_payload: Mapping[str, Any] | None = None,
    unique_key_fields: Sequence[str] = (),
    row_payload_rows_ref: str = "",
    binding_manifest_ref: str = "",
    binding_manifest_digest: str = "",
    evidence_resolver: RuntimeEvidenceResolver | None = None,
    rows_loader: RowsPayloadLoader | None = None,
    runtime_registry: RuntimeContractRegistry | None = None,
    release_resolver: DatasetReleaseResolver | None = None,
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
        rows_loader=rows_loader,
        runtime_registry=runtime_registry,
        release_resolver=release_resolver,
    )
    required = set(str(item) for item in required_dimensions if item)
    covered: set[str] = set()
    exact_matches: list[dict[str, Any]] = []
    partial_matches: list[dict[str, Any]] = []
    seen_identities = set()
    for asset in prior_assets:
        evaluation = evaluate_dimension_scan_reuse(
            asset,
            expected_contract=expected_contract,
            now=now,
            evidence_resolver=evidence_resolver,
            rows_loader=rows_loader,
            runtime_registry=runtime_registry,
            release_resolver=release_resolver,
        )
        dimensions = set(asset_dimensions(asset))
        overlap = dimensions.intersection(required)
        if not overlap:
            continue
        decision = str(evaluation["reuse_decision"].get("decision") or "")
        delta = evaluation.get("delta_query_descriptor")
        if decision != "reuse" and not isinstance(delta, Mapping):
            continue
        identity = (
            str((asset.get("reuse_contract") or {}).get("contract_signature") or ""),
            tuple(asset.get("result_refs") or ()),
        )
        if identity in seen_identities:
            continue
        seen_identities.add(identity)
        if decision != "reuse":
            partial_matches.append(
                {
                    "query_ref": str(asset.get("query_ref") or ""),
                    "result_refs": [
                        str(ref) for ref in (asset.get("result_refs") or ()) if ref
                    ],
                    "dimensions": list(asset_dimensions(asset)),
                    "reuse_decision": dict(evaluation["reuse_decision"]),
                    "delta_query_descriptor": dict(delta),
                }
            )
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
        exact_matches.append(
            {
                "query_ref": str(asset.get("query_ref") or ""),
                "result_refs": [str(ref) for ref in (asset.get("result_refs") or ()) if ref],
                "dimensions": list(asset_dimensions(asset)),
                "rows": normalized_rows,
                "row_count": int(row_payload.get("row_count") or 0),
                "created_at": str(asset.get("created_at") or ""),
                "expires_at": str(asset.get("expires_at") or ""),
                "reuse_decision": dict(evaluation["reuse_decision"]),
                "delta_query_descriptor": evaluation.get(
                    "delta_query_descriptor"
                ),
            }
        )
        covered.update(overlap)
    if required and not required.issubset(covered):
        exact_matches = []
    return tuple(
        item
        for item in (*exact_matches, *partial_matches)
        if item.get("query_ref")
        and (item.get("rows") or item.get("delta_query_descriptor"))
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
    return (
        evaluate_dimension_scan_reuse(
            asset,
            expected_contract=expected_contract,
            now=now,
        )["reuse_decision"]["decision"]
        == "reuse"
    )


def evaluate_dimension_scan_reuse(
    asset: Mapping[str, Any],
    *,
    expected_contract: Mapping[str, Any],
    now: datetime | None = None,
    evidence_resolver: RuntimeEvidenceResolver | None = None,
    rows_loader: RowsPayloadLoader | None = None,
    runtime_registry: RuntimeContractRegistry | None = None,
    release_resolver: DatasetReleaseResolver | None = None,
) -> dict[str, Any]:
    source_ref = str(asset.get("query_ref") or asset.get("asset_id") or "")
    result_refs = tuple(str(ref) for ref in asset.get("result_refs") or () if ref)
    result_ref = result_refs[0] if result_refs else source_ref

    def context_only(reason: str, *, delta: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return {
            "reuse_decision": _reuse_decision(
                decision="context_only",
                source_ref=source_ref,
                result_ref=result_ref,
                reason=reason,
                can_support_claim=False,
                requires_rerun=True,
            ),
            "delta_query_descriptor": dict(delta) if delta else None,
        }

    if not _is_reusable_dimension_scan_asset(asset):
        return context_only("asset_not_usable")
    if not _dimension_scan_asset_fresh(asset, now=now):
        return context_only("asset_outside_ttl")
    contract = asset.get("reuse_contract")
    if not isinstance(contract, Mapping):
        return context_only("reuse_contract_missing")
    if str(contract.get("contract_signature") or "") != _reuse_contract_signature(
        contract
    ):
        return context_only("reuse_contract_signature_invalid")
    if str(expected_contract.get("contract_signature") or "") != _reuse_contract_signature(
        expected_contract
    ):
        return context_only("expected_reuse_contract_signature_invalid")
    if str(contract.get("completeness_status") or "") != "complete":
        return context_only("completeness_not_complete")
    missing_field = _missing_exact_reuse_signature_field(contract)
    if missing_field:
        return context_only(f"reuse_signature_missing:{missing_field}")
    expected_missing_field = _missing_exact_reuse_signature_field(expected_contract)
    if expected_missing_field:
        return context_only(f"expected_reuse_signature_missing:{expected_missing_field}")
    try:
        authority_reason = _asset_authority_validation(
            asset,
            contract,
            evidence_resolver,
            rows_loader,
            runtime_registry,
            release_resolver,
        )
    except Exception:
        authority_reason = "runtime_evidence_resolution_failed"
    if authority_reason:
        return context_only(authority_reason)
    row_payload = asset.get("row_payload")
    if not isinstance(row_payload, Mapping) or not _dimension_scan_rows_complete(row_payload):
        return context_only("row_payload_incomplete")
    try:
        content_reason, actual_row_window_ids = _asset_content_validation(
            asset,
            contract,
            row_payload,
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        content_reason, actual_row_window_ids = "asset_content_invalid", ()
    if content_reason:
        return context_only(content_reason)

    required_exact_fields = (
        "permission_scope",
        "snapshot_version",
        "scope",
        "target_metric",
        "time_window",
        "baselines",
        "dimensions",
        "required_fields",
        "contract_versions",
        "schema_fingerprint",
        "capability_contract_version",
        "source_snapshot_refs",
    )
    for key in required_exact_fields:
        if _signature_value(contract.get(key)) != _signature_value(
            expected_contract.get(key)
        ):
            return context_only(f"reuse_signature_mismatch:{key}")
    query_intent = str(contract.get("query_intent") or "")
    expected_intent = str(expected_contract.get("query_intent") or "")
    if query_intent not in {"dimension_scan", "dimension_scan_reuse"}:
        return context_only("reuse_signature_mismatch:query_intent")
    if expected_intent not in {"dimension_scan", "dimension_scan_reuse"}:
        return context_only("expected_query_intent_invalid")

    actual_windows = _resolved_window_map(contract.get("resolved_windows"))
    expected_windows = _resolved_window_map(
        expected_contract.get("resolved_windows")
    )
    reusable_window_ids = tuple(
        window_id
        for window_id in expected_windows
        if window_id in actual_row_window_ids
        and actual_windows.get(window_id) == expected_windows[window_id]
    )
    missing_window_ids = tuple(
        window_id
        for window_id in expected_windows
        if window_id not in reusable_window_ids
    )
    extra_window_ids = tuple(
        window_id for window_id in actual_windows if window_id not in expected_windows
    )
    if missing_window_ids and reusable_window_ids:
        if not _overlap_query_signatures_match(
            contract.get("query_contract_signatures"),
            expected_contract.get("query_contract_signatures"),
            reusable_window_ids,
        ):
            return context_only(
                "reuse_signature_mismatch:query_contract_signatures"
            )
        return context_only(
            "partial_window_coverage",
            delta={
                "query_mode": "delta_query",
                "missing_window_ids": missing_window_ids,
                "reusable_window_ids": reusable_window_ids,
                "source_snapshot_refs": tuple(
                    _string_list(expected_contract.get("source_snapshot_refs"))
                ),
            },
        )
    if missing_window_ids or extra_window_ids:
        return context_only("reuse_signature_mismatch:resolved_windows")
    for key in ("query_contract_signatures", "completeness_digest"):
        if _signature_value(contract.get(key)) != _signature_value(
            expected_contract.get(key)
        ):
            return context_only(f"reuse_signature_mismatch:{key}")
    if str(contract.get("completeness_status") or "") != str(
        expected_contract.get("completeness_status") or ""
    ):
        return context_only("reuse_signature_mismatch:completeness_status")
    if str(contract.get("contract_signature") or "") != str(
        expected_contract.get("contract_signature") or ""
    ):
        return context_only("reuse_signature_mismatch:contract_signature")
    return {
        "reuse_decision": _reuse_decision(
            decision="reuse",
            source_ref=source_ref,
            result_ref=result_ref,
            reason="exact_asset_signature_match",
            can_support_claim=True,
            requires_rerun=False,
        ),
        "delta_query_descriptor": None,
    }


def _dimension_scan_asset_fresh(
    asset: Mapping[str, Any],
    *,
    now: datetime | None,
) -> bool:
    created_at = _parse_datetime(asset.get("created_at"))
    expires_at = _parse_datetime(asset.get("expires_at"))
    if created_at is None or expires_at is None:
        return False
    if now is None:
        current = datetime.now(timezone.utc)
    elif not isinstance(now, datetime) or now.tzinfo is None:
        return False
    else:
        current = now.astimezone(timezone.utc)
    return created_at <= current <= expires_at


def _dimension_scan_rows_complete(row_payload: Mapping[str, Any]) -> bool:
    rows = row_payload.get("rows")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return False
    try:
        row_count = int(row_payload.get("row_count") or 0)
    except (TypeError, ValueError):
        return False
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
        return None
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


def _missing_exact_reuse_signature_field(
    contract: Mapping[str, Any],
) -> str:
    required = (
        "resolved_windows",
        "query_contract_signatures",
        "completeness_digest",
        "completeness_reports",
        "capability_contract_version",
        "source_snapshot_refs",
        "result_provenance",
        "result_refs",
        "rows_refs",
        "completeness_report_refs",
        "completeness_record_refs",
        "completeness_record_digests",
        "row_payload_digest",
        "row_payload_rows_ref",
        "unique_key_fields",
        "binding_manifest_ref",
        "binding_manifest_digest",
    )
    for field in required:
        if not contract.get(field):
            return field
    if not contract.get("contract_signature"):
        return "contract_signature"
    return ""


def _reuse_decision(
    *,
    decision: str,
    source_ref: str,
    result_ref: str,
    reason: str,
    can_support_claim: bool,
    requires_rerun: bool,
) -> dict[str, Any]:
    from bi_agent.conversation.models import ReuseDecision

    return ReuseDecision(
        decision,
        result_ref,
        reason,
        can_support_claim=can_support_claim,
        requires_rerun=requires_rerun,
        source_ref=source_ref,
    ).to_dict()


def _signature_value(value: Any) -> Any:
    return to_jsonable(value)


def _normalized_completeness_report(value: Any) -> dict[str, Any]:
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    if not isinstance(value, Mapping):
        return {}
    required = (
        "report_ref",
        "query_contract_ref",
        "result_ref",
        "completeness_status",
        "analysis_readiness",
        "assertion_results",
        "failure_reasons",
        "coverage_summary",
    )
    if any(field not in value for field in required):
        return {}
    assertions = value.get("assertion_results")
    coverage = value.get("coverage_summary")
    if (
        not isinstance(assertions, Sequence)
        or isinstance(assertions, (str, bytes))
        or any(not isinstance(item, Mapping) for item in assertions)
        or not isinstance(coverage, Mapping)
    ):
        return {}
    return {
        "report_ref": str(value.get("report_ref") or ""),
        "query_contract_ref": str(value.get("query_contract_ref") or ""),
        "result_ref": str(value.get("result_ref") or ""),
        "completeness_status": str(value.get("completeness_status") or ""),
        "analysis_readiness": str(value.get("analysis_readiness") or ""),
        "assertion_results": to_jsonable(tuple(assertions)),
        "failure_reasons": list(_string_list(tuple(value.get("failure_reasons") or ()))),
        "coverage_summary": to_jsonable(dict(coverage)),
    }


def _normalized_row_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    rows = value.get("rows")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return {}
    normalized_rows = [
        to_jsonable(dict(row))
        for row in rows
        if isinstance(row, Mapping)
    ]
    try:
        row_count = int(value.get("row_count") or 0)
    except (TypeError, ValueError):
        return {}
    return {
        "rows": normalized_rows,
        "row_count": row_count,
        "truncated": bool(value.get("truncated")),
    }


def _canonical_sha256(value: Any) -> str:
    try:
        encoded = json.dumps(
            to_jsonable(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return ""
    return hashlib.sha256(encoded).hexdigest()


def _reuse_contract_signature(contract: Mapping[str, Any]) -> str:
    payload = {
        str(key): to_jsonable(value)
        for key, value in contract.items()
        if key != "contract_signature"
    }
    return _canonical_sha256(payload)


def _asset_authority_validation(
    asset: Mapping[str, Any],
    contract: Mapping[str, Any],
    resolver: RuntimeEvidenceResolver | None,
    rows_loader: RowsPayloadLoader | None,
    runtime_registry: RuntimeContractRegistry | None,
    release_resolver: DatasetReleaseResolver | None,
) -> str:
    if resolver is None:
        return "runtime_evidence_resolver_missing"
    if rows_loader is None:
        return "rows_payload_loader_missing"
    registry_error = runtime_registry_integrity_error(runtime_registry)
    if registry_error:
        return registry_error
    binding_ref = str(contract.get("binding_manifest_ref") or "")
    binding = resolver.resolve_capability_binding(binding_ref)
    if binding is None:
        return "capability_binding_record_missing"
    try:
        chain = validate_authoritative_query_chain(
            binding,
            resolver=resolver,
            rows_loader=rows_loader,
            runtime_registry=runtime_registry,
            release_resolver=release_resolver,
        )
    except AuthoritativeQueryChainError as exc:
        return f"authoritative_query_chain_invalid:{exc}"
    if binding.binding_digest != str(contract.get("binding_manifest_digest") or ""):
        return "capability_binding_digest_mismatch"
    if binding.status != "ready" or any(
        status != "complete" for status in binding.input_completeness_statuses
    ):
        return "capability_binding_not_ready"
    result_refs = tuple(_string_list(asset.get("result_refs")))
    if not result_refs or len(set(result_refs)) != len(result_refs):
        return "asset_result_refs_invalid"
    authorized_results = set((*binding.result_refs, *binding.validation_result_refs))
    if any(ref not in authorized_results for ref in result_refs):
        return "asset_result_ref_not_authorized"
    rows_refs = tuple(_string_list(asset.get("rows_refs")))
    report_refs = tuple(_string_list(asset.get("completeness_report_refs")))
    completeness_record_refs = tuple(
        _string_list(asset.get("completeness_record_refs"))
    )
    completeness_record_digests = tuple(
        _string_list(asset.get("completeness_record_digests"))
    )
    authorized_completeness = {
        result_ref: (report_ref, record_ref, digest)
        for result_ref, report_ref, record_ref, digest in (
            *zip(
                binding.result_refs,
                binding.completeness_report_refs,
                binding.completeness_record_refs,
                binding.completeness_record_digests,
            ),
            *zip(
                binding.validation_result_refs,
                binding.validation_completeness_report_refs,
                binding.validation_completeness_record_refs,
                binding.validation_completeness_record_digests,
            ),
        )
    }
    if completeness_record_refs != tuple(
        authorized_completeness[result_ref][1]
        for result_ref in result_refs
        if result_ref in authorized_completeness
    ) or completeness_record_digests != tuple(
        authorized_completeness[result_ref][2]
        for result_ref in result_refs
        if result_ref in authorized_completeness
    ):
        return "asset_completeness_record_mismatch"
    snapshot_refs = set(_string_list(contract.get("source_snapshot_refs")))
    query_signatures = _mapping_jsonable(contract.get("query_contract_signatures"))
    for result_ref in result_refs:
        query = chain.query_records.get(result_ref)
        if query is None:
            return "query_execution_record_not_authorized"
        if query.rows_ref not in rows_refs:
            return "asset_rows_ref_mismatch"
        if query.completeness_report_ref not in report_refs:
            return "asset_completeness_ref_mismatch"
        if query_signatures.get(query.query_contract_ref) != query.contract_signature:
            return "query_contract_signature_mismatch"
        immutable = authorized_completeness.get(result_ref)
        if immutable is None:
            return "asset_completeness_record_not_authorized"
        report_ref, record_ref, record_digest = immutable
        if (
            report_ref != query.completeness_report_ref
            or record_ref not in completeness_record_refs
            or record_digest not in completeness_record_digests
        ):
            return "asset_completeness_record_mismatch"
        if not set(query.source_snapshot_refs).issubset(snapshot_refs):
            return "snapshot_record_missing"
    primary_rows_ref = str(contract.get("row_payload_rows_ref") or "")
    authoritative_rows = chain.rows_by_ref.get(primary_rows_ref)
    if authoritative_rows is None:
        return "row_payload_rows_record_missing"
    try:
        authoritative_hash = canonical_rows_hash(
            authoritative_rows,
            tuple(_string_list(contract.get("unique_key_fields"))),
        )
    except EvidenceIntegrityError:
        return "row_payload_authority_hash_invalid"
    if authoritative_hash != str(contract.get("row_payload_digest") or ""):
        return "row_payload_authority_hash_mismatch"
    return ""


def _asset_content_validation(
    asset: Mapping[str, Any],
    contract: Mapping[str, Any],
    row_payload: Mapping[str, Any],
) -> tuple[str, tuple[str, ...]]:
    normalized_rows = _normalized_row_payload(row_payload)
    if not normalized_rows:
        return "row_payload_unparseable", ()
    try:
        actual_rows_hash = canonical_rows_hash(
            tuple(normalized_rows["rows"]),
            tuple(_string_list(contract.get("unique_key_fields"))),
        )
    except EvidenceIntegrityError:
        return "row_payload_unparseable", ()
    if actual_rows_hash != str(contract.get("row_payload_digest") or ""):
        return "row_payload_digest_mismatch", ()
    rows = tuple(normalized_rows["rows"])
    if (
        normalized_rows["truncated"]
        or normalized_rows["row_count"] != len(rows)
        or not rows
    ):
        return "row_payload_incomplete", ()
    required_fields = tuple(_string_list(contract.get("required_fields")))
    if not required_fields or any(
        any(field not in row for field in required_fields) for row in rows
    ):
        return "row_payload_required_fields_mismatch", ()
    unique_fields = tuple(_string_list(contract.get("unique_key_fields")))
    if not unique_fields:
        return "row_payload_unique_key_missing", ()
    actual_windows = tuple(
        dict.fromkeys(str(row.get("window_id") or "") for row in rows)
    )
    if not actual_windows or any(not window_id for window_id in actual_windows):
        return "row_payload_window_id_missing", ()
    resolved_windows = _resolved_window_map(contract.get("resolved_windows"))
    if any(window_id not in resolved_windows for window_id in actual_windows):
        return "row_payload_window_id_uncontracted", ()

    reports = tuple(
        _normalized_completeness_report(report)
        for report in contract.get("completeness_reports") or ()
    )
    if not reports or any(not report for report in reports):
        return "completeness_reports_unparseable", ()
    if _canonical_sha256(reports) != str(contract.get("completeness_digest") or ""):
        return "completeness_digest_mismatch", ()
    if any(
        report.get("completeness_status") != "complete"
        or report.get("analysis_readiness") != "ready"
        or report.get("failure_reasons")
        or not report.get("assertion_results")
        or any(
            assertion.get("passed") is not True
            for assertion in report.get("assertion_results") or ()
        )
        for report in reports
    ):
        return "completeness_report_not_ready", ()

    results = tuple(
        item
        for item in contract.get("result_provenance") or ()
        if isinstance(item, Mapping)
    )
    if not results:
        return "result_provenance_unparseable", ()
    expected_result_refs = tuple(str(item.get("result_ref") or "") for item in results)
    expected_rows_refs = tuple(str(item.get("rows_ref") or "") for item in results)
    expected_report_refs = tuple(
        str(item.get("completeness_report_ref") or "") for item in results
    )
    if (
        tuple(_string_list(asset.get("result_refs"))) != expected_result_refs
        or tuple(_string_list(asset.get("rows_refs"))) != expected_rows_refs
        or tuple(_string_list(asset.get("completeness_report_refs")))
        != expected_report_refs
    ):
        return "asset_result_reference_mismatch", ()
    if (
        tuple(_string_list(contract.get("result_refs"))) != expected_result_refs
        or tuple(_string_list(contract.get("rows_refs"))) != expected_rows_refs
        or tuple(_string_list(contract.get("completeness_report_refs")))
        != expected_report_refs
    ):
        return "reuse_contract_result_reference_mismatch", ()
    if len(set(expected_result_refs)) != len(expected_result_refs):
        return "result_provenance_duplicate_result_ref", ()
    if len(set(expected_rows_refs)) != len(expected_rows_refs):
        return "result_provenance_duplicate_rows_ref", ()
    if len(set(expected_report_refs)) != len(expected_report_refs):
        return "result_provenance_duplicate_report_ref", ()
    if str(contract.get("row_payload_rows_ref") or "") not in expected_rows_refs:
        return "row_payload_rows_ref_mismatch", ()

    report_by_ref = {str(report["report_ref"]): report for report in reports}
    query_signatures = _mapping_jsonable(
        contract.get("query_contract_signatures")
    )
    snapshot_union = []
    for result in results:
        query_ref = str(result.get("query_contract_ref") or "")
        result_ref = str(result.get("result_ref") or "")
        rows_ref = str(result.get("rows_ref") or "")
        report_ref = str(result.get("completeness_report_ref") or "")
        snapshots = tuple(_string_list(result.get("source_snapshot_refs")))
        report = report_by_ref.get(report_ref)
        if (
            not query_ref
            or query_signatures.get(query_ref)
            != str(result.get("query_contract_signature") or "")
            or report is None
            or report.get("query_contract_ref") != query_ref
            or report.get("result_ref") != result_ref
        ):
            return "result_report_query_linkage_mismatch", ()
        coverage = report.get("coverage_summary") or {}
        coverage_snapshots = tuple(_string_list(coverage.get("snapshot_refs")))
        if (
            str(coverage.get("rows_ref") or "") != rows_ref
            or coverage_snapshots != snapshots
            or int(coverage.get("row_count") or 0)
            != int(result.get("row_count") or 0)
        ):
            return "result_report_coverage_mismatch", ()
        snapshot_union.extend(snapshots)
    if tuple(dict.fromkeys(snapshot_union)) != tuple(
        _string_list(contract.get("source_snapshot_refs"))
    ):
        return "result_snapshot_provenance_mismatch", ()
    primary_rows_ref = str(contract.get("row_payload_rows_ref") or "")
    primary = next(
        item for item in results if str(item.get("rows_ref") or "") == primary_rows_ref
    )
    if int(primary.get("row_count") or 0) != len(rows):
        return "row_payload_result_count_mismatch", ()
    primary_report = report_by_ref[str(primary.get("completeness_report_ref") or "")]
    observed = tuple(
        _string_list(
            (primary_report.get("coverage_summary") or {}).get("observed_windows")
        )
    )
    if observed != actual_windows:
        return "row_payload_report_window_mismatch", ()
    return "", actual_windows


def _overlap_query_signatures_match(
    actual: Any,
    expected: Any,
    reusable_window_ids: Sequence[str],
) -> bool:
    if not isinstance(actual, Mapping) or not isinstance(expected, Mapping):
        return False
    actual_by_window = all(
        isinstance(value, Mapping) for value in actual.values()
    )
    expected_by_window = all(
        isinstance(value, Mapping) for value in expected.values()
    )
    if actual_by_window and expected_by_window:
        return all(
            window_id in actual
            and window_id in expected
            and _signature_value(actual[window_id])
            == _signature_value(expected[window_id])
            for window_id in reusable_window_ids
        )
    return _signature_value(actual) == _signature_value(expected)


def _resolved_window_map(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        normalized = {}
        for key, item in value.items():
            if isinstance(item, Mapping):
                normalized[str(key)] = {
                    str(field): to_jsonable(field_value)
                    for field, field_value in item.items()
                    if field not in {"label"} and field_value not in (None, "")
                }
            else:
                normalized[str(key)] = {"value": to_jsonable(item)}
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        normalized = {}
        for item in value:
            if not isinstance(item, Mapping):
                continue
            window_id = str(item.get("window_id") or "")
            if not window_id:
                continue
            normalized[window_id] = {
                str(field): to_jsonable(field_value)
                for field, field_value in item.items()
                if field not in {"window_id", "label"}
                and field_value not in (None, "")
            }
        return normalized
    return {}


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
_MATERIAL_ASSUMPTION_SCALAR_FIELDS = (
    "option",
    "value",
    "action_kind",
    "obligation_id",
    "obligation_decision",
    "degradation_decision",
    "dataset_id",
    "claim_ceiling",
    "target_semantic",
    "scope",
)
_MATERIAL_ASSUMPTION_LIST_FIELDS = (
    "affected_capabilities",
    "affected_datasets",
    "affected_claim_types",
)


def canonical_material_assumption(choice: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only stable fields that can change execution or claim boundaries."""

    if not isinstance(choice, Mapping) or not choice:
        return {}
    canonical: dict[str, Any] = {}
    for field in _MATERIAL_ASSUMPTION_SCALAR_FIELDS:
        value = choice.get(field)
        if value not in (None, "", (), [], {}):
            canonical[field] = to_jsonable(value)
    for field in _MATERIAL_ASSUMPTION_LIST_FIELDS:
        raw = choice.get(field) or ()
        if isinstance(raw, (str, bytes)):
            raw = (raw,)
        values = sorted({str(item) for item in raw if str(item)})
        if values:
            canonical[field] = values
    return canonical


def material_assumption_digest(choice: Mapping[str, Any]) -> str:
    canonical = canonical_material_assumption(choice)
    return canonical_digest(canonical) if canonical else ""
