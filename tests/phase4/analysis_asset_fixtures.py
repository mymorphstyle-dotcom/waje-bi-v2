from dataclasses import replace
from datetime import date

from bi_agent.runtime.analysis_assets import build_dimension_scan_reuse_contract
from bi_agent.runtime.analysis_contracts import (
    CapabilityExecutionPlan,
    CapabilityInputSlot,
    CompletenessReport,
    DimensionBinding,
    MetricBinding,
    QueryContract,
    QueryResultEnvelope,
    ReconciliationBinding,
    ResolvedWindow,
    ResultShape,
    query_contract_signature,
)
from bi_agent.runtime.dataset_catalog import (
    DatasetSnapshot,
    build_dataset_release_authority_record,
    dataset_snapshot_release_ref,
)
from bi_agent.runtime.evidence_authority import (
    RuntimeEvidenceAuthority,
    _record_capability_binding,
    _record_completeness,
    _record_query_execution,
    canonical_result_rows_hash,
)
from bi_agent.runtime.query_audit import query_audit_refs
from bi_agent.runtime.query_completeness import (
    ASSERTIONS,
    validate_query_result,
    validate_query_set,
)
from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry


def verified_dimension_scan_asset(
    *,
    rows,
    required_fields,
    resolved_windows,
    query_ref="query:channel-scan",
    snapshot_ref="snapshot:paid:1",
    analysis_contract_ref="analysis:asset-fixture:1",
    contract_versions=None,
    schema_fingerprint="schema-v1",
    completeness_status="complete",
    analysis_readiness="ready",
    created_at="2026-07-08T08:00:00+00:00",
    expires_at="2026-07-10T08:00:00+00:00",
    time_window="2026-07-08",
):
    windows = tuple(
        _resolved_window(window_id, payload)
        for window_id, payload in resolved_windows.items()
    )
    window_by_id = {window.window_id: window for window in windows}
    rows = tuple(
        {
            **dict(row),
            "window_role": str(
                row.get("window_role")
                or window_by_id[str(row.get("window_id") or "")].role
            ),
            "observation_key": str(
                row.get("observation_key")
                or row.get("period")
                or window_by_id[str(row.get("window_id") or "")].start_inclusive
            ),
            "paid_amount": float(row.get("paid_amount", row.get("amount", 0.0))),
        }
        for row in rows
    )
    unique_key = ("window_id", "observation_key", "channel")
    registry = RuntimeContractRegistry.from_path(
        "contracts/runtime/clickhouse-analysis-bindings.yaml"
    )
    reviewed_metric = registry.metric("paid_amount")
    reviewed_dimension = registry.dimension("channel")
    metric_binding = MetricBinding(
        metric_id="paid_amount",
        contract_ref=str(reviewed_metric["contract_ref"]),
        dataset_id=str(reviewed_metric["dataset_id"]),
        expression=str(reviewed_metric["expression"]),
        aggregation=str(reviewed_metric["aggregation"]),
        required_fields=tuple(reviewed_metric["required_fields"]),
        grain=tuple(reviewed_metric["grain"]),
        claim_types=tuple(reviewed_metric["claim_types"]),
        reconciliation_tolerance=float(
            reviewed_metric["reconciliation_tolerance"]
        ),
        reconciliation_strategy=str(reviewed_metric["reconciliation_strategy"]),
        value_semantics=str(reviewed_metric["value_semantics"]),
        display_format=str(reviewed_metric["display_format"]),
    )
    dimension_binding = DimensionBinding(
        dimension_id="channel",
        contract_ref=str(reviewed_dimension["contract_ref"]),
        dataset_id=str(reviewed_dimension["dataset_id"]),
        source_field=str(reviewed_dimension["source_field"]),
        allowed_grains=tuple(reviewed_dimension["allowed_grains"]),
        permission_scope="analyst",
    )
    total_query_ref = f"{query_ref}:total"
    total_contract = QueryContract(
        query_contract_id=total_query_ref,
        analysis_contract_ref=analysis_contract_ref,
        query_intent="daily_metric_baselines",
        dataset_snapshot_refs=(snapshot_ref,),
        metric_bindings=(metric_binding,),
        dimension_bindings=(),
        window_refs=tuple(resolved_windows),
        resolved_windows=windows,
        filters=(),
        result_shape=ResultShape(
            required_fields=(
                "window_id",
                "window_role",
                "observation_key",
                "paid_amount",
            ),
            unique_key=("window_id", "observation_key"),
            grain=("window_id", "observation_key"),
            required_window_ids=tuple(resolved_windows),
        ),
        completeness_assertions=ASSERTIONS,
        permission_scope="analyst",
        workload_class="interactive_aggregate",
        contract_signature="",
        query_role_ref=f"query-role:{total_query_ref}",
    )
    total_contract = replace(
        total_contract,
        contract_signature=query_contract_signature(total_contract),
    )
    contract = QueryContract(
        query_contract_id=query_ref,
        analysis_contract_ref=analysis_contract_ref,
        query_intent="dimension_contribution_scan",
        dataset_snapshot_refs=(snapshot_ref,),
        metric_bindings=(metric_binding,),
        dimension_bindings=(dimension_binding,),
        window_refs=tuple(resolved_windows),
        resolved_windows=windows,
        filters=(),
        result_shape=ResultShape(
            required_fields=(
                "window_id",
                "window_role",
                "observation_key",
                "paid_amount",
                "channel",
            ),
            unique_key=unique_key,
            grain=unique_key,
            required_window_ids=tuple(resolved_windows),
        ),
        completeness_assertions=ASSERTIONS,
        permission_scope="analyst",
        workload_class="interactive_aggregate",
        contract_signature="",
        query_role_ref=f"query-role:{query_ref}",
        reconciliation_binding=ReconciliationBinding(
            reference_query_role_ref=total_contract.query_role_ref,
            reference_contract_signature=total_contract.contract_signature,
        ),
    )
    contract = replace(
        contract,
        contract_signature=query_contract_signature(contract),
    )
    rows = tuple(
        {
            field: row[field]
            for field in contract.result_shape.required_fields
        }
        for row in rows
    )
    snapshot = DatasetSnapshot(
        snapshot_ref=snapshot_ref,
        dataset_id="paid_order_success",
        physical_table="analytics.paid_success",
        watermark=max(window.source_watermark_requirement for window in windows),
        schema_fingerprint=schema_fingerprint or "fixture-schema-v1",
        schema_fields=("business_date_lagos", "paid_amount_ngn", "channel"),
        contract_ref="source:paid@1",
        permission_scopes=("analyst",),
        loaded_at="2026-07-09T00:00:00+00:00",
        status="active",
        logical_snapshot_id="paid-order-success-fixture",
        load_revision="paid-order-success-load:sha256:fixture",
        rows_content_hash="a" * 64,
        snapshot_id="paid-order-success-fixture",
    )
    release_ref = dataset_snapshot_release_ref(
        snapshot.logical_snapshot_id,
        snapshot.load_revision,
        (snapshot.snapshot_ref,),
    )
    snapshot = replace(snapshot, release_ref=release_ref)
    release_record = build_dataset_release_authority_record(
        ({**snapshot.to_dict(), "requires_release": True},)
    )
    snapshot = replace(
        snapshot,
        authority_record_ref=release_record.authority_record_ref,
    )

    class ReleaseResolver:
        def resolve_dataset_release(self, requested_release_ref):
            if requested_release_ref != release_record.release_ref:
                raise KeyError(requested_release_ref)
            return release_record

    release_resolver = ReleaseResolver()
    attempt_ref = "attempt:asset-fixture"
    query_hash = "hash:asset-fixture"
    refs = query_audit_refs(
        query_hash,
        contract.contract_signature,
        contract.dataset_snapshot_refs,
        query_contract_ref=query_ref,
        execution_attempt_ref=attempt_ref,
        rows_content_hash=canonical_result_rows_hash(
            rows,
            contract.result_shape.unique_key,
        ),
    )
    result = QueryResultEnvelope(
        query_contract_ref=query_ref,
        query_id="clickhouse:asset-fixture",
        query_hash=query_hash,
        result_ref=refs.result_ref,
        execution_status="succeeded",
        rows_ref=refs.rows_ref,
        row_count=len(rows),
        completeness_report_ref=refs.completeness_report_ref,
        rows=rows,
        observed_schema={
            field: "String" for field in contract.result_shape.required_fields
        },
        observed_windows=tuple(
            dict.fromkeys(str(row.get("window_id") or "") for row in rows)
        ),
        observed_grain=unique_key,
        source_snapshot_refs=(snapshot_ref,),
        execution_attempt_ref=attempt_ref,
    )
    total_rows_by_key = {}
    for row in rows:
        key = (
            row["window_id"],
            row["window_role"],
            row["observation_key"],
        )
        total_rows_by_key[key] = total_rows_by_key.get(key, 0.0) + float(
            row["paid_amount"]
        )
    total_rows = tuple(
        {
            "window_id": key[0],
            "window_role": key[1],
            "observation_key": key[2],
            "paid_amount": value,
        }
        for key, value in sorted(total_rows_by_key.items())
    )
    total_attempt_ref = "attempt:asset-fixture:total"
    total_query_hash = "hash:asset-fixture:total"
    total_refs = query_audit_refs(
        total_query_hash,
        total_contract.contract_signature,
        total_contract.dataset_snapshot_refs,
        query_contract_ref=total_query_ref,
        execution_attempt_ref=total_attempt_ref,
        rows_content_hash=canonical_result_rows_hash(
            total_rows,
            total_contract.result_shape.unique_key,
        ),
    )
    total_result = QueryResultEnvelope(
        query_contract_ref=total_query_ref,
        query_id="clickhouse:asset-fixture:total",
        query_hash=total_query_hash,
        result_ref=total_refs.result_ref,
        execution_status="succeeded",
        rows_ref=total_refs.rows_ref,
        row_count=len(total_rows),
        completeness_report_ref=total_refs.completeness_report_ref,
        rows=total_rows,
        observed_schema={
            field: "String"
            for field in total_contract.result_shape.required_fields
        },
        observed_windows=tuple(
            dict.fromkeys(str(row["window_id"]) for row in total_rows)
        ),
        observed_grain=total_contract.result_shape.grain,
        source_snapshot_refs=(snapshot_ref,),
        execution_attempt_ref=total_attempt_ref,
    )
    report, total_report = validate_query_set(
        (contract, total_contract),
        (result, total_result),
        (
            validate_query_result(
                contract,
                result,
                snapshot,
                release_resolver=release_resolver,
            ),
            validate_query_result(
                total_contract,
                total_result,
                snapshot,
                release_resolver=release_resolver,
            ),
        ),
    )
    if completeness_status != "complete" or analysis_readiness != "ready":
        report = replace(
            report,
            completeness_status=completeness_status,
            analysis_readiness=analysis_readiness,
            failure_reasons=("missing_window",),
        )
    capability = registry.capability_inputs("segment_contribution")
    authority = RuntimeEvidenceAuthority()
    query_record = _record_query_execution(
        authority,
        contract,
        result,
        {snapshot_ref: snapshot},
    )
    completeness_record = _record_completeness(authority, report)
    total_query_record = _record_query_execution(
        authority,
        total_contract,
        total_result,
        {snapshot_ref: snapshot},
    )
    total_completeness_record = _record_completeness(authority, total_report)
    plan = CapabilityExecutionPlan(
        capability_id="segment_contribution",
        capability_contract_ref=registry.capability_contract_ref(
            "segment_contribution"
        ),
        required_input_slots=(
            CapabilityInputSlot(
                slot_id="dimension_contribution_scan",
                query_contract_refs=(query_ref,),
                validation_query_contract_refs=(total_query_ref,),
                required=True,
                accepted_completeness=("complete",),
                required_fields=contract.result_shape.required_fields,
                required_window_ids=contract.window_refs,
            ),
        ),
        optional_input_slots=(),
        merge_strategy="by_query_family",
        minimum_readiness=capability["minimum_readiness"],
        degradation_policy=capability["degradation_policy"],
        supported_evidence_types=tuple(capability["supported_evidence_types"]),
        maximum_claim_strength=capability["maximum_claim_strength"],
        analysis_contract_ref=contract.analysis_contract_ref,
        supported_claim_types=tuple(capability["supported_claim_types"]),
        capability_contract_version=registry.contract_version,
        capability_contract_signature=registry.capability_contract_signature(
            "segment_contribution"
        ),
        claim_strength_taxonomy_version=registry.claim_strength_taxonomy_version,
        maximum_claim_strength_rank=registry.maximum_claim_strength_rank(
            capability["maximum_claim_strength"]
        ),
    )
    binding_payload = {
        "status": "ready" if completeness_status == "complete" else "degraded",
        "query_contract_refs": (query_ref,),
        "result_refs": (result.result_ref,),
        "query_execution_record_refs": (query_record.record_ref,),
        "query_execution_record_digests": (query_record.record_digest,),
        "rows_refs": (result.rows_ref,),
        "rows_metadata_record_refs": (
            authority.resolve_rows(result.rows_ref).record_ref,
        ),
        "rows_metadata_record_digests": (
            authority.resolve_rows(result.rows_ref).record_digest,
        ),
        "rows_content_hashes": (query_record.rows_content_hash,),
        "completeness_report_refs": (report.report_ref,),
        "completeness_record_refs": (completeness_record.record_ref,),
        "completeness_record_digests": (completeness_record.report_digest,),
        "source_snapshot_refs": (snapshot_ref,),
        "validation_query_contract_refs": (total_query_ref,),
        "validation_result_refs": (total_result.result_ref,),
        "validation_query_execution_record_refs": (total_query_record.record_ref,),
        "validation_query_execution_record_digests": (total_query_record.record_digest,),
        "validation_rows_refs": (total_result.rows_ref,),
        "validation_rows_metadata_record_refs": (
            authority.resolve_rows(total_result.rows_ref).record_ref,
        ),
        "validation_rows_metadata_record_digests": (
            authority.resolve_rows(total_result.rows_ref).record_digest,
        ),
        "validation_rows_content_hashes": (total_query_record.rows_content_hash,),
        "validation_completeness_report_refs": (total_report.report_ref,),
        "validation_completeness_record_refs": (
            total_completeness_record.record_ref,
        ),
        "validation_completeness_record_digests": (
            total_completeness_record.report_digest,
        ),
        "validation_source_snapshot_refs": (snapshot_ref,),
        "supported_evidence_types": plan.supported_evidence_types,
        "supported_claim_types": plan.supported_claim_types,
        "maximum_claim_strength": plan.maximum_claim_strength,
        "maximum_claim_strength_rank": plan.maximum_claim_strength_rank,
        "claim_strength_taxonomy_version": plan.claim_strength_taxonomy_version,
        "input_completeness_statuses": (
            report.completeness_status,
            total_report.completeness_status,
        ),
    }
    binding = _record_capability_binding(authority, plan, binding_payload)
    row_payload = {"rows": rows, "row_count": len(rows), "truncated": False}
    provenance = {
        "query_contract_ref": query_ref,
        "query_contract_signature": contract.contract_signature,
        "result_ref": result.result_ref,
        "rows_ref": result.rows_ref,
        "completeness_report_ref": report.report_ref,
        "source_snapshot_refs": (snapshot_ref,),
        "row_count": len(rows),
    }
    content = {
        "resolved_windows": resolved_windows,
        "query_contract_signatures": {query_ref: contract.contract_signature},
        "capability_contract_version": "1",
        "source_snapshot_refs": (snapshot_ref,),
        "completeness_reports": (report.to_dict(),),
        "result_provenance": (provenance,),
        "completeness_record_refs": (completeness_record.record_ref,),
        "completeness_record_digests": (completeness_record.report_digest,),
        "row_payload": row_payload,
        "unique_key_fields": unique_key,
        "row_payload_rows_ref": result.rows_ref,
        "binding_manifest_ref": binding.record_ref,
        "binding_manifest_digest": binding.binding_digest,
        "evidence_resolver": authority,
        "rows_loader": authority.rows_loader,
        "runtime_registry": registry,
        "release_resolver": release_resolver,
    }
    reuse_contract = build_dimension_scan_reuse_contract(
        target_metric="paid_amount",
        scope="full_sample",
        time_window=time_window,
        windows={
            window.role: window.start_inclusive
            for window in windows
            if window.role in {"target", "baseline"}
        },
        baselines=("previous_day",),
        permission_scope="analyst",
        snapshot_version="2026H1",
        dimensions=("channel",),
        required_fields=contract.result_shape.required_fields,
        contract_versions=contract_versions or {"runtime": "contract-v1"},
        schema_fingerprint=schema_fingerprint,
        **content,
    )
    content["query_contracts"] = (contract.to_dict(),)
    asset = {
        "asset_type": "dimension_scan",
        "dimensions": ("channel",),
        "status": "usable",
        "query_ref": query_ref,
        "result_refs": (result.result_ref,),
        "rows_refs": (result.rows_ref,),
        "completeness_report_refs": (report.report_ref,),
        "completeness_record_refs": (completeness_record.record_ref,),
        "completeness_record_digests": (completeness_record.report_digest,),
        "reuse_contract": reuse_contract,
        "created_at": created_at,
        "expires_at": expires_at,
        "row_payload": row_payload,
        "applicable_scans": ("dimension_scan",),
    }
    return asset, content


def _resolved_window(window_id, payload):
    start = str(payload["start_inclusive"])
    end = str(payload["end_exclusive"])
    end_date = date.fromisoformat(end)
    return ResolvedWindow(
        window_id=window_id,
        role="baseline" if "baseline" in window_id or "previous" in window_id else "target",
        label=window_id,
        start_inclusive=start,
        end_exclusive=end,
        timezone=str(payload["timezone"]),
        aggregation="daily_total",
        required_complete_days=max(1, (end_date - date.fromisoformat(start)).days),
        source_watermark_requirement=(end_date.fromordinal(end_date.toordinal() - 1)).isoformat(),
    )
