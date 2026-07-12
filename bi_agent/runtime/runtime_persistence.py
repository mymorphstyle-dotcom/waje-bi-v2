from __future__ import annotations

from dataclasses import fields, replace
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from bi_agent.runtime.canonical_values import canonical_thaw
from bi_agent.runtime.clickhouse_revenue_rows import _query_contract_from_mapping
from bi_agent.runtime.analysis_contracts import (
    AnalysisContract,
    QueryContract,
    analysis_contract_from_dict,
    analysis_contract_signature,
    query_contract_signature,
)
from bi_agent.runtime.dataset_catalog import DatasetSnapshot
from bi_agent.runtime.authoritative_query_chain import (
    AuthoritativeQueryChainError,
    validate_capability_binding_plan_semantics,
)
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)
from bi_agent.runtime.claim_provenance import (
    validate_context_manifest_record,
    validate_trusted_claim_provenance_record,
    validate_verified_claim_record,
)
from bi_agent.runtime.evidence_authority import (
    CapabilityBindingRecord,
    CompletenessRecord,
    EvidenceIntegrityError,
    QueryExecutionRecord,
    RowsRecord,
    RuntimeEvidenceResolver,
    SnapshotRecord,
    canonical_rows_hash,
    canonical_rows_storage_ref,
    canonical_value,
    runtime_evidence_record_integrity_errors,
    _deep_freeze,
)


_HEX64 = r"[0-9a-f]{64}"
_CONTENT_STORAGE_RE = re.compile(rf"^rows-storage:sha256:({_HEX64})$")
_CLICKHOUSE_STORAGE_RE = re.compile(
    rf"^clickhouse-rows:([A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*):({_HEX64}):({_HEX64})$"
)


def authority_record_payload(kind: str, record: Any) -> dict[str, Any]:
    expected = {
        "snapshot": SnapshotRecord,
        "rows": RowsRecord,
        "query_execution": QueryExecutionRecord,
        "completeness": CompletenessRecord,
        "capability_binding": CapabilityBindingRecord,
    }
    if type(record) is not expected.get(kind):
        raise EvidenceIntegrityError(f"runtime_authority_record_type_invalid:{kind}")
    errors = runtime_evidence_record_integrity_errors(record)
    if errors:
        raise EvidenceIntegrityError(errors[0])
    return canonical_value({"kind": kind, "record": record})


def validate_analysis_runtime_records(
    *,
    run_id: str,
    analysis_contract: Mapping[str, Any],
    query_contracts: Sequence[QueryContract],
    query_execution_records: Sequence[QueryExecutionRecord],
    rows_records: Sequence[RowsRecord],
    snapshot_records: Sequence[SnapshotRecord],
    completeness_records: Sequence[CompletenessRecord],
    capability_binding_records: Sequence[CapabilityBindingRecord],
    evidence_manifests: Sequence[Mapping[str, Any]],
    context_manifests: Sequence[Mapping[str, Any]],
    trusted_provenance_records: Sequence[Mapping[str, Any]],
    verified_claims: Sequence[Mapping[str, Any]],
    claim_links: Sequence[Mapping[str, Any]],
    repair_attempts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Close the persistence graph before any database statement is issued."""
    if not run_id:
        raise EvidenceIntegrityError("runtime_persistence_run_id_missing")
    if not isinstance(analysis_contract, Mapping):
        raise EvidenceIntegrityError("runtime_persistence_analysis_contract_invalid")
    expected_analysis_keys = {
        *AnalysisContract.__dataclass_fields__,
        "contract_signature",
    }
    if set(analysis_contract) != expected_analysis_keys:
        raise EvidenceIntegrityError("runtime_persistence_analysis_contract_shape_invalid")
    try:
        typed_analysis = analysis_contract_from_dict(
            {
                key: value
                for key, value in analysis_contract.items()
                if key != "contract_signature"
            }
        )
    except (TypeError, ValueError) as exc:
        raise EvidenceIntegrityError(
            f"runtime_persistence_analysis_contract_shape_invalid:{exc}"
        ) from exc
    signature = str(analysis_contract.get("contract_signature") or "")
    analysis_ref = typed_analysis.analysis_contract_id
    if len(signature) != 64:
        raise EvidenceIntegrityError("runtime_persistence_analysis_contract_identity_invalid")
    if signature != analysis_contract_signature(typed_analysis):
        raise EvidenceIntegrityError(
            "runtime_persistence_analysis_contract_signature_invalid"
        )
    analysis_payload = canonical_value(
        {**typed_analysis.to_dict(), "contract_signature": signature}
    )
    if any(type(item) is not QueryContract for item in query_contracts):
        raise EvidenceIntegrityError("runtime_persistence_query_contract_type_invalid")
    query_by_ref = {item.query_contract_id: item for item in query_contracts}
    if len(query_by_ref) != len(query_contracts):
        raise EvidenceIntegrityError("runtime_persistence_query_contract_duplicate")
    for contract in query_contracts:
        if (
            contract.analysis_contract_ref != analysis_ref
            or query_contract_signature(contract) != contract.contract_signature
        ):
            raise EvidenceIntegrityError("runtime_persistence_query_contract_integrity")

    typed_groups = (
        ("query_execution", QueryExecutionRecord, query_execution_records),
        ("rows", RowsRecord, rows_records),
        ("snapshot", SnapshotRecord, snapshot_records),
        ("completeness", CompletenessRecord, completeness_records),
        ("capability_binding", CapabilityBindingRecord, capability_binding_records),
    )
    for kind, expected, records in typed_groups:
        for record in records:
            if type(record) is not expected:
                raise EvidenceIntegrityError(f"runtime_persistence_{kind}_type_invalid")
            errors = runtime_evidence_record_integrity_errors(record)
            if errors:
                raise EvidenceIntegrityError(errors[0])

    query_records = _unique_by(
        query_execution_records, "result_ref", "runtime_persistence_query_result_duplicate"
    )
    rows_by_ref = _unique_by(rows_records, "rows_ref", "runtime_persistence_rows_record_duplicate")
    snapshots_by_ref = _unique_by(
        snapshot_records, "snapshot_ref", "runtime_persistence_snapshot_record_duplicate"
    )
    completeness_by_ref = _unique_by(
        completeness_records, "record_ref", "runtime_persistence_completeness_record_duplicate"
    )
    bindings_by_ref = _unique_by(
        capability_binding_records,
        "record_ref",
        "runtime_persistence_capability_binding_duplicate",
    )
    _validate_query_contract_analysis_closure(
        typed_analysis,
        query_contracts,
        snapshots_by_ref,
    )
    for query in query_execution_records:
        contract = query_by_ref.get(query.query_contract_ref)
        if contract is None or canonical_value(contract) != canonical_value(query.contract):
            raise EvidenceIntegrityError("runtime_persistence_query_contract_missing")
        rows = rows_by_ref.get(query.rows_ref)
        if rows is None:
            raise EvidenceIntegrityError("runtime_persistence_rows_record_missing")
        if rows.row_count != query.row_count or rows.rows_content_hash != query.rows_content_hash:
            raise EvidenceIntegrityError("runtime_persistence_rows_record_link_mismatch")
        for snapshot_ref, record_ref, record_digest in zip(
            query.source_snapshot_refs,
            query.source_snapshot_record_refs,
            query.source_snapshot_record_digests,
        ):
            snapshot = snapshots_by_ref.get(snapshot_ref)
            if snapshot is None:
                raise EvidenceIntegrityError("runtime_persistence_snapshot_record_missing")
            if (
                snapshot.record_ref != record_ref
                or snapshot.record_digest != record_digest
            ):
                raise EvidenceIntegrityError("runtime_persistence_snapshot_record_link_mismatch")
    reports_by_result = _unique_by(
        completeness_records,
        "result_ref",
        "runtime_persistence_completeness_result_duplicate",
    )
    for report in completeness_records:
        query = query_records.get(report.result_ref)
        if (
            query is None
            or report.query_contract_ref != query.query_contract_ref
            or report.report_ref != query.completeness_report_ref
        ):
            raise EvidenceIntegrityError("runtime_persistence_completeness_link_mismatch")
    if set(reports_by_result) != set(query_records):
        raise EvidenceIntegrityError("runtime_persistence_completeness_chain_incomplete")

    all_report_records = set(completeness_by_ref)
    bound_result_refs: set[str] = set()
    supported_claim_intents: set[str] = set()
    for binding in capability_binding_records:
        if binding.analysis_contract_ref != analysis_ref:
            raise EvidenceIntegrityError("runtime_persistence_binding_analysis_contract_mismatch")
        _validate_capability_binding_analysis_closure(
            typed_analysis,
            binding,
            query_by_ref,
        )
        supported_claim_intents.update(binding.supported_claim_types)
        groups = (
            (
                binding.query_contract_refs, binding.result_refs,
                binding.query_execution_record_refs,
                binding.query_execution_record_digests,
                binding.rows_refs, binding.rows_metadata_record_refs,
                binding.rows_metadata_record_digests, binding.rows_content_hashes,
                binding.completeness_report_refs,
                binding.completeness_record_refs,
                binding.completeness_record_digests,
            ),
            (
                binding.validation_query_contract_refs, binding.validation_result_refs,
                binding.validation_query_execution_record_refs,
                binding.validation_query_execution_record_digests,
                binding.validation_rows_refs,
                binding.validation_rows_metadata_record_refs,
                binding.validation_rows_metadata_record_digests,
                binding.validation_rows_content_hashes,
                binding.validation_completeness_report_refs,
                binding.validation_completeness_record_refs,
                binding.validation_completeness_record_digests,
            ),
        )
        for (
            query_refs, result_refs, query_record_refs, query_record_digests,
            rows_refs, rows_record_refs, rows_record_digests, rows_hashes,
            report_aliases, report_refs, report_digests,
        ) in groups:
            for index, result_ref in enumerate(result_refs):
                bound_result_refs.add(result_ref)
                query = query_records.get(result_ref)
                if (
                    query is None
                    or query.query_contract_ref != query_refs[index]
                    or query.record_ref != query_record_refs[index]
                    or query.record_digest != query_record_digests[index]
                    or query.rows_ref != rows_refs[index]
                ):
                    raise EvidenceIntegrityError("runtime_persistence_binding_query_link_mismatch")
                rows = rows_by_ref.get(rows_refs[index])
                if (
                    rows is None
                    or rows.record_ref != rows_record_refs[index]
                    or rows.record_digest != rows_record_digests[index]
                    or rows.rows_content_hash != rows_hashes[index]
                ):
                    raise EvidenceIntegrityError("runtime_persistence_binding_rows_link_mismatch")
                if report_refs[index] not in all_report_records:
                    raise EvidenceIntegrityError("runtime_persistence_binding_completeness_missing")
                report = completeness_by_ref[report_refs[index]]
                if (
                    report.result_ref != result_ref
                    or report.report_ref != report_aliases[index]
                    or report.report_digest != report_digests[index]
                ):
                    raise EvidenceIntegrityError(
                        "runtime_persistence_binding_completeness_link_mismatch"
                    )
    unbound_result_refs = set(query_records) - bound_result_refs
    repair_signatures = {
        str(item.get("failed_signature") or "")
        for item in repair_attempts
        if isinstance(item, Mapping)
        and all(item.get(key) for key in ("attempt_ref", "failed_signature", "action", "reason"))
    }
    repaired_unbound_results = bool(unbound_result_refs) and all(
        query_records[result_ref].contract_signature in repair_signatures
        for result_ref in unbound_result_refs
    )
    if any(
        query_records[result_ref].contract_signature not in repair_signatures
        for result_ref in unbound_result_refs
    ):
        raise EvidenceIntegrityError("runtime_persistence_binding_chain_incomplete")
    unbound_claim_intents = (
        set(typed_analysis.claim_intents)
        if repaired_unbound_results
        else _validated_unbound_claim_intents(
            typed_analysis,
            supported_claim_intents,
        )
    )
    if unbound_claim_intents and (
        verified_claims or claim_links or trusted_provenance_records
    ):
        raise EvidenceIntegrityError(
            "runtime_persistence_unbound_claim_intent_requires_zero_claims"
        )

    evidence_by_ref: dict[str, Mapping[str, Any]] = {}
    for raw in evidence_manifests:
        payload = canonical_value(raw)
        ref = str(payload.get("evidence_ref") or "")
        binding_ref = str(payload.get("binding_record_ref") or "")
        if (
            not ref
            or binding_ref not in bindings_by_ref
        ):
            raise EvidenceIntegrityError("runtime_persistence_evidence_binding_missing")
        binding = bindings_by_ref[binding_ref]
        expected_results = set((*binding.result_refs, *binding.validation_result_refs))
        expected_reports = set(
            (*binding.completeness_record_refs, *binding.validation_completeness_record_refs)
        )
        if (
            set(payload.get("result_refs") or ()) != expected_results
            or set(payload.get("completeness_record_refs") or ()) != expected_reports
        ):
            raise EvidenceIntegrityError("runtime_persistence_evidence_membership_mismatch")
        if ref in evidence_by_ref and evidence_by_ref[ref] != payload:
            raise EvidenceIntegrityError("authority_ref_collision:evidence_manifest")
        evidence_by_ref[ref] = payload

    contexts_by_ref: dict[str, Mapping[str, Any]] = {}
    for raw in context_manifests:
        payload = canonical_value(raw)
        validate_context_manifest_record(payload)
        ref = str(payload["manifest_id"])
        if payload["run_id"] != run_id:
            raise EvidenceIntegrityError("runtime_persistence_context_run_mismatch")
        if ref in contexts_by_ref:
            raise EvidenceIntegrityError("runtime_persistence_context_duplicate")
        contexts_by_ref[ref] = payload
    has_verified_claims = bool(verified_claims)
    if has_verified_claims and not contexts_by_ref:
        raise EvidenceIntegrityError("runtime_persistence_context_missing")
    if not has_verified_claims and contexts_by_ref:
        raise EvidenceIntegrityError("runtime_persistence_zero_claim_context_invalid")

    provenance_by_ref: dict[str, Mapping[str, Any]] = {}
    for raw in trusted_provenance_records:
        payload = canonical_value(raw)
        validate_trusted_claim_provenance_record(payload)
        ref = str(payload["record_ref"])
        if payload["run_id"] != run_id:
            raise EvidenceIntegrityError("runtime_persistence_claim_provenance_run_mismatch")
        if ref in provenance_by_ref:
            raise EvidenceIntegrityError("runtime_persistence_claim_provenance_duplicate")
        provenance_by_ref[ref] = payload

    claims_by_ref: dict[str, Mapping[str, Any]] = {}
    try:
        claim_registry = RuntimeContractRegistry.from_path(
            CANONICAL_RUNTIME_BINDINGS_PATH
        )
    except (OSError, TypeError, ValueError) as exc:
        raise EvidenceIntegrityError(
            "runtime_persistence_claim_strength_registry_invalid"
        ) from exc
    for raw in verified_claims:
        payload = canonical_value(raw)
        context = contexts_by_ref.get(str(payload.get("context_manifest_ref") or ""))
        provenance = provenance_by_ref.get(str(payload.get("provenance_record_ref") or ""))
        if context is None or provenance is None:
            raise EvidenceIntegrityError("runtime_persistence_verified_claim_provenance_missing")
        validate_verified_claim_record(
            payload,
            context_manifest=context,
            evidence_by_ref=evidence_by_ref,
            trusted_provenance=provenance,
        )
        _validate_verified_claim_contract_boundary(
            payload,
            analysis=typed_analysis,
            unbound_claim_intents=unbound_claim_intents,
            evidence_by_ref=evidence_by_ref,
            bindings_by_ref=bindings_by_ref,
            registry=claim_registry,
        )
        ref = str(payload["claim_ref"])
        if payload["run_id"] != run_id:
            raise EvidenceIntegrityError("runtime_persistence_verified_claim_run_mismatch")
        if ref in claims_by_ref:
            raise EvidenceIntegrityError("runtime_persistence_verified_claim_duplicate")
        claims_by_ref[ref] = payload
    if not has_verified_claims and (
        provenance_by_ref or claim_links
    ):
        raise EvidenceIntegrityError("runtime_persistence_zero_claim_provenance_invalid")

    expected_sources_by_context: dict[str, set[tuple[str, str]]] = {
        ref: set() for ref in contexts_by_ref
    }
    for claim in claims_by_ref.values():
        context_ref = str(claim["context_manifest_ref"])
        for evidence_ref in claim["evidence_refs"]:
            expected_sources_by_context[context_ref].add(("evidence", evidence_ref))
            for completeness_ref in evidence_by_ref[evidence_ref].get(
                "completeness_record_refs"
            ) or ():
                expected_sources_by_context[context_ref].add(
                    ("completeness", completeness_ref)
                )
    for context_ref, context in contexts_by_ref.items():
        actual = {
            (str(source["type"]), str(source["ref"]))
            for source in context["sources"]
        }
        if actual != expected_sources_by_context[context_ref]:
            raise EvidenceIntegrityError(
                "runtime_persistence_context_sources_mismatch"
            )
    for evidence in evidence_by_ref.values():
        context_ref = str(evidence.get("context_manifest_ref") or "")
        if has_verified_claims and context_ref not in contexts_by_ref:
            raise EvidenceIntegrityError("runtime_persistence_evidence_context_missing")
        if not has_verified_claims and context_ref:
            raise EvidenceIntegrityError(
                "runtime_persistence_zero_claim_evidence_context_invalid"
            )

    normalized_links = []
    for raw in claim_links:
        payload = canonical_value(raw)
        if (
            str(payload.get("claim_ref") or "") not in claims_by_ref
            or str(payload.get("evidence_ref") or "") not in evidence_by_ref
            or str(payload.get("context_manifest_ref") or "")
            != str(
                evidence_by_ref[str(payload.get("evidence_ref"))].get(
                    "context_manifest_ref"
                )
                or ""
            )
        ):
            raise EvidenceIntegrityError("runtime_persistence_claim_evidence_link_invalid")
        normalized_links.append(payload)
    expected_links = {
        (
            str(claim["claim_ref"]),
            str(evidence_ref),
            str(claim["context_manifest_ref"]),
        )
        for claim in claims_by_ref.values()
        for evidence_ref in claim["evidence_refs"]
    }
    actual_links = {
        (
            str(link["claim_ref"]),
            str(link["evidence_ref"]),
            str(link["context_manifest_ref"]),
        )
        for link in normalized_links
    }
    if actual_links != expected_links or len(actual_links) != len(normalized_links):
        raise EvidenceIntegrityError("runtime_persistence_claim_evidence_links_mismatch")
    normalized_repairs = []
    for raw in repair_attempts:
        payload = canonical_value(raw)
        if any(not payload.get(key) for key in ("attempt_ref", "failed_signature", "action", "reason")):
            raise EvidenceIntegrityError("runtime_persistence_repair_attempt_invalid")
        normalized_repairs.append(payload)
    return {
        "analysis_contract": analysis_payload,
        "query_contracts": tuple(query_contracts),
        "query_execution_records": tuple(query_execution_records),
        "rows_records": tuple(rows_records),
        "snapshot_records": tuple(snapshot_records),
        "completeness_records": tuple(completeness_records),
        "capability_binding_records": tuple(capability_binding_records),
        "evidence_manifests": tuple(evidence_by_ref.values()),
        "context_manifests": tuple(contexts_by_ref.values()),
        "trusted_provenance_records": tuple(provenance_by_ref.values()),
        "verified_claims": tuple(claims_by_ref.values()),
        "claim_links": tuple(normalized_links),
        "repair_attempts": tuple(normalized_repairs),
    }


def _unique_by(records: Sequence[Any], field: str, code: str) -> dict[str, Any]:
    result = {}
    for record in records:
        ref = str(getattr(record, field))
        if ref in result:
            raise EvidenceIntegrityError(code)
        result[ref] = record
    return result


def _validate_query_contract_analysis_closure(
    analysis: AnalysisContract,
    contracts: Sequence[QueryContract],
    snapshots_by_ref: Mapping[str, SnapshotRecord],
) -> None:
    for contract in contracts:
        _validate_query_contract_analysis_semantics(analysis, contract)
        for snapshot_ref in contract.dataset_snapshot_refs:
            snapshot = snapshots_by_ref.get(snapshot_ref)
            if snapshot is None or snapshot.snapshot.dataset_id not in set(
                analysis.dataset_requirements
            ):
                raise EvidenceIntegrityError(
                    "runtime_persistence_query_dataset_requirement_mismatch"
                )


def _validate_query_contract_analysis_semantics(
    analysis: AnalysisContract,
    contract: QueryContract,
) -> None:
    metrics = {
        (item.metric_id, item.dataset_id): item
        for item in analysis.metric_bindings
    }
    dimensions = {
        (item.dimension_id, item.dataset_id): item
        for item in analysis.dimension_bindings
    }
    windows = {item.window_id: item for item in analysis.resolved_windows}
    datasets = set(analysis.dataset_requirements)
    if any(
        item.dataset_id not in datasets
        for item in (*analysis.metric_bindings, *analysis.dimension_bindings)
    ):
        raise EvidenceIntegrityError(
            "runtime_persistence_analysis_dataset_requirement_mismatch"
        )
    if not set(analysis.target_metric_refs).issubset(
        {item.contract_ref for item in analysis.metric_bindings}
    ):
        raise EvidenceIntegrityError(
            "runtime_persistence_analysis_target_metric_mismatch"
        )
    if contract.permission_scope != analysis.permission_scope:
        raise EvidenceIntegrityError(
            "runtime_persistence_query_permission_scope_mismatch"
        )
    for metric in contract.metric_bindings:
        if canonical_value(metrics.get((metric.metric_id, metric.dataset_id))) != canonical_value(metric):
            raise EvidenceIntegrityError(
                "runtime_persistence_query_metric_binding_mismatch"
            )
        if metric.dataset_id not in datasets:
            raise EvidenceIntegrityError(
                "runtime_persistence_query_dataset_requirement_mismatch"
            )
    for dimension in contract.dimension_bindings:
        if canonical_value(
            dimensions.get((dimension.dimension_id, dimension.dataset_id))
        ) != canonical_value(dimension):
            raise EvidenceIntegrityError(
                "runtime_persistence_query_dimension_binding_mismatch"
            )
        if dimension.dataset_id not in datasets:
            raise EvidenceIntegrityError(
                "runtime_persistence_query_dataset_requirement_mismatch"
            )
    if set(contract.window_refs) != {
        item.window_id for item in contract.resolved_windows
    }:
        raise EvidenceIntegrityError(
            "runtime_persistence_query_window_ref_mismatch"
        )
    for window in contract.resolved_windows:
        if canonical_value(windows.get(window.window_id)) != canonical_value(window):
            raise EvidenceIntegrityError(
                "runtime_persistence_query_window_binding_mismatch"
            )


def _validated_unbound_claim_intents(
    analysis: AnalysisContract,
    supported_claim_intents: set[str],
) -> set[str]:
    sentinel = "unbound_claim_intent"
    intents = set(analysis.claim_intents)
    unsupported = intents - supported_claim_intents - {sentinel}
    metric_claim_intents = {
        claim_type
        for metric in analysis.metric_bindings
        for claim_type in metric.claim_types
    }
    boundary_gaps = tuple(
        gap
        for gap in analysis.contract_gaps
        if gap.requires_clarification
        or gap.gap_type in {
            "source_unbound",
            "permission_blocked",
            "contract_partial",
        }
    )
    bound_metric_ids = {metric.metric_id for metric in analysis.metric_bindings}
    required_dataset_ids = set(analysis.dataset_requirements)
    target_metric_refs = set(analysis.target_metric_refs)
    claim_authorizing_boundary_gaps = tuple(
        gap
        for gap in boundary_gaps
        if _boundary_gap_authorizes_claim_intents(
            gap,
            bound_metric_ids=bound_metric_ids,
            required_dataset_ids=required_dataset_ids,
            target_metric_refs=target_metric_refs,
        )
    )
    boundary_claim_intents = {
        claim_type
        for gap in claim_authorizing_boundary_gaps
        for claim_type in gap.affected_claim_types
    }
    has_terminal_boundary_gap = any(
        gap.requires_clarification
        and bool(gap.owner)
        and bool(gap.repair_options)
        and (
            "analysis_contract" in gap.affected_capabilities
            or bool(
                set(gap.affected_capabilities).intersection(
                    analysis.capability_requirements
                )
            )
        )
        for gap in boundary_gaps
    )
    try:
        registry = RuntimeContractRegistry.from_path(
            CANONICAL_RUNTIME_BINDINGS_PATH
        )
        capability_claim_intents = {
            claim_type
            for gap in boundary_gaps
            for capability_id in gap.affected_capabilities
            if capability_id in analysis.capability_requirements
            for claim_type in registry.capability_inputs(capability_id).get(
                "supported_claim_types", ()
            )
        }
        queryless_capability_claim_intents = {
            claim_type
            for capability_id in analysis.capability_requirements
            for inputs in (registry.capability_inputs(capability_id),)
            if has_terminal_boundary_gap
            if not inputs.get("query_families")
            and not inputs.get("required_metrics")
            and inputs.get("minimum_readiness", {}).get("required_slots") == "none"
            for claim_type in inputs.get("supported_claim_types", ())
        }
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise EvidenceIntegrityError(
            "runtime_persistence_analysis_claim_contract_invalid"
        ) from exc
    if not unsupported.issubset(
        queryless_capability_claim_intents
        | (metric_claim_intents | capability_claim_intents).intersection(
            boundary_claim_intents
        )
    ):
        raise EvidenceIntegrityError(
            "runtime_persistence_analysis_claim_intent_unsupported"
        )
    if sentinel not in intents:
        return unsupported
    expected_capabilities = (
        analysis.capability_requirements or ("analysis_contract",)
    )
    direct_gaps = tuple(
        gap
        for gap in analysis.contract_gaps
        if gap.gap_type == "contract_partial"
        and gap.gap_id == "claim_intents:unbound"
        and gap.dataset_id == ""
        and gap.affected_capabilities == expected_capabilities
        and gap.affected_claim_types == (sentinel,)
        and gap.owner == "contract_owner"
        and gap.repair_options
        == (
            "bind_capability_claim_types",
            "bind_metric_claim_types",
            "clarify_claim_intent",
        )
        and gap.requires_clarification is True
        and canonical_value(gap.diagnostic_context) == {}
    )
    unsupported_gaps = tuple(
        gap
        for gap in analysis.contract_gaps
        if gap.gap_type == "contract_partial"
        and len(gap.affected_claim_types) == 1
        and gap.affected_claim_types != (sentinel,)
        and gap.gap_id
        == f"claim_intent:{gap.affected_claim_types[0]}:unsupported"
        and gap.dataset_id == ""
        and gap.affected_capabilities == expected_capabilities
        and gap.owner == "contract_owner"
        and gap.repair_options
        == ("choose_supported_claim_intent", "clarify_claim_intent")
        and gap.requires_clarification is True
        and canonical_value(gap.diagnostic_context) == {}
    )
    if (len(direct_gaps) != 1) == (not unsupported_gaps):
        raise EvidenceIntegrityError(
            "runtime_persistence_unbound_claim_intent_gap_invalid"
        )
    return {sentinel, *unsupported}


def _boundary_gap_authorizes_claim_intents(
    gap: Any,
    *,
    bound_metric_ids: set[str],
    required_dataset_ids: set[str],
    target_metric_refs: set[str],
) -> bool:
    if not gap.affected_claim_types:
        return False
    parts = gap.gap_id.split(":")
    if parts[0] == "metric" and len(parts) >= 3:
        metric_id = parts[1]
        diagnostic = canonical_value(gap.diagnostic_context)
        if metric_id in bound_metric_ids:
            return True
        try:
            registry = RuntimeContractRegistry.from_path(
                CANONICAL_RUNTIME_BINDINGS_PATH
            )
            metric_sources = registry.metric_sources(metric_id)
            source_ids = tuple(metric_sources)
            metric_contract_refs = {
                str(source.get("contract_ref") or "")
                for source in metric_sources.values()
                if str(source.get("contract_ref") or "")
            }
        except (KeyError, OSError, TypeError, ValueError):
            return False
        return (
            len(source_ids) > 1
            and metric_contract_refs.issubset(target_metric_refs)
            and gap.gap_id
            == f"metric:{metric_id}:source_ambiguous:{','.join(source_ids)}"
            and gap.gap_type == "contract_partial"
            and gap.dataset_id == ""
            and gap.owner == "contract_owner"
            and gap.repair_options
            == ("select_dataset_requirement", "clarify_source_scope")
            and gap.requires_clarification is True
            and diagnostic.get("item_kind") == "metric"
            and diagnostic.get("item_id") == metric_id
            and set(diagnostic.get("claim_intents") or ())
            == set(gap.affected_claim_types)
        )
    if parts[0] == "dataset":
        return bool(gap.dataset_id) and gap.dataset_id in required_dataset_ids
    return True


def _validate_verified_claim_contract_boundary(
    payload: Mapping[str, Any],
    *,
    analysis: AnalysisContract,
    unbound_claim_intents: set[str],
    evidence_by_ref: Mapping[str, Mapping[str, Any]],
    bindings_by_ref: Mapping[str, CapabilityBindingRecord],
    registry: RuntimeContractRegistry,
) -> None:
    claim_type = str(payload.get("claim_type") or "")
    if (
        claim_type not in analysis.claim_intents
        or claim_type in unbound_claim_intents
    ):
        raise EvidenceIntegrityError(
            "runtime_persistence_verified_claim_intent_mismatch"
        )
    linked_bindings = tuple(
        bindings_by_ref[
            str(evidence_by_ref[evidence_ref]["binding_record_ref"])
        ]
        for evidence_ref in payload.get("evidence_refs") or ()
        if evidence_ref in evidence_by_ref
    )
    if not linked_bindings or any(
        claim_type not in binding.supported_claim_types
        for binding in linked_bindings
    ):
        raise EvidenceIntegrityError(
            "runtime_persistence_verified_claim_binding_mismatch"
        )
    try:
        claim_strength_rank = registry.claim_strength_rank(
            str(payload.get("claim_strength") or "")
        )
    except KeyError as exc:
        raise EvidenceIntegrityError(
            "runtime_persistence_verified_claim_strength_invalid"
        ) from exc
    if any(
        binding.claim_strength_taxonomy_version
        != registry.claim_strength_taxonomy_version
        or claim_strength_rank > binding.maximum_claim_strength_rank
        for binding in linked_bindings
    ):
        raise EvidenceIntegrityError(
            "runtime_persistence_verified_claim_strength_ceiling_exceeded"
        )


def _analysis_contract_from_envelope(
    raw: Any,
    *,
    stored_signature: str,
    code: str,
) -> AnalysisContract:
    if not isinstance(raw, Mapping) or set(raw) != {
        *AnalysisContract.__dataclass_fields__,
        "contract_signature",
    }:
        raise EvidenceIntegrityError(code)
    try:
        analysis = analysis_contract_from_dict(
            {key: value for key, value in raw.items() if key != "contract_signature"}
        )
    except (TypeError, ValueError) as exc:
        raise EvidenceIntegrityError(code) from exc
    if (
        str(raw.get("contract_signature") or "") != stored_signature
        or analysis_contract_signature(analysis) != stored_signature
    ):
        raise EvidenceIntegrityError(code)
    return analysis


def _validate_capability_binding_analysis_closure(
    analysis: AnalysisContract,
    binding: CapabilityBindingRecord,
    query_by_ref: Mapping[str, QueryContract] | None,
) -> None:
    if binding.capability_id not in set(analysis.capability_requirements):
        raise EvidenceIntegrityError(
            "runtime_persistence_binding_capability_requirement_mismatch"
        )
    scoped_query_by_ref = query_by_ref
    if query_by_ref is not None:
        binding_query_refs = tuple(
            dict.fromkeys(
                (
                    *binding.query_contract_refs,
                    *binding.validation_query_contract_refs,
                )
            )
        )
        scoped_query_by_ref = {
            ref: query_by_ref[ref]
            for ref in binding_query_refs
            if ref in query_by_ref
        }
    try:
        validate_capability_binding_plan_semantics(
            binding,
            RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH),
            scoped_query_by_ref,
        )
    except (AuthoritativeQueryChainError, KeyError, OSError, TypeError, ValueError) as exc:
        raise EvidenceIntegrityError(
            f"runtime_persistence_binding_plan_semantics_invalid:{exc}"
        ) from exc


class PostgresRuntimeEvidenceResolver(RuntimeEvidenceResolver):
    """Read-only adapter from normalized PostgreSQL authority rows to Task 6 records."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def resolve_query_execution(self, result_ref: str) -> QueryExecutionRecord | None:
        return self._resolve_query("q.result_ref = %(ref)s", result_ref)

    def resolve_query_execution_record(
        self, record_ref: str
    ) -> QueryExecutionRecord | None:
        return self._resolve_query("q.record_ref = %(ref)s", record_ref)

    def _resolve_query(self, where: str, ref: str) -> QueryExecutionRecord | None:
        row = self._one(
            f"""
            SELECT q.record_ref, q.record_digest, q.result_ref,
                   q.query_contract_ref, q.rows_ref, q.payload,
                   q.run_id AS authority_run_id,
                   qr.result_ref AS run_result_ref, qr.run_id,
                   qr.query_contract_id AS run_query_contract_id,
                   qr.execution_status, qr.query_hash AS run_query_hash,
                   qr.rows_ref AS run_rows_ref,
                   qr.completeness_report_ref AS run_completeness_report_ref,
                   qr.payload AS result_payload,
                   qc.query_contract_id AS contract_query_id,
                   qc.run_id AS contract_run_id,
                   qc.analysis_contract_id,
                   qc.contract_signature AS stored_contract_signature,
                   qc.payload AS contract_payload,
                   ac.run_id AS analysis_run_id,
                   ac.contract_signature AS stored_analysis_signature,
                   ac.payload AS analysis_payload,
                   ar.run_id AS analysis_run_id_actual,
                   ar.thread_id, ar.topic_id
            FROM waje_runtime.query_execution_authority q
            JOIN waje_runtime.query_runs qr ON qr.result_ref = q.result_ref
            JOIN waje_runtime.query_contracts qc
              ON qc.query_contract_id = qr.query_contract_id
            JOIN waje_runtime.analysis_contracts ac
              ON ac.analysis_contract_id = qc.analysis_contract_id
            JOIN waje_runtime.analysis_runs ar ON ar.run_id = qr.run_id
            WHERE {where}
            """,
            {"ref": str(ref)},
        )
        if row is None:
            return None
        row = _row_mapping(
            row,
            (
                "record_ref", "record_digest", "result_ref", "query_contract_ref",
                "rows_ref", "payload", "authority_run_id", "run_result_ref",
                "run_id", "run_query_contract_id", "execution_status",
                "run_query_hash", "run_rows_ref", "run_completeness_report_ref",
                "result_payload", "contract_query_id", "contract_run_id",
                "analysis_contract_id", "stored_contract_signature",
                "contract_payload", "analysis_run_id", "stored_analysis_signature",
                "analysis_payload", "analysis_run_id_actual", "thread_id", "topic_id",
            ),
        )
        record = _record_from_row("query_execution", row)
        _require_columns(
            record,
            row,
            {
                "record_ref": "record_ref",
                "record_digest": "record_digest",
                "result_ref": "result_ref",
                "query_contract_ref": "query_contract_ref",
                "rows_ref": "rows_ref",
            },
            "query_execution_record_column_mismatch",
        )
        _validate_query_execution_join(record, row)
        return record

    def resolve_rows(self, rows_ref: str) -> RowsRecord | None:
        return self._resolve_rows("r.rows_ref = %(ref)s", rows_ref)

    def resolve_rows_record(self, record_ref: str) -> RowsRecord | None:
        return self._resolve_rows("r.record_ref = %(ref)s", record_ref)

    def _resolve_rows(self, where: str, ref: str) -> RowsRecord | None:
        row = self._one(
            f"""
            SELECT r.record_ref, r.record_digest, r.rows_ref,
                   r.rows_content_hash, r.row_count, r.unique_key_fields,
                   r.storage_ref, r.payload,
                   q.run_id AS authority_run_id,
                   q.rows_ref AS query_rows_ref,
                   qr.run_id, qr.rows_ref AS run_rows_ref
            FROM waje_runtime.rows_metadata_authority r
            JOIN waje_runtime.query_execution_authority q ON q.rows_ref = r.rows_ref
            JOIN waje_runtime.query_runs qr ON qr.result_ref = q.result_ref
            WHERE {where}
            """,
            {"ref": str(ref)},
        )
        if row is None:
            return None
        row = _row_mapping(
            row,
            (
                "record_ref", "record_digest", "rows_ref", "rows_content_hash",
                "row_count", "unique_key_fields", "storage_ref", "payload",
                "authority_run_id", "query_rows_ref", "run_id", "run_rows_ref",
            ),
        )
        record = _record_from_row("rows", row)
        _require_columns(
            record,
            row,
            {
                "record_ref": "record_ref",
                "record_digest": "record_digest",
                "rows_ref": "rows_ref",
                "rows_content_hash": "rows_content_hash",
                "row_count": "row_count",
                "unique_key_fields": "unique_key_fields",
                "storage_ref": "storage_ref",
            },
            "rows_record_column_mismatch",
        )
        if (
            str(row.get("authority_run_id") or "")
            != str(row.get("run_id") or "")
            or record.rows_ref != row.get("query_rows_ref")
            or record.rows_ref != row.get("run_rows_ref")
        ):
            raise EvidenceIntegrityError("rows_record_run_membership_mismatch")
        return record

    def resolve_snapshot(self, snapshot_ref: str) -> SnapshotRecord | None:
        row = self._one(
            """
            SELECT s.record_ref, s.record_digest, s.snapshot_ref, s.payload
            FROM waje_runtime.snapshot_authority s
            WHERE s.snapshot_ref = %(ref)s
            """,
            {"ref": str(snapshot_ref)},
        )
        if row is None:
            return None
        row = _row_mapping(
            row, ("record_ref", "record_digest", "snapshot_ref", "payload")
        )
        record = _record_from_row("snapshot", row)
        _require_columns(
            record,
            row,
            {
                "record_ref": "record_ref",
                "record_digest": "record_digest",
                "snapshot_ref": "snapshot_ref",
            },
            "snapshot_record_column_mismatch",
        )
        return record

    def resolve_completeness(self, record_ref: str) -> CompletenessRecord | None:
        return self._resolve_completeness("c.record_ref = %(ref)s", record_ref)

    def resolve_latest_completeness(
        self, report_ref: str
    ) -> CompletenessRecord | None:
        return self._resolve_completeness(
            "c.report_ref = %(ref)s ORDER BY c.created_at DESC, c.record_ref DESC LIMIT 1",
            report_ref,
        )

    def _resolve_completeness(
        self, where: str, ref: str
    ) -> CompletenessRecord | None:
        row = self._one(
            f"""
            SELECT c.record_ref, c.report_ref, c.report_digest,
                   c.result_ref, c.query_contract_ref, c.payload,
                   c.run_id AS authority_run_id,
                   c.completeness_status AS stored_completeness_status,
                   c.analysis_readiness AS stored_analysis_readiness,
                   qr.run_id, qr.query_contract_id AS run_query_contract_id,
                   qr.result_ref AS run_result_ref,
                   qr.completeness_report_ref AS run_completeness_report_ref,
                   qc.run_id AS contract_run_id,
                   qc.query_contract_id AS contract_query_id,
                   qc.analysis_contract_id,
                   qc.contract_signature AS stored_contract_signature,
                   qc.payload AS contract_payload,
                   ac.run_id AS analysis_run_id,
                   ac.contract_signature AS stored_analysis_signature,
                   ac.payload AS analysis_payload,
                   ar.run_id AS analysis_run_id_actual
            FROM waje_runtime.query_completeness_reports c
            JOIN waje_runtime.query_runs qr ON qr.result_ref = c.result_ref
            JOIN waje_runtime.query_contracts qc
              ON qc.query_contract_id = c.query_contract_ref
            JOIN waje_runtime.analysis_contracts ac
              ON ac.analysis_contract_id = qc.analysis_contract_id
            JOIN waje_runtime.analysis_runs ar ON ar.run_id = c.run_id
            WHERE {where}
            """,
            {"ref": str(ref)},
        )
        if row is None:
            return None
        row = _row_mapping(
            row,
            (
                "record_ref", "report_ref", "report_digest", "result_ref",
                "query_contract_ref", "payload", "authority_run_id",
                "stored_completeness_status", "stored_analysis_readiness",
                "run_id", "run_query_contract_id", "run_result_ref",
                "run_completeness_report_ref", "contract_run_id",
                "contract_query_id", "analysis_contract_id",
                "stored_contract_signature", "contract_payload", "analysis_run_id",
                "stored_analysis_signature", "analysis_payload",
                "analysis_run_id_actual",
            ),
        )
        record = _record_from_row("completeness", row)
        _require_columns(
            record,
            row,
            {
                "record_ref": "record_ref",
                "report_ref": "report_ref",
                "report_digest": "report_digest",
                "result_ref": "result_ref",
                "query_contract_ref": "query_contract_ref",
            },
            "completeness_record_column_mismatch",
        )
        _validate_completeness_join(record, row)
        return record

    def resolve_capability_binding(
        self, binding_ref: str
    ) -> CapabilityBindingRecord | None:
        row = self._one(
            """
            SELECT b.record_ref, b.binding_digest, b.capability_id,
                   b.claim_strength_taxonomy_version,
                   b.maximum_claim_strength_rank, b.payload,
                   b.run_id AS authority_run_id,
                   b.analysis_contract_id AS stored_analysis_contract_id,
                   ac.run_id AS analysis_run_id,
                   ac.contract_signature AS stored_analysis_signature,
                   ac.payload AS analysis_payload,
                   ar.run_id AS analysis_run_id_actual
            FROM waje_runtime.capability_binding_authority b
            JOIN waje_runtime.analysis_contracts ac
              ON ac.analysis_contract_id = b.analysis_contract_id
            JOIN waje_runtime.analysis_runs ar ON ar.run_id = b.run_id
            WHERE b.record_ref = %(ref)s
            """,
            {"ref": str(binding_ref)},
        )
        if row is None:
            return None
        row = _row_mapping(
            row,
            (
                "record_ref", "binding_digest", "capability_id",
                "claim_strength_taxonomy_version", "maximum_claim_strength_rank",
                "payload", "authority_run_id", "stored_analysis_contract_id",
                "analysis_run_id", "stored_analysis_signature", "analysis_payload",
                "analysis_run_id_actual",
            ),
        )
        record = _record_from_row("capability_binding", row)
        _require_columns(
            record,
            row,
            {
                "record_ref": "record_ref",
                "binding_digest": "binding_digest",
                "capability_id": "capability_id",
                "claim_strength_taxonomy_version": "claim_strength_taxonomy_version",
                "maximum_claim_strength_rank": "maximum_claim_strength_rank",
            },
            "capability_binding_record_column_mismatch",
        )
        _validate_binding_join(record, row)
        return record

    def resolve_verified_claim(self, claim_ref: str) -> Mapping[str, Any] | None:
        row = self._one(
            """
            SELECT claim_ref, claim_digest, run_id, context_manifest_ref,
                   provenance_record_ref, payload
            FROM waje_runtime.verified_claims
            WHERE claim_ref = %(ref)s
            """,
            {"ref": str(claim_ref)},
        )
        if row is None:
            return None
        row = _row_mapping(
            row,
            (
                "claim_ref", "claim_digest", "run_id", "context_manifest_ref",
                "provenance_record_ref", "payload",
            ),
        )
        payload = _authority_mapping(row.get("payload"), "verified_claim")
        for key in (
            "claim_ref", "claim_digest", "run_id", "context_manifest_ref",
            "provenance_record_ref",
        ):
            if str(payload.get(key) or "") != str(row.get(key) or ""):
                raise EvidenceIntegrityError(
                    f"verified_claim_column_mismatch:{key}"
                )
        return payload

    def resolve_claim_provenance(
        self, record_ref: str
    ) -> Mapping[str, Any] | None:
        row = self._one(
            """
            SELECT record_ref, record_digest, run_id, payload
            FROM waje_runtime.claim_provenance_records
            WHERE record_ref = %(ref)s
            """,
            {"ref": str(record_ref)},
        )
        if row is None:
            return None
        row = _row_mapping(
            row, ("record_ref", "record_digest", "run_id", "payload")
        )
        payload = _authority_mapping(row.get("payload"), "claim_provenance")
        for key in ("record_ref", "record_digest", "run_id"):
            if str(payload.get(key) or "") != str(row.get(key) or ""):
                raise EvidenceIntegrityError(
                    f"claim_provenance_column_mismatch:{key}"
                )
        validate_trusted_claim_provenance_record(payload)
        return payload

    def _one(self, statement: str, params: Mapping[str, Any]) -> Any:
        return self.connection.execute(statement, dict(params)).fetchone()


class ClickHouseArtifactRowsPayloadLoader:
    """Load aggregate rows by an audited storage locator, outside PostgreSQL."""

    def __init__(
        self,
        *,
        artifact_root: str | Path,
        clickhouse: Any = None,
        max_bytes: int = 32 * 1024 * 1024,
        max_rows: int = 100_000,
        max_nesting_depth: int = 32,
    ) -> None:
        if (
            isinstance(max_bytes, bool)
            or isinstance(max_rows, bool)
            or isinstance(max_nesting_depth, bool)
            or not isinstance(max_bytes, int)
            or not isinstance(max_rows, int)
            or not isinstance(max_nesting_depth, int)
            or min(max_bytes, max_rows, max_nesting_depth) < 1
        ):
            raise EvidenceIntegrityError("rows_payload_limits_invalid")
        self.artifact_root = Path(artifact_root).resolve()
        self.clickhouse = clickhouse
        self.max_bytes = max_bytes
        self.max_rows = max_rows
        self.max_nesting_depth = max_nesting_depth

    def load_rows(self, storage_ref: str) -> tuple[Mapping[str, Any], ...] | None:
        text = str(storage_ref or "")
        artifact_match = _CONTENT_STORAGE_RE.fullmatch(text)
        if artifact_match:
            path = (self.artifact_root / f"{artifact_match.group(1)}.json").resolve()
            if path.parent != self.artifact_root:
                raise EvidenceIntegrityError("rows_storage_ref_unsafe")
            if not path.is_file():
                return None
            try:
                before = path.stat()
            except OSError as exc:
                raise EvidenceIntegrityError("rows_payload_read_failed") from exc
            if before.st_size > self.max_bytes:
                raise EvidenceIntegrityError("rows_payload_too_large")
            try:
                raw_bytes = path.read_bytes()
                if len(raw_bytes) > self.max_bytes:
                    raise EvidenceIntegrityError("rows_payload_too_large")
                after = path.stat()
                if (
                    (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                    or after.st_size != len(raw_bytes)
                ):
                    raise EvidenceIntegrityError("rows_payload_changed_during_read")
                raw = raw_bytes.decode("utf-8", errors="strict")
                value = json.loads(raw)
            except EvidenceIntegrityError:
                raise
            except OSError as exc:
                raise EvidenceIntegrityError("rows_payload_read_failed") from exc
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
                raise EvidenceIntegrityError("rows_payload_json_invalid") from exc
            rows = _rows_from_value(
                value,
                max_rows=self.max_rows,
                max_nesting_depth=self.max_nesting_depth,
            )
            if canonical_rows_storage_ref(rows) != text:
                raise EvidenceIntegrityError("rows_payload_storage_hash_mismatch")
            return rows
        clickhouse_match = _CLICKHOUSE_STORAGE_RE.fullmatch(text)
        if clickhouse_match:
            if self.clickhouse is None or not hasattr(self.clickhouse, "load_rows"):
                raise EvidenceIntegrityError("rows_storage_ref_loader_missing")
            table, revision, content_hash = clickhouse_match.groups()
            loaded = self.clickhouse.load_rows(table=table, revision=revision)
            if not isinstance(loaded, Mapping):
                raise EvidenceIntegrityError("rows_storage_ref_revision_missing")
            if str(loaded.get("revision") or "") != revision:
                raise EvidenceIntegrityError("rows_storage_ref_revision_mismatch")
            rows = _rows_from_value(
                loaded.get("rows"),
                max_rows=self.max_rows,
                max_nesting_depth=self.max_nesting_depth,
            )
            if canonical_rows_hash(rows, ()) != content_hash:
                raise EvidenceIntegrityError("rows_payload_storage_hash_mismatch")
            return rows
        if ".." in text or "/" in text or "\\" in text or ";" in text:
            raise EvidenceIntegrityError("rows_storage_ref_unsafe")
        if text.startswith("clickhouse"):
            raise EvidenceIntegrityError("rows_storage_ref_revision_invalid")
        raise EvidenceIntegrityError("rows_storage_ref_invalid")

    def load_rows_record(self, record: RowsRecord) -> tuple[Mapping[str, Any], ...]:
        errors = runtime_evidence_record_integrity_errors(record)
        if errors:
            raise EvidenceIntegrityError(errors[0])
        rows = self.load_rows(record.storage_ref)
        if rows is None:
            raise EvidenceIntegrityError("rows_payload_missing")
        if len(rows) != record.row_count:
            raise EvidenceIntegrityError("rows_payload_count_mismatch")
        try:
            digest = canonical_rows_hash(rows, record.unique_key_fields)
        except EvidenceIntegrityError as exc:
            raise EvidenceIntegrityError(f"rows_payload_unique_key_invalid:{exc}") from exc
        if digest != record.rows_content_hash:
            raise EvidenceIntegrityError("rows_payload_hash_mismatch")
        return rows


def _rows_from_value(
    value: Any,
    *,
    max_rows: int,
    max_nesting_depth: int,
) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list) or any(not isinstance(row, Mapping) for row in value):
        raise EvidenceIntegrityError("rows_payload_invalid")
    if len(value) > max_rows:
        raise EvidenceIntegrityError("rows_payload_row_limit_exceeded")
    if _nesting_depth(value) > max_nesting_depth:
        raise EvidenceIntegrityError("rows_payload_nesting_limit_exceeded")
    return tuple(canonical_thaw(row) for row in value)


def _nesting_depth(value: Any) -> int:
    maximum = 0
    stack = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        maximum = max(maximum, depth)
        if isinstance(current, Mapping):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
    return maximum


def _record_from_row(kind: str, row: Any) -> Any:
    payload = _json_value(_field(row, "payload"))
    if (
        not isinstance(payload, Mapping)
        or set(payload) != {"kind", "record"}
        or payload.get("kind") != kind
    ):
        if isinstance(payload, Mapping) and payload.get("kind") == kind:
            raise EvidenceIntegrityError(f"{kind}_record_payload_keys_invalid")
        raise EvidenceIntegrityError(f"{kind}_record_kind_mismatch")
    record_payload = payload.get("record")
    if not isinstance(record_payload, Mapping):
        raise EvidenceIntegrityError(f"{kind}_record_payload_invalid")
    try:
        record = _record_from_payload(kind, record_payload)
    except EvidenceIntegrityError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise EvidenceIntegrityError(f"{kind}_record_payload_invalid") from exc
    errors = runtime_evidence_record_integrity_errors(record)
    if errors:
        raise EvidenceIntegrityError(errors[0])
    return record


def _record_from_payload(kind: str, payload: Mapping[str, Any]) -> Any:
    data = canonical_thaw(payload)
    record_types = {
        "snapshot": SnapshotRecord,
        "rows": RowsRecord,
        "query_execution": QueryExecutionRecord,
        "completeness": CompletenessRecord,
        "capability_binding": CapabilityBindingRecord,
    }
    expected_fields = {field.name for field in fields(record_types[kind])}
    if set(data) != expected_fields:
        raise EvidenceIntegrityError(f"{kind}_record_fields_invalid")
    if kind == "snapshot":
        snapshot_payload = dict(data["snapshot"])
        for name in (
            "schema_fields", "permission_scopes", "source_checksums", "date_range",
            "no_data_partitions", "no_data_partition_windows",
        ):
            snapshot_payload[name] = tuple(snapshot_payload.get(name) or ())
        return SnapshotRecord(
            record_ref=str(data["record_ref"]),
            record_digest=str(data["record_digest"]),
            snapshot_ref=str(data["snapshot_ref"]),
            payload=_deep_freeze(data["payload"]),
            payload_digest=str(data["payload_digest"]),
            snapshot=DatasetSnapshot(**snapshot_payload),
        )
    if kind == "rows":
        return RowsRecord(
            record_ref=str(data["record_ref"]),
            record_digest=str(data["record_digest"]),
            rows_ref=str(data["rows_ref"]),
            rows_content_hash=str(data["rows_content_hash"]),
            row_count=int(data["row_count"]),
            unique_key_fields=tuple(data["unique_key_fields"]),
            storage_ref=str(data["storage_ref"]),
            metadata_payload=_deep_freeze(data["metadata_payload"]),
        )
    if kind == "query_execution":
        if set(data["query_contract"]) != {
            field.name for field in fields(QueryContract)
        }:
            raise EvidenceIntegrityError("query_contract_payload_fields_invalid")
        contract = _query_contract_from_mapping(data["query_contract"])
        contract = replace(
            contract,
            filters=tuple(_deep_freeze(item) for item in contract.filters),
            query_parameters=_deep_freeze(contract.query_parameters),
        )
        return QueryExecutionRecord(
            record_ref=str(data["record_ref"]),
            record_digest=str(data["record_digest"]),
            record_payload=_deep_freeze(data["record_payload"]),
            query_contract_ref=str(data["query_contract_ref"]),
            contract_signature=str(data["contract_signature"]),
            query_contract=_deep_freeze(data["query_contract"]),
            contract=contract,
            query_hash=str(data["query_hash"]),
            execution_attempt_ref=str(data["execution_attempt_ref"]),
            result_ref=str(data["result_ref"]),
            rows_ref=str(data["rows_ref"]),
            completeness_report_ref=str(data["completeness_report_ref"]),
            execution_status=str(data["execution_status"]),
            row_count=int(data["row_count"]),
            rows_content_hash=str(data["rows_content_hash"]),
            source_snapshot_refs=tuple(data["source_snapshot_refs"]),
            source_snapshot_record_refs=tuple(data["source_snapshot_record_refs"]),
            source_snapshot_record_digests=tuple(data["source_snapshot_record_digests"]),
            result_payload=_deep_freeze(data["result_payload"]),
        )
    if kind == "completeness":
        return CompletenessRecord(
            record_ref=str(data["record_ref"]),
            report_ref=str(data["report_ref"]),
            query_contract_ref=str(data["query_contract_ref"]),
            result_ref=str(data["result_ref"]),
            report_digest=str(data["report_digest"]),
            report_payload=_deep_freeze(data["report_payload"]),
        )
    if kind == "capability_binding":
        tuple_fields = {
            field.name
            for field in fields(CapabilityBindingRecord)
            if field.name.endswith(("_refs", "_digests", "_hashes", "_types", "_statuses"))
        }
        tuple_fields.update({"query_contract_refs", "result_refs", "rows_refs"})
        kwargs = {}
        for field in fields(CapabilityBindingRecord):
            value = data[field.name]
            kwargs[field.name] = tuple(value) if field.name in tuple_fields else value
        kwargs["plan_payload"] = _deep_freeze(kwargs["plan_payload"])
        kwargs["binding_payload"] = _deep_freeze(kwargs["binding_payload"])
        return CapabilityBindingRecord(**kwargs)
    raise EvidenceIntegrityError(f"runtime_authority_record_kind_invalid:{kind}")


def _require_columns(
    record: Any,
    row: Any,
    columns: Mapping[str, str],
    code: str,
) -> None:
    for attribute, column in columns.items():
        expected = canonical_value(getattr(record, attribute))
        actual = _field(row, column)
        if isinstance(expected, (list, dict)):
            actual = _json_value(actual)
        if actual != expected:
            raise EvidenceIntegrityError(code)


def _validate_query_execution_join(
    record: QueryExecutionRecord,
    row: Mapping[str, Any],
) -> None:
    run_ids = {
        str(row.get(name) or "")
        for name in (
            "authority_run_id",
            "run_id",
            "contract_run_id",
            "analysis_run_id",
            "analysis_run_id_actual",
        )
    }
    if len(run_ids) != 1 or "" in run_ids:
        raise EvidenceIntegrityError("query_execution_run_membership_mismatch")
    scalar_pairs = (
        (record.result_ref, row.get("run_result_ref")),
        (record.query_contract_ref, row.get("run_query_contract_id")),
        (record.query_contract_ref, row.get("contract_query_id")),
        (record.execution_status, row.get("execution_status")),
        (record.query_hash, row.get("run_query_hash")),
        (record.rows_ref, row.get("run_rows_ref")),
        (record.completeness_report_ref, row.get("run_completeness_report_ref")),
        (record.contract.analysis_contract_ref, row.get("analysis_contract_id")),
        (record.contract_signature, row.get("stored_contract_signature")),
    )
    if any(expected != actual for expected, actual in scalar_pairs):
        raise EvidenceIntegrityError("query_execution_join_mirror_mismatch")
    if canonical_value(_json_value(row.get("result_payload"))) != canonical_value(
        record.result_payload
    ):
        raise EvidenceIntegrityError("query_execution_result_payload_mismatch")
    if canonical_value(_json_value(row.get("contract_payload"))) != canonical_value(
        record.query_contract
    ):
        raise EvidenceIntegrityError("query_execution_contract_payload_mismatch")
    analysis_payload = _json_value(row.get("analysis_payload"))
    analysis_signature = str(row.get("stored_analysis_signature") or "")
    analysis = _analysis_contract_from_envelope(
        analysis_payload,
        stored_signature=analysis_signature,
        code="query_execution_analysis_contract_mismatch",
    )
    if analysis.analysis_contract_id != record.contract.analysis_contract_ref:
        raise EvidenceIntegrityError("query_execution_analysis_contract_mismatch")
    try:
        _validate_query_contract_analysis_semantics(analysis, record.contract)
    except EvidenceIntegrityError as exc:
        raise EvidenceIntegrityError(
            "query_execution_analysis_contract_mismatch"
        ) from exc


def _validate_completeness_join(
    record: CompletenessRecord,
    row: Mapping[str, Any],
) -> None:
    run_ids = {
        str(row.get(name) or "")
        for name in (
            "authority_run_id", "run_id", "contract_run_id", "analysis_run_id",
            "analysis_run_id_actual",
        )
    }
    if len(run_ids) != 1 or "" in run_ids:
        raise EvidenceIntegrityError("completeness_run_membership_mismatch")
    report = canonical_value(record.report_payload)
    scalar_pairs = (
        (record.result_ref, row.get("run_result_ref")),
        (record.query_contract_ref, row.get("run_query_contract_id")),
        (record.query_contract_ref, row.get("contract_query_id")),
        (record.report_ref, row.get("run_completeness_report_ref")),
        (report.get("completeness_status"), row.get("stored_completeness_status")),
        (report.get("analysis_readiness"), row.get("stored_analysis_readiness")),
    )
    if any(expected != actual for expected, actual in scalar_pairs):
        raise EvidenceIntegrityError("completeness_join_mirror_mismatch")
    contract_payload = _json_value(row.get("contract_payload"))
    if not isinstance(contract_payload, Mapping):
        raise EvidenceIntegrityError("completeness_contract_payload_invalid")
    try:
        contract = _query_contract_from_mapping(contract_payload)
    except (TypeError, ValueError) as exc:
        raise EvidenceIntegrityError("completeness_contract_payload_invalid") from exc
    if (
        contract.query_contract_id != record.query_contract_ref
        or contract.contract_signature != row.get("stored_contract_signature")
        or query_contract_signature(contract) != contract.contract_signature
        or contract.analysis_contract_ref != row.get("analysis_contract_id")
    ):
        raise EvidenceIntegrityError("completeness_contract_mismatch")
    analysis_payload = _json_value(row.get("analysis_payload"))
    analysis_signature = str(row.get("stored_analysis_signature") or "")
    analysis = _analysis_contract_from_envelope(
        analysis_payload,
        stored_signature=analysis_signature,
        code="completeness_analysis_contract_mismatch",
    )
    if analysis.analysis_contract_id != contract.analysis_contract_ref:
        raise EvidenceIntegrityError("completeness_analysis_contract_mismatch")
    try:
        _validate_query_contract_analysis_semantics(analysis, contract)
    except EvidenceIntegrityError as exc:
        raise EvidenceIntegrityError(
            "completeness_analysis_contract_mismatch"
        ) from exc


def _validate_binding_join(
    record: CapabilityBindingRecord,
    row: Mapping[str, Any],
) -> None:
    run_ids = {
        str(row.get(name) or "")
        for name in ("authority_run_id", "analysis_run_id", "analysis_run_id_actual")
    }
    if len(run_ids) != 1 or "" in run_ids:
        raise EvidenceIntegrityError("capability_binding_run_membership_mismatch")
    if record.analysis_contract_ref != row.get("stored_analysis_contract_id"):
        raise EvidenceIntegrityError("capability_binding_analysis_contract_mismatch")
    analysis_payload = _json_value(row.get("analysis_payload"))
    analysis_signature = str(row.get("stored_analysis_signature") or "")
    analysis = _analysis_contract_from_envelope(
        analysis_payload,
        stored_signature=analysis_signature,
        code="capability_binding_analysis_contract_mismatch",
    )
    if analysis.analysis_contract_id != record.analysis_contract_ref:
        raise EvidenceIntegrityError("capability_binding_analysis_contract_mismatch")
    try:
        _validate_capability_binding_analysis_closure(
            analysis,
            record,
            None,
        )
    except EvidenceIntegrityError as exc:
        raise EvidenceIntegrityError(
            "capability_binding_analysis_contract_mismatch"
        ) from exc


def _field(row: Any, name: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(name)
    try:
        return getattr(row, name)
    except AttributeError as exc:
        raise EvidenceIntegrityError(f"runtime_authority_row_column_missing:{name}") from exc


def _row_mapping(row: Any, names: Sequence[str]) -> Mapping[str, Any]:
    if isinstance(row, Mapping):
        return row
    if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
        raise EvidenceIntegrityError("runtime_authority_row_invalid")
    if len(row) != len(names):
        raise EvidenceIntegrityError("runtime_authority_row_column_count_mismatch")
    return dict(zip(names, row))


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return canonical_thaw(value)


def _authority_mapping(value: Any, kind: str) -> Mapping[str, Any]:
    payload = _json_value(value)
    if not isinstance(payload, Mapping):
        raise EvidenceIntegrityError(f"{kind}_payload_invalid")
    return canonical_value(payload)
