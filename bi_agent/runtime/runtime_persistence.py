from __future__ import annotations

from dataclasses import fields, replace
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Sequence

from bi_agent.runtime.canonical_values import canonical_thaw
from bi_agent.runtime.clickhouse_revenue_rows import _query_contract_from_mapping
from bi_agent.runtime.analysis_contracts import (
    AnalysisContract,
    QueryContract,
    analysis_contract_from_dict,
    analysis_contract_signature,
    query_contract_semantic_body,
    query_contract_signature,
)
from bi_agent.runtime.dataset_catalog import (
    DatasetSnapshot,
    immutable_dataset_snapshot_projection,
)
from bi_agent.runtime.authoritative_query_chain import (
    AuthoritativeQueryChainError,
    validate_capability_binding_plan_semantics,
)
from bi_agent.runtime.contract_gaps import (
    canonical_source_ambiguity_source_ids,
    is_canonical_direct_analysis_source_ambiguity,
)
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)
from bi_agent.runtime.runtime_publication_index import (
    RUNTIME_PUBLICATION_INDEX_SCHEMA_VERSION,
    RUNTIME_PUBLICATION_RECORD_GROUPS,
)
from bi_agent.runtime.answer_package_artifact import (
    validate_answer_package_artifact_record,
)
from bi_agent.runtime.claim_provenance import (
    validate_context_manifest_record,
    validate_trusted_claim_provenance_record,
    validate_verified_claim_record,
)
from bi_agent.runtime.reuse_decision import (
    PHYSICAL_QUERY_REUSE_DECISION_SCHEMA_VERSION,
    physical_reuse_decision_cache_provenance_matches,
    validated_physical_query_reuse_decision_record,
)
from bi_agent.runtime.evidence_authority import (
    CapabilityBindingRecord,
    CompletenessRecord,
    EvidenceIntegrityError,
    QueryExecutionRecord,
    RowsRecord,
    RuntimeEvidenceResolver,
    SnapshotRecord,
    canonical_result_rows_hash_matches,
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


def _result_candidate_publication_authority_projection(
    candidate: Mapping[str, Any],
    publication_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind a signed reuse candidate to one reconstructed source authority graph."""
    from bi_agent.conversation.models import validate_result_reuse_candidate

    normalized = validate_result_reuse_candidate(candidate)
    if not isinstance(publication_payload, Mapping):
        _source_publication_mismatch("payload")

    analysis = publication_payload.get("analysis_contract")
    if not isinstance(analysis, Mapping):
        _source_publication_mismatch("analysis_missing")
    try:
        typed_analysis = analysis_contract_from_dict(
            {
                key: value
                for key, value in analysis.items()
                if key != "contract_signature"
            }
        )
    except (TypeError, ValueError):
        _source_publication_mismatch("analysis")
    analysis_signature = str(analysis.get("contract_signature") or "")
    if (
        typed_analysis.analysis_contract_id
        != normalized["analysis_contract_ref"]
        or analysis_signature != normalized["analysis_contract_signature"]
        or analysis_contract_signature(typed_analysis) != analysis_signature
        or typed_analysis.permission_scope
        != normalized["permission_scope"]
        or normalized["semantic_scope_signature"]
        != f"analysis-contract:sha256:{analysis_signature}"
    ):
        _source_publication_mismatch("analysis")

    query_contracts = _publication_records(
        publication_payload,
        "query_contracts",
        "query_contract_id",
    )
    query_contract_payload = query_contracts.get(normalized["query_contract_ref"])
    try:
        typed_query_contract = _query_contract_from_mapping(
            query_contract_payload or {}
        )
        _validate_query_contract_analysis_semantics(
            typed_analysis,
            typed_query_contract,
        )
    except (EvidenceIntegrityError, TypeError, ValueError):
        _source_publication_mismatch("query_contract")
    if (
        query_contract_payload is None
        or canonical_value(query_contract_payload)
        != canonical_value(typed_query_contract.to_dict())
        or typed_query_contract.query_contract_id
        != normalized["query_contract_ref"]
        or typed_query_contract.analysis_contract_ref
        != normalized["analysis_contract_ref"]
        or typed_query_contract.contract_signature
        != normalized["query_contract_signature"]
        or query_contract_signature(typed_query_contract)
        != typed_query_contract.contract_signature
        or typed_query_contract.permission_scope
        != normalized["permission_scope"]
        or typed_query_contract.dataset_snapshot_refs
        != tuple(normalized["source_snapshot_refs"])
    ):
        _source_publication_mismatch("query_contract")
    query_contract = typed_query_contract.to_dict()

    query_execution_records = _publication_records(
        publication_payload,
        "query_execution_records",
        "record_ref",
    )
    query_matches = tuple(
        record
        for record in query_execution_records.values()
        if str(record.get("result_ref") or "") == normalized["result_ref"]
    )
    if len(query_matches) != 1:
        _source_publication_mismatch("query_execution_missing")
    query_execution = query_matches[0]
    if (
        str(query_execution.get("record_ref") or "")
        != normalized["query_execution_record_ref"]
        or str(query_execution.get("record_digest") or "")
        != normalized["query_execution_record_digest"]
        or str(query_execution.get("execution_status") or "") != "succeeded"
        or str(query_execution.get("query_contract_ref") or "")
        != normalized["query_contract_ref"]
        or str(query_execution.get("contract_signature") or "")
        != normalized["query_contract_signature"]
        or str(query_execution.get("rows_ref") or "") != normalized["rows_ref"]
        or str(query_execution.get("rows_content_hash") or "")
        != normalized["rows_content_hash"]
        or str(query_execution.get("completeness_report_ref") or "")
        != normalized["completeness_report_ref"]
        or tuple(query_execution.get("source_snapshot_refs") or ())
        != tuple(normalized["source_snapshot_refs"])
        or tuple(query_execution.get("source_snapshot_record_refs") or ())
        != tuple(normalized["source_snapshot_record_refs"])
        or tuple(query_execution.get("source_snapshot_record_digests") or ())
        != tuple(normalized["source_snapshot_record_digests"])
        or canonical_value(query_execution.get("contract"))
        != canonical_value(query_contract)
        or canonical_value(query_execution.get("query_contract"))
        != canonical_value(query_contract)
    ):
        _source_publication_mismatch("query_execution")
    result_payload = query_execution.get("result_payload")
    if (
        not isinstance(result_payload, Mapping)
        or str(result_payload.get("result_ref") or "") != normalized["result_ref"]
        or str(result_payload.get("query_contract_ref") or "")
        != normalized["query_contract_ref"]
        or str(result_payload.get("rows_ref") or "") != normalized["rows_ref"]
        or str(result_payload.get("completeness_report_ref") or "")
        != normalized["completeness_report_ref"]
        or tuple(result_payload.get("source_snapshot_refs") or ())
        != tuple(normalized["source_snapshot_refs"])
    ):
        _source_publication_mismatch("query_result")

    rows_records = _publication_records(
        publication_payload,
        "rows_records",
        "record_ref",
    )
    rows_matches = tuple(
        record
        for record in rows_records.values()
        if str(record.get("rows_ref") or "") == normalized["rows_ref"]
    )
    if len(rows_matches) != 1:
        _source_publication_mismatch("rows_missing")
    rows = rows_matches[0]
    if (
        str(rows.get("record_ref") or "") != normalized["rows_record_ref"]
        or str(rows.get("record_digest") or "")
        != normalized["rows_record_digest"]
        or str(rows.get("rows_content_hash") or "")
        != normalized["rows_content_hash"]
        or str((rows.get("metadata_payload") or {}).get("rows_ref") or "")
        != normalized["rows_ref"]
        or str(
            (rows.get("metadata_payload") or {}).get("rows_content_hash") or ""
        )
        != normalized["rows_content_hash"]
        or rows.get("row_count") != query_execution.get("row_count")
    ):
        _source_publication_mismatch("rows")

    snapshot_records = _publication_records(
        publication_payload,
        "snapshot_records",
        "snapshot_ref",
    )
    matching_snapshots = []
    for index, snapshot_ref in enumerate(normalized["source_snapshot_refs"]):
        snapshot_record = snapshot_records.get(snapshot_ref)
        snapshot = (
            snapshot_record.get("snapshot")
            if isinstance(snapshot_record, Mapping)
            else None
        )
        if (
            not isinstance(snapshot, Mapping)
            or str(snapshot_record.get("record_ref") or "")
            != normalized["source_snapshot_record_refs"][index]
            or str(snapshot_record.get("record_digest") or "")
            != normalized["source_snapshot_record_digests"][index]
            or canonical_value(snapshot_record.get("payload"))
            != canonical_value(snapshot)
            or str(snapshot.get("snapshot_ref") or "") != snapshot_ref
            or str(snapshot.get("release_ref") or "")
            != normalized["source_release_refs"][index]
            or str(snapshot.get("authority_record_ref") or "")
            != normalized["source_release_authority_refs"][index]
            or str(snapshot.get("schema_fingerprint") or "")
            != normalized["source_schema_fingerprints"][index]
            or normalized["permission_scope"]
            not in tuple(str(item) for item in snapshot.get("permission_scopes") or ())
        ):
            _source_publication_mismatch("snapshot_release")
        matching_snapshots.append(snapshot_record)

    completeness_records = _publication_records(
        publication_payload,
        "completeness_records",
        "record_ref",
    )
    completeness_refs = tuple(normalized["completeness_record_refs"])
    completeness_digests = tuple(normalized["completeness_record_digests"])
    if (
        len(set(completeness_refs)) != len(completeness_refs)
        or completeness_refs != tuple(sorted(completeness_refs))
    ):
        _source_publication_mismatch("completeness_order")
    matching_completeness = tuple(
        completeness_records.get(record_ref) for record_ref in completeness_refs
    )
    if (
        any(record is None for record in matching_completeness)
        or any(
            str(record.get("result_ref") or "") != normalized["result_ref"]
            or str(record.get("query_contract_ref") or "")
            != normalized["query_contract_ref"]
            or str(record.get("report_ref") or "")
            != normalized["completeness_report_ref"]
            or str(record.get("report_digest") or "") != record_digest
            or str((record.get("report_payload") or {}).get("completeness_status") or "")
            != "complete"
            or str((record.get("report_payload") or {}).get("analysis_readiness") or "")
            != "ready"
            for record, record_digest in zip(
                matching_completeness,
                completeness_digests,
            )
        )
    ):
        _source_publication_mismatch("completeness")

    binding_records = _publication_records(
        publication_payload,
        "capability_binding_records",
        "record_ref",
    )
    binding_refs = tuple(normalized["binding_record_refs"])
    binding_digests = tuple(normalized["binding_record_digests"])
    if (
        len(set(binding_refs)) != len(binding_refs)
        or binding_refs != tuple(sorted(binding_refs))
    ):
        _source_publication_mismatch("binding_order")
    matching_bindings = tuple(
        binding_records.get(record_ref) for record_ref in binding_refs
    )
    if (
        any(record is None for record in matching_bindings)
        or any(
            str(record.get("binding_digest") or "") != binding_digest
            or str(record.get("status") or "") != "ready"
            or str(record.get("analysis_contract_ref") or "")
            != normalized["analysis_contract_ref"]
            or not _publication_binding_supports_candidate(
                record,
                normalized,
                query_execution=query_execution,
                rows=rows,
                completeness=matching_completeness,
            )
            for record, binding_digest in zip(
                matching_bindings,
                binding_digests,
            )
        )
    ):
        _source_publication_mismatch("binding")
    return canonical_value(
        {
            "candidate": normalized,
            "analysis_contract": analysis,
            "query_contract": query_contract,
            "query_execution_record": query_execution,
            "rows_record": rows,
            "snapshot_records": matching_snapshots,
            "completeness_records": matching_completeness,
            "binding_records": matching_bindings,
        }
    )


def validate_result_candidate_publication_authority(
    candidate: Mapping[str, Any],
    publication_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate and return the exact signed candidate from source authority."""
    projection = _result_candidate_publication_authority_projection(
        candidate,
        publication_payload,
    )
    return dict(projection["candidate"])


def result_candidate_publication_authority_projection(
    candidate: Mapping[str, Any],
    publication_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the validated source records needed for cache equivalence checks."""
    return _result_candidate_publication_authority_projection(
        candidate,
        publication_payload,
    )


def validate_result_candidate_publication_index(
    candidate: Mapping[str, Any],
    publication_index: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the compact PG index and selected-record membership exactly."""
    from bi_agent.conversation.models import validate_result_reuse_candidate

    normalized = validate_result_reuse_candidate(candidate)
    if (
        not isinstance(publication_index, Mapping)
        or set(publication_index)
        != {"schema_version", "analysis_contract_id", "ordered_refs"}
        or publication_index.get("schema_version")
        != RUNTIME_PUBLICATION_INDEX_SCHEMA_VERSION
        or str(publication_index.get("analysis_contract_id") or "")
        != normalized["analysis_contract_ref"]
    ):
        _source_publication_mismatch("index")
    raw_groups = publication_index.get("ordered_refs")
    if not isinstance(raw_groups, Mapping) or set(raw_groups) != set(
        RUNTIME_PUBLICATION_RECORD_GROUPS
    ):
        _source_publication_mismatch("index")
    ordered_refs: dict[str, list[str]] = {}
    for group in RUNTIME_PUBLICATION_RECORD_GROUPS:
        values = raw_groups.get(group)
        if (
            not isinstance(values, (list, tuple))
            or isinstance(values, (str, bytes))
            or any(not isinstance(value, str) or not value for value in values)
            or len(values) != len(set(values))
        ):
            _source_publication_mismatch("index")
        ordered_refs[group] = list(values)
    selected = {
        "query_contracts": (normalized["query_contract_ref"],),
        "query_execution_records": (
            normalized["query_execution_record_ref"],
        ),
        "rows_records": (normalized["rows_record_ref"],),
        "snapshot_records": tuple(normalized["source_snapshot_record_refs"]),
        "completeness_records": tuple(normalized["completeness_record_refs"]),
        "capability_binding_records": tuple(normalized["binding_record_refs"]),
    }
    if any(
        any(ref not in ordered_refs[group] for ref in refs)
        for group, refs in selected.items()
    ):
        _source_publication_mismatch("index_membership")
    return canonical_value(
        {
            "schema_version": RUNTIME_PUBLICATION_INDEX_SCHEMA_VERSION,
            "analysis_contract_id": normalized["analysis_contract_ref"],
            "ordered_refs": ordered_refs,
        }
    )


def _publication_records(
    publication_payload: Mapping[str, Any],
    group: str,
    identity_field: str,
) -> dict[str, Mapping[str, Any]]:
    raw_records = publication_payload.get(group)
    if (
        not isinstance(raw_records, (list, tuple))
        or isinstance(raw_records, (str, bytes))
        or any(not isinstance(record, Mapping) for record in raw_records)
    ):
        _source_publication_mismatch(f"{group}_shape")
    records: dict[str, Mapping[str, Any]] = {}
    for record in raw_records:
        identity = str(record.get(identity_field) or "")
        if not identity or identity in records:
            _source_publication_mismatch(f"{group}_identity")
        records[identity] = record
    return records


def _publication_binding_supports_candidate(
    binding: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    query_execution: Mapping[str, Any],
    rows: Mapping[str, Any],
    completeness: Sequence[Mapping[str, Any]],
) -> bool:
    expected_completeness = {
        (
            str(record.get("record_ref") or ""),
            str(record.get("report_digest") or ""),
        )
        for record in completeness
    }
    groups = (
        "",
        "validation_",
    )
    for prefix in groups:
        result_refs = tuple(
            str(ref) for ref in binding.get(f"{prefix}result_refs") or ()
        )
        if candidate["result_ref"] not in result_refs:
            continue
        index = result_refs.index(candidate["result_ref"])
        fields = (
            f"{prefix}query_execution_record_refs",
            f"{prefix}query_execution_record_digests",
            f"{prefix}rows_refs",
            f"{prefix}rows_metadata_record_refs",
            f"{prefix}rows_metadata_record_digests",
            f"{prefix}rows_content_hashes",
            f"{prefix}completeness_report_refs",
            f"{prefix}completeness_record_refs",
            f"{prefix}completeness_record_digests",
        )
        aligned = [tuple(binding.get(field) or ()) for field in fields]
        if any(index >= len(values) for values in aligned):
            continue
        actual = tuple(str(values[index]) for values in aligned)
        if actual[:7] != (
            str(query_execution.get("record_ref") or ""),
            str(query_execution.get("record_digest") or ""),
            str(rows.get("rows_ref") or ""),
            str(rows.get("record_ref") or ""),
            str(rows.get("record_digest") or ""),
            str(rows.get("rows_content_hash") or ""),
            str(query_execution.get("completeness_report_ref") or ""),
        ):
            continue
        if (actual[7], actual[8]) in expected_completeness:
            return True
    return False


def _source_publication_mismatch(component: str) -> None:
    raise EvidenceIntegrityError(
        f"result_candidate_source_publication_mismatch:{component}"
    )


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
    answer_package_artifacts: Sequence[Mapping[str, Any]] | None = None,
    result_candidate_resolver: Callable[..., Mapping[str, Any]] | None = None,
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
    for binding in capability_binding_records:
        if binding.analysis_contract_ref != analysis_ref:
            raise EvidenceIntegrityError("runtime_persistence_binding_analysis_contract_mismatch")
        _validate_capability_binding_analysis_closure(
            typed_analysis,
            binding,
            query_by_ref,
        )
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
    auxiliary_terminal_result_refs, _ = _validated_auxiliary_terminal_closure(
        analysis=typed_analysis,
        repair_attempts=repair_attempts,
        query_records=query_records,
        unbound_result_refs=unbound_result_refs,
        has_bound_results=bool(bound_result_refs),
    )
    unresolved_result_refs = (
        unbound_result_refs - auxiliary_terminal_result_refs
    )
    repair_signatures = {
        str(item.get("failed_signature") or "")
        for item in repair_attempts
        if isinstance(item, Mapping)
        and item.get("action") != "quarantine_auxiliary_results"
        and all(item.get(key) for key in ("attempt_ref", "failed_signature", "action", "reason"))
    }
    if any(
        query_records[result_ref].contract_signature not in repair_signatures
        for result_ref in unresolved_result_refs
    ):
        raise EvidenceIntegrityError("runtime_persistence_binding_chain_incomplete")
    _validate_analysis_target_metric_refs(typed_analysis)
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
    if (
        not has_verified_claims
        and provenance_by_ref
        and not _zero_claim_provenance_is_final_physical_reuse(
            provenance_by_ref,
            run_id=run_id,
        )
    ):
        raise EvidenceIntegrityError("runtime_persistence_zero_claim_provenance_invalid")
    _validate_physical_reuse_provenance(
        provenance_by_ref,
        run_id=run_id,
        analysis_contract_ref=analysis_ref,
        query_by_ref=query_by_ref,
        query_records=query_records,
        reports_by_result=reports_by_result,
        snapshots_by_ref=snapshots_by_ref,
        result_candidate_resolver=result_candidate_resolver,
    )

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
        _validate_claim_reuse_membership(payload, provenance)
        validate_verified_claim_record(
            payload,
            context_manifest=context,
            evidence_by_ref=evidence_by_ref,
            trusted_provenance=provenance,
        )
        _validate_verified_claim_contract_boundary(
            payload,
            analysis=typed_analysis,
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
    if not has_verified_claims and claim_links:
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

    normalized_artifacts: tuple[Mapping[str, Any], ...] = ()
    declared_artifact_refs = {
        str(ref)
        for provenance in provenance_by_ref.values()
        for ref in provenance.get("artifact_refs") or ()
        if str(ref)
    }
    artifact_records = tuple(answer_package_artifacts or ())
    if len(artifact_records) > 1:
        raise EvidenceIntegrityError(
            "runtime_persistence_answer_package_artifact_ambiguous"
        )
    if declared_artifact_refs and not artifact_records:
        raise EvidenceIntegrityError(
            "runtime_persistence_answer_package_artifact_missing"
        )
    if artifact_records:
        artifact_record = validate_answer_package_artifact_record(
            artifact_records[0],
            run_id=run_id,
        )
        expected_artifact_refs = [artifact_record["artifact_ref"]]
        if any(
            list(provenance.get("artifact_refs") or ())
            != expected_artifact_refs
            for provenance in provenance_by_ref.values()
        ):
            raise EvidenceIntegrityError(
                "runtime_persistence_answer_package_artifact_provenance_mismatch"
            )
        normalized_artifacts = (artifact_record,)

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
        "answer_package_artifacts": normalized_artifacts,
        "verified_claims": tuple(claims_by_ref.values()),
        "claim_links": tuple(normalized_links),
        "repair_attempts": tuple(normalized_repairs),
    }


def _zero_claim_provenance_is_final_physical_reuse(
    provenance_by_ref: Mapping[str, Mapping[str, Any]],
    *,
    run_id: str,
) -> bool:
    decision_refs: set[str] = set()
    for provenance in provenance_by_ref.values():
        raw_decisions = provenance.get("reuse_decisions") or ()
        if not raw_decisions:
            return False
        for raw in raw_decisions:
            try:
                decision = validated_physical_query_reuse_decision_record(raw)
            except (EvidenceIntegrityError, TypeError, ValueError):
                return False
            decision_ref = str(decision.get("decision_ref") or "")
            if (
                decision["run_id"] != run_id
                or not decision_ref
                or decision_ref in decision_refs
            ):
                return False
            decision_refs.add(decision_ref)
    return bool(decision_refs)


def _validate_claim_reuse_membership(
    claim: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> None:
    result_refs = {
        str(ref) for ref in claim.get("result_refs") or () if str(ref)
    }
    for raw in provenance.get("reuse_decisions") or ():
        if (
            not isinstance(raw, Mapping)
            or raw.get("schema_version")
            != PHYSICAL_QUERY_REUSE_DECISION_SCHEMA_VERSION
        ):
            continue
        decision = validated_physical_query_reuse_decision_record(raw)
        if str(decision.get("result_ref") or "") not in result_refs:
            raise EvidenceIntegrityError(
                "runtime_persistence_claim_reuse_result_mismatch"
            )


def _validate_physical_reuse_provenance(
    provenance_by_ref: Mapping[str, Mapping[str, Any]],
    *,
    run_id: str,
    analysis_contract_ref: str,
    query_by_ref: Mapping[str, QueryContract],
    query_records: Mapping[str, QueryExecutionRecord],
    reports_by_result: Mapping[str, CompletenessRecord],
    snapshots_by_ref: Mapping[str, SnapshotRecord],
    result_candidate_resolver: Callable[..., Mapping[str, Any]] | None,
) -> None:
    decision_refs: set[str] = set()
    for provenance in provenance_by_ref.values():
        for raw in provenance.get("reuse_decisions") or ():
            if (
                not isinstance(raw, Mapping)
                or raw.get("schema_version")
                != PHYSICAL_QUERY_REUSE_DECISION_SCHEMA_VERSION
            ):
                continue
            decision = validated_physical_query_reuse_decision_record(raw)
            if (
                decision["run_id"] != run_id
                or decision["analysis_contract_ref"] != analysis_contract_ref
            ):
                raise EvidenceIntegrityError(
                    "runtime_persistence_reuse_decision_owner_mismatch"
                )
            contract = query_by_ref.get(decision["query_contract_ref"])
            query = query_records.get(decision["result_ref"])
            if (
                contract is None
                or query is None
                or contract.contract_signature
                != decision["query_contract_signature"]
                or query.query_contract_ref != decision["query_contract_ref"]
                or query.contract_signature
                != decision["query_contract_signature"]
                or query.record_ref != decision["query_execution_record_ref"]
            ):
                raise EvidenceIntegrityError(
                    "runtime_persistence_reuse_decision_query_mismatch"
                )

            report = reports_by_result.get(decision["result_ref"])
            if (
                report is None
                or report.query_contract_ref != decision["query_contract_ref"]
                or tuple(decision["completeness_record_refs"])
                != (report.record_ref,)
            ):
                raise EvidenceIntegrityError(
                    "runtime_persistence_reuse_decision_completeness_mismatch"
                )

            result_payload = canonical_thaw(query.result_payload)
            provider_stats = (
                result_payload.get("provider_stats") or {}
                if isinstance(result_payload, Mapping)
                else {}
            )
            if not physical_reuse_decision_cache_provenance_matches(
                decision,
                provider_stats,
            ):
                raise EvidenceIntegrityError(
                    "runtime_persistence_reuse_decision_cache_mismatch"
                )
            if decision.get("decision") == "reuse":
                _validate_physical_reuse_source_authority(
                    decision,
                    current_contract=contract,
                    current_query=query,
                    current_report=report,
                    current_snapshots=snapshots_by_ref,
                    result_candidate_resolver=result_candidate_resolver,
                )

            decision_ref = decision["decision_ref"]
            if decision_ref in decision_refs:
                raise EvidenceIntegrityError(
                    "runtime_persistence_reuse_decision_duplicate"
                )
            decision_refs.add(decision_ref)


def _validate_physical_reuse_source_authority(
    decision: Mapping[str, Any],
    *,
    current_contract: QueryContract,
    current_query: QueryExecutionRecord,
    current_report: CompletenessRecord,
    current_snapshots: Mapping[str, SnapshotRecord],
    result_candidate_resolver: Callable[..., Mapping[str, Any]] | None,
) -> None:
    if not callable(result_candidate_resolver):
        raise EvidenceIntegrityError(
            "runtime_persistence_reuse_decision_source_authority_unavailable"
        )
    try:
        authority = result_candidate_resolver(
            result_ref=str(decision.get("source_ref") or ""),
            topic_id=str(decision.get("topic_id") or ""),
        )
    except EvidenceIntegrityError as exc:
        if str(exc).startswith("result_candidate_source_publication_mismatch"):
            raise
        raise EvidenceIntegrityError(
            "runtime_persistence_reuse_decision_source_authority_missing"
        ) from exc
    except Exception as exc:
        raise EvidenceIntegrityError(
            "runtime_persistence_reuse_decision_source_authority_missing"
        ) from exc
    if not isinstance(authority, Mapping):
        raise EvidenceIntegrityError(
            "runtime_persistence_reuse_decision_source_authority_missing"
        )
    result_ref_record = authority.get("result_ref_record") or {}
    candidate = (
        result_ref_record.get("payload")
        if isinstance(result_ref_record, Mapping)
        else None
    )
    source_contract = authority.get("analysis_contract") or {}
    source_cache_authority = authority.get("cache_authority") or {}
    expected = {
        "source_run_id": decision.get("source_run_id"),
        "result_ref": decision.get("source_ref"),
        "analysis_contract_ref": decision.get(
            "source_analysis_contract_ref"
        ),
        "query_contract_ref": decision.get("source_query_contract_ref"),
        "query_execution_record_ref": decision.get(
            "source_query_execution_record_ref"
        ),
        "completeness_record_refs": list(
            decision.get("source_completeness_record_refs") or ()
        ),
        "candidate_signature": decision.get("candidate_signature"),
    }
    if (
        not isinstance(candidate, Mapping)
        or not isinstance(source_contract, Mapping)
        or not isinstance(source_cache_authority, Mapping)
        or str(authority.get("run_status") or "") != "completed"
        or str(authority.get("run_topic_id") or "")
        != str(decision.get("topic_id") or "")
        or str(source_contract.get("analysis_contract_id") or "")
        != str(decision.get("source_analysis_contract_ref") or "")
        or any(
            canonical_value(candidate.get(field))
            != canonical_value(value)
            for field, value in expected.items()
        )
    ):
        raise EvidenceIntegrityError(
            "runtime_persistence_reuse_decision_source_authority_mismatch"
        )
    _validate_physical_reuse_cache_equivalence(
        source_cache_authority,
        current_contract=current_contract,
        current_query=current_query,
        current_report=current_report,
        current_snapshots=current_snapshots,
    )


def _validate_physical_reuse_cache_equivalence(
    source: Mapping[str, Any],
    *,
    current_contract: QueryContract,
    current_query: QueryExecutionRecord,
    current_report: CompletenessRecord,
    current_snapshots: Mapping[str, SnapshotRecord],
) -> None:
    candidate = source.get("candidate")
    source_contract = source.get("query_contract")
    source_query = source.get("query_execution_record")
    source_rows = source.get("rows_record")
    source_snapshots = source.get("snapshot_records")
    source_completeness = source.get("completeness_records")
    if (
        not isinstance(candidate, Mapping)
        or not isinstance(source_contract, Mapping)
        or not isinstance(source_query, Mapping)
        or not isinstance(source_rows, Mapping)
        or not isinstance(source_snapshots, (list, tuple))
        or not isinstance(source_completeness, (list, tuple))
    ):
        raise EvidenceIntegrityError(
            "runtime_persistence_reuse_decision_cache_equivalence_mismatch:source"
        )
    try:
        typed_source_contract = _query_contract_from_mapping(source_contract)
    except (TypeError, ValueError) as exc:
        raise EvidenceIntegrityError(
            "runtime_persistence_reuse_decision_cache_equivalence_mismatch:query_contract"
        ) from exc
    if (
        query_contract_signature(current_contract)
        != query_contract_signature(typed_source_contract)
        or canonical_value(query_contract_semantic_body(current_contract))
        != canonical_value(query_contract_semantic_body(typed_source_contract))
    ):
        raise EvidenceIntegrityError(
            "runtime_persistence_reuse_decision_cache_equivalence_mismatch:query_contract"
        )
    if (
        current_query.execution_status != "succeeded"
        or str(source_query.get("execution_status") or "") != "succeeded"
        or current_query.rows_content_hash
        != str(source_rows.get("rows_content_hash") or "")
        or current_query.row_count != source_rows.get("row_count")
    ):
        raise EvidenceIntegrityError(
            "runtime_persistence_reuse_decision_cache_equivalence_mismatch:result"
        )
    report_payload = canonical_value(current_report.report_payload)
    if (
        report_payload.get("completeness_status") != "complete"
        or report_payload.get("analysis_readiness") != "ready"
        or any(
            not isinstance(record, Mapping)
            or (record.get("report_payload") or {}).get("completeness_status")
            != "complete"
            or (record.get("report_payload") or {}).get("analysis_readiness")
            != "ready"
            for record in source_completeness
        )
    ):
        raise EvidenceIntegrityError(
            "runtime_persistence_reuse_decision_cache_equivalence_mismatch:completeness"
        )
    current_snapshot_records = tuple(
        current_snapshots.get(ref) for ref in current_query.source_snapshot_refs
    )
    if any(record is None for record in current_snapshot_records):
        raise EvidenceIntegrityError(
            "runtime_persistence_reuse_decision_cache_equivalence_mismatch:snapshot_release"
        )
    current_snapshot_axes = tuple(
        (
            record.snapshot.snapshot_ref,
            record.snapshot.release_ref,
            record.snapshot.authority_record_ref,
            record.snapshot.schema_fingerprint,
        )
        for record in current_snapshot_records
    )
    source_snapshot_axes = tuple(
        zip(
            candidate.get("source_snapshot_refs") or (),
            candidate.get("source_release_refs") or (),
            candidate.get("source_release_authority_refs") or (),
            candidate.get("source_schema_fingerprints") or (),
        )
    )
    if current_snapshot_axes != source_snapshot_axes:
        raise EvidenceIntegrityError(
            "runtime_persistence_reuse_decision_cache_equivalence_mismatch:snapshot_release"
        )
    try:
        current_snapshot_authority = tuple(
            immutable_dataset_snapshot_projection(record.snapshot)
            for record in current_snapshot_records
        )
        source_snapshot_authority = tuple(
            immutable_dataset_snapshot_projection(record.get("snapshot"))
            for record in source_snapshots
            if isinstance(record, Mapping)
        )
    except (TypeError, ValueError) as exc:
        raise EvidenceIntegrityError(
            "runtime_persistence_reuse_decision_cache_equivalence_mismatch:snapshot_release"
        ) from exc
    if (
        len(source_snapshot_authority) != len(source_snapshots)
        or current_snapshot_authority != source_snapshot_authority
    ):
        raise EvidenceIntegrityError(
            "runtime_persistence_reuse_decision_cache_equivalence_mismatch:snapshot_release"
        )


def _unique_by(records: Sequence[Any], field: str, code: str) -> dict[str, Any]:
    result = {}
    for record in records:
        ref = str(getattr(record, field))
        if ref in result:
            raise EvidenceIntegrityError(code)
        result[ref] = record
    return result


def _validated_auxiliary_terminal_closure(
    *,
    analysis: AnalysisContract,
    repair_attempts: Sequence[Mapping[str, Any]],
    query_records: Mapping[str, QueryExecutionRecord],
    unbound_result_refs: set[str],
    has_bound_results: bool,
) -> tuple[set[str], set[str]]:
    terminal_results: set[str] = set()
    terminal_claim_intents: set[str] = set()
    terminal_attempt_refs: set[str] = set()
    analysis_capabilities = set(analysis.capability_requirements)
    analysis_claim_intents = set(analysis.claim_intents)
    try:
        registry = RuntimeContractRegistry.from_path(
            CANONICAL_RUNTIME_BINDINGS_PATH
        )
    except (OSError, TypeError, ValueError) as exc:
        raise EvidenceIntegrityError(
            "runtime_persistence_auxiliary_terminal_invalid:registry"
        ) from exc

    for raw in repair_attempts:
        if (
            not isinstance(raw, Mapping)
            or raw.get("action") != "quarantine_auxiliary_results"
        ):
            continue
        payload = canonical_value(raw)
        required_strings = (
            "attempt_ref",
            "failed_signature",
            "reason",
            "capability_id",
            "analysis_role",
            "failure_stage",
            "publication_authority",
        )
        if any(
            not isinstance(payload.get(field), str)
            or not payload[field].strip()
            for field in required_strings
        ):
            raise EvidenceIntegrityError(
                "runtime_persistence_auxiliary_terminal_invalid:shape"
            )
        attempt_ref = payload["attempt_ref"]
        if attempt_ref in terminal_attempt_refs:
            raise EvidenceIntegrityError(
                "runtime_persistence_auxiliary_terminal_invalid:duplicate"
            )
        terminal_attempt_refs.add(attempt_ref)
        if (
            payload["analysis_role"] != "auxiliary"
            or payload["publication_authority"] != "none"
            or not has_bound_results
        ):
            raise EvidenceIntegrityError(
                "runtime_persistence_auxiliary_terminal_invalid:authority"
            )

        capability_id = payload["capability_id"]
        if capability_id not in analysis_capabilities:
            raise EvidenceIntegrityError(
                "runtime_persistence_auxiliary_terminal_invalid:capability"
            )
        try:
            supported_claim_types = set(
                registry.capability_inputs(capability_id).get(
                    "supported_claim_types", ()
                )
            )
        except KeyError as exc:
            raise EvidenceIntegrityError(
                "runtime_persistence_auxiliary_terminal_invalid:capability"
            ) from exc

        raw_claim_types = payload.get("affected_claim_types")
        raw_query_refs = payload.get("query_contract_refs")
        raw_result_refs = payload.get("result_refs")
        if any(
            not isinstance(value, (list, tuple)) or not value
            for value in (
                raw_claim_types,
                raw_query_refs,
                raw_result_refs,
            )
        ):
            raise EvidenceIntegrityError(
                "runtime_persistence_auxiliary_terminal_invalid:shape"
            )
        claim_types = tuple(raw_claim_types)
        query_refs = tuple(raw_query_refs)
        result_refs = tuple(raw_result_refs)
        if any(
            not isinstance(value, str) or not value
            for value in (*claim_types, *query_refs, *result_refs)
        ):
            raise EvidenceIntegrityError(
                "runtime_persistence_auxiliary_terminal_invalid:shape"
            )
        if (
            len(query_refs) != len(result_refs)
            or len(set(result_refs)) != len(result_refs)
            or len(set(query_refs)) != len(query_refs)
            or not set(claim_types).issubset(analysis_claim_intents)
            or not set(claim_types).issubset(supported_claim_types)
        ):
            raise EvidenceIntegrityError(
                "runtime_persistence_auxiliary_terminal_invalid:scope"
            )
        for query_ref, result_ref in zip(query_refs, result_refs):
            query = query_records.get(result_ref)
            if (
                result_ref not in unbound_result_refs
                or result_ref in terminal_results
                or query is None
                or query.query_contract_ref != query_ref
            ):
                raise EvidenceIntegrityError(
                    "runtime_persistence_auxiliary_terminal_invalid:result"
                )
            terminal_results.add(result_ref)
        terminal_claim_intents.update(claim_types)
    return terminal_results, terminal_claim_intents


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


def _validate_analysis_target_metric_refs(analysis: AnalysisContract) -> None:
    try:
        registry = RuntimeContractRegistry.from_path(
            CANONICAL_RUNTIME_BINDINGS_PATH
        )
    except (OSError, TypeError, ValueError) as exc:
        raise EvidenceIntegrityError(
            "runtime_persistence_analysis_target_metric_contract_invalid"
        ) from exc

    target_refs = tuple(analysis.target_metric_refs)
    target_ref_membership = set(target_refs)
    bound_refs_by_metric: dict[str, list[str]] = {}
    for binding in analysis.metric_bindings:
        if binding.contract_ref:
            bound_refs_by_metric.setdefault(binding.metric_id, []).append(
                binding.contract_ref
            )
    ambiguity_refs_by_metric: dict[str, list[tuple[tuple[str, ...], bool]]] = {}
    for gap in analysis.contract_gaps:
        if not is_canonical_direct_analysis_source_ambiguity(
            gap,
            tuple(gap.affected_capabilities),
            registry=registry,
        ):
            continue
        diagnostic = gap.diagnostic_context
        if diagnostic.get("item_kind") != "metric":
            continue
        metric_id = str(diagnostic.get("item_id") or "")
        source_ids = canonical_source_ambiguity_source_ids(
            gap,
            registry=registry,
        )
        try:
            sources = registry.metric_sources(metric_id)
            selected_refs = tuple(
                str(sources[source_id].get("contract_ref") or "")
                for source_id in source_ids
            )
        except (KeyError, TypeError, ValueError):
            continue
        if selected_refs and all(selected_refs):
            affected = tuple(gap.affected_capabilities)
            actual_scope = is_canonical_direct_analysis_source_ambiguity(
                gap,
                affected,
                registry=registry,
                expected_capability_requirements=(
                    analysis.capability_requirements
                ),
            )
            if affected != ("analysis_contract",) and not actual_scope:
                continue
            ambiguity_refs_by_metric.setdefault(metric_id, []).append(
                (tuple(dict.fromkeys(selected_refs)), actual_scope and bool(affected[1:]))
            )

    requested_metric_ids = analysis.scope.get("requested_metric_ids")
    metric_order = tuple(dict.fromkeys((
        *(
            tuple(requested_metric_ids)
            if isinstance(requested_metric_ids, (list, tuple))
            and all(
                isinstance(metric_id, str) and metric_id
                for metric_id in requested_metric_ids
            )
            else ()
        ),
        *bound_refs_by_metric,
        *ambiguity_refs_by_metric,
    )))
    expected_refs: list[str] = []
    queryless_review = not analysis.metric_bindings
    if queryless_review and target_refs:
        target_owner_metric_ids: list[str] = []
        for target_ref in target_refs:
            owners = registry.metric_ids_for_contract_ref(target_ref)
            if len(owners) != 1:
                raise EvidenceIntegrityError(
                    "runtime_persistence_analysis_target_metric_mismatch"
                )
            if owners[0] not in target_owner_metric_ids:
                target_owner_metric_ids.append(owners[0])
        if metric_order and any(
            metric_id not in metric_order
            for metric_id in target_owner_metric_ids
        ):
            raise EvidenceIntegrityError(
                "runtime_persistence_analysis_target_metric_mismatch"
            )
        if not metric_order:
            metric_order = tuple(target_owner_metric_ids)

    def extend_distinct(refs: Sequence[str]) -> None:
        for ref in refs:
            if ref and ref not in expected_refs:
                expected_refs.append(ref)

    for metric_id in metric_order:
        bound_refs = tuple(bound_refs_by_metric.get(metric_id, ()))
        if any(ref in target_ref_membership for ref in bound_refs):
            extend_distinct(bound_refs)
        ambiguity_refs = ambiguity_refs_by_metric.get(
            metric_id,
            (),
        )
        for selected_refs, mandatory in ambiguity_refs:
            if mandatory:
                extend_distinct(selected_refs)
            else:
                extend_distinct(tuple(
                    ref for ref in selected_refs if ref in target_ref_membership
                ))
        if queryless_review and not bound_refs and not ambiguity_refs:
            try:
                reviewed_refs = {
                    str(source.get("contract_ref") or "")
                    for source in registry.metric_sources(metric_id).values()
                }
            except (KeyError, TypeError, ValueError):
                reviewed_refs = set()
            extend_distinct(
                tuple(
                    ref
                    for ref in target_refs
                    if ref in reviewed_refs and ref
                )
            )

    if target_refs != tuple(expected_refs):
        raise EvidenceIntegrityError(
            "runtime_persistence_analysis_target_metric_mismatch"
        )


def _validate_verified_claim_contract_boundary(
    payload: Mapping[str, Any],
    *,
    analysis: AnalysisContract,
    evidence_by_ref: Mapping[str, Mapping[str, Any]],
    bindings_by_ref: Mapping[str, CapabilityBindingRecord],
    registry: RuntimeContractRegistry,
) -> None:
    claim_type = str(payload.get("claim_type") or "")
    if (
        claim_type not in analysis.claim_intents
    ):
        raise EvidenceIntegrityError(
            "runtime_persistence_verified_claim_intent_mismatch"
        )
    linked_evidence = tuple(
        evidence_by_ref[evidence_ref]
        for evidence_ref in payload.get("evidence_refs") or ()
        if evidence_ref in evidence_by_ref
    )
    linked_binding_refs = tuple(
        dict.fromkeys(
            str(evidence["binding_record_ref"])
            for evidence in linked_evidence
        )
    )
    linked_bindings = tuple(
        bindings_by_ref[binding_ref]
        for binding_ref in linked_binding_refs
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
            hash_matches = canonical_result_rows_hash_matches(
                rows,
                record.unique_key_fields,
                record.rows_content_hash,
            )
        except EvidenceIntegrityError as exc:
            raise EvidenceIntegrityError(f"rows_payload_unique_key_invalid:{exc}") from exc
        if not hash_matches:
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
