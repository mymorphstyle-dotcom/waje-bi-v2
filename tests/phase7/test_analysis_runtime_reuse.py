from __future__ import annotations

from copy import deepcopy
from collections.abc import Mapping
from dataclasses import replace
from datetime import date, timedelta
import json
from types import MappingProxyType, SimpleNamespace
import unittest

from bi_agent.conversation.store import InMemoryConversationStore
from bi_agent.runtime.analysis_contracts import (
    analysis_contract_signature,
    query_contract_signature,
)
from bi_agent.runtime.analysis_runtime import (
    AnalysisRuntime,
    AnalysisRuntimeRequest,
    AnswerPackageBuildContext,
    _claim_physical_reuse_decisions,
)
from bi_agent.runtime.clickhouse_runtime import ClickHouseQueryResult
from bi_agent.runtime.claim_provenance import (
    build_trusted_claim_provenance_record,
    build_verified_claim_record,
)
from bi_agent.runtime.evidence_authority import (
    EvidenceIntegrityError,
    RuntimeEvidenceAuthority,
    canonical_digest,
    canonical_value,
)
from bi_agent.runtime.reuse_decision import (
    build_physical_query_reuse_decision_record,
    validate_physical_query_reuse_decision_record,
)
from bi_agent.runtime.query_executor import ClickHouseQueryExecutor
from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry
from tests.phase4.test_analysis_contract_compiler import (
    canonical_release_catalog,
    snapshot,
)
from tests.phase7.artifact_test_support import bind_answer_package_artifact


class _CountingRowsRuntime:
    def __init__(self) -> None:
        self.calls = 0

    def aggregate(self, sql, query_id, **kwargs):
        self.calls += 1
        rows = [
            {
                "window_id": "target_day",
                "window_role": "target",
                "observation_key": "2026-06-02",
                "paid_amount": 120.0,
            },
            {
                "window_id": "previous_day",
                "window_role": "baseline",
                "observation_key": "2026-06-01",
                "paid_amount": 100.0,
            },
        ]
        rolling_start = date.fromisoformat("2026-05-26")
        for offset in range(7):
            day = rolling_start + timedelta(days=offset)
            rows.append(
                {
                    "window_id": "rolling_7_day_baseline",
                    "window_role": "baseline",
                    "observation_key": day.isoformat(),
                    "paid_amount": 95.0 + offset,
                }
            )
        return ClickHouseQueryResult(
            ok=True,
            query_id=query_id,
            rows=tuple(rows),
            execution_attempt_ref=str(kwargs.get("execution_attempt_ref") or ""),
        )

    bounded_context = aggregate


class _MultiQueryRowsRuntime(_CountingRowsRuntime):
    def aggregate(self, sql, query_id, **kwargs):
        result = super().aggregate(sql, query_id, **kwargs)
        if ":2:" not in query_id:
            return result
        rows = []
        for row in result.rows:
            enriched = dict(row)
            enriched.update(
                {
                    "calendar_week": enriched["observation_key"][:7],
                    "weekday": "monday",
                    "month_phase": "month_start",
                }
            )
            rows.append(enriched)
        return ClickHouseQueryResult(
            ok=result.ok,
            query_id=result.query_id,
            rows=tuple(rows),
            execution_attempt_ref=result.execution_attempt_ref,
        )

    bounded_context = aggregate


class _CompositeReleaseResolver:
    def __init__(self, *resolvers) -> None:
        self._records = {
            resolver.record.release_ref: resolver.record
            for resolver in resolvers
        }

    def resolve_dataset_release(self, release_ref):
        if release_ref not in self._records:
            raise KeyError(release_ref)
        return self._records[release_ref]


def _proposal(baselines=("previous_day", "rolling_7_day_baseline")):
    return {
        "question_families": ["custom_baseline_comparison"],
        "target_metrics": ["paid_amount"],
        "claim_intents": ["comparative_change"],
        "scope": {"type": "full_sample"},
        "target_semantic": "yesterday",
        "baselines": list(baselines),
    }


def _multi_query_proposal():
    return {
        **_proposal(),
        "question_families": [
            "custom_baseline_comparison",
            "recurring_pattern_analysis",
        ],
        "claim_intents": [
            "comparative_change",
            "recurring_pattern_existence",
        ],
    }


def _runtime_fixture():
    registry = RuntimeContractRegistry.from_path(
        "contracts/runtime/clickhouse-analysis-bindings.yaml"
    )
    authority = RuntimeEvidenceAuthority(runtime_registry=registry)
    catalog, release_resolver, signed = canonical_release_catalog(
        snapshot("paid_order_success", "paid_success", "2026-07-04")
    )
    provider = _CountingRowsRuntime()
    store = InMemoryConversationStore()
    store.create_thread("thread-reuse", owner_id="analyst-1")
    topic = store.create_topic("thread-reuse", title="付费金额基线分析")
    runtime = AnalysisRuntime(
        catalog=catalog,
        registry=registry,
        executor=ClickHouseQueryExecutor(
            provider,
            evidence_resolver=authority,
            rows_loader=authority.rows_loader,
            evidence_writer=authority._runtime_writer(),
            release_resolver=release_resolver,
        ),
        release_resolver=release_resolver,
        evidence_authority=authority,
        store=store,
    )
    return runtime, provider, store, topic.topic_id, signed


def _source_request(run_id, topic_id, baselines=("previous_day", "rolling_7_day_baseline")):
    return AnalysisRuntimeRequest.create(
        run_id=run_id,
        topic_id=topic_id,
        proposal=_proposal(baselines),
        accepted_graph=("compare_periods",),
        as_of="2026-06-03T12:00:00+01:00",
        permission_scope="analyst",
    )


def _publish_source(
    runtime,
    store,
    topic_id,
    result,
    candidate,
    *,
    proposal=None,
    accepted_graph=("compare_periods",),
):
    from bi_agent.conversation.clarification_authority import (
        build_execution_material,
        build_material_authority,
    )

    analysis_signature = analysis_contract_signature(result.analysis_contract)
    source_run_id = result.analysis_contract.analysis_contract_id.split(":")[1]
    source_request_payload = {
        "context_manifest": {
            "snapshot_version": "2026H1",
            "contract_versions": {"runtime": "contracts-v1"},
        }
    }
    store.upsert_run(
        source_run_id,
        thread_id="thread-reuse",
        topic_id=topic_id,
        status="running_workflow",
        request=source_request_payload,
    )
    bundle = runtime.build_persistence_bundle(
        result,
        answer_package={"status": "draft", "sections": []},
        request={
            "run_id": source_run_id,
            "thread_id": "thread-reuse",
            "topic_id": topic_id,
            "permission_context": {"role": "analyst"},
            **source_request_payload,
        },
        artifact_path="artifacts/phase7/source-reuse/answer_package.json",
    )
    store.save_analysis_runtime_records(
        run_id=source_run_id,
        **bundle,
    )
    contract = result.analysis_contract
    metric_ids = tuple(
        dict.fromkeys(binding.metric_id for binding in contract.metric_bindings)
    )
    families = tuple(contract.question_families)
    scope_payload = canonical_value(contract.scope)
    scope = str(scope_payload.get("type") or "full_sample")
    proposal = dict(proposal or _proposal())
    execution_material = build_execution_material(
        proposal=proposal,
        accepted_graph=accepted_graph,
        as_of=contract.as_of,
        permission_scope=contract.permission_scope,
        run_mode="production",
        runtime_contract_version=runtime.registry.contract_version,
        runtime_registry_digest=runtime.registry.source_payload_digest,
        analysis_contract=contract,
        query_contracts=result.query_contracts,
        capability_execution_plans=result.capability_plans,
    )
    material_authority = build_material_authority(
        source_run_id=source_run_id,
        thread_id="thread-reuse",
        topic_id=topic_id,
        original_intent={
            "question_family": families[0],
            "question_families": list(families),
            "primary_question_family": families[0],
            "secondary_question_families": list(families[1:]),
            "target_metric": metric_ids[0],
            "requested_components": [],
            "requested_dimensions": [],
            "baseline_candidates": list(proposal["baselines"]),
            "context_sources": [],
            "claim_intents": list(contract.claim_intents),
            "scope": scope,
        },
        material_slots={
            "target_metrics": list(metric_ids),
            "requested_components": [],
            "requested_dimensions": [],
            "baselines": list(proposal["baselines"]),
            "context_sources": [],
            "claim_intents": list(contract.claim_intents),
            "diagnostic_tags": [],
            "scope": scope,
        },
        runtime_material=execution_material,
    )
    store.finalize_completed_material_authority(
        run_id=source_run_id,
        thread_id="thread-reuse",
        topic_id=topic_id,
        request=source_request_payload,
        material_authority=material_authority,
    )
    candidates = (candidate,) if isinstance(candidate, Mapping) else tuple(candidate)
    for item in candidates:
        store.add_result_ref(
            topic_id,
            result_ref=item["result_ref"],
            snapshot_id="2026H1",
            contract_version="contracts-v1",
            permission_scope="analyst",
            semantic_scope=f"analysis-contract:sha256:{analysis_signature}",
            payload=item,
        )


def _candidate(runtime, result, signed_snapshots, *, result_index=0):
    source_result = result.query_results[result_index]
    query = runtime.evidence_resolver.resolve_query_execution(source_result.result_ref)
    rows = runtime.evidence_resolver.resolve_rows(query.rows_ref)
    completeness = runtime.evidence_authority.resolve_latest_completeness(
        query.completeness_report_ref
    )
    binding = next(
        item
        for item in result.persistence_records["capability_binding_records"]
        if source_result.result_ref in (*item.result_refs, *item.validation_result_refs)
    )
    snapshots_by_ref = {item.snapshot_ref: item for item in signed_snapshots}
    analysis_signature = analysis_contract_signature(result.analysis_contract)
    payload = {
        "schema_version": "result-reuse-candidate.v1",
        "source_run_id": result.analysis_contract.analysis_contract_id.split(":")[1],
        "result_ref": source_result.result_ref,
        "query_contract_ref": query.query_contract_ref,
        "query_contract_signature": query.contract_signature,
        "query_execution_record_ref": query.record_ref,
        "query_execution_record_digest": query.record_digest,
        "analysis_contract_ref": result.analysis_contract.analysis_contract_id,
        "analysis_contract_signature": analysis_signature,
        "runtime_snapshot_id": "2026H1",
        "runtime_contract_version": "contracts-v1",
        "source_snapshot_refs": list(query.source_snapshot_refs),
        "source_snapshot_record_refs": list(query.source_snapshot_record_refs),
        "source_snapshot_record_digests": list(query.source_snapshot_record_digests),
        "source_release_refs": [
            snapshots_by_ref[ref].release_ref for ref in query.source_snapshot_refs
        ],
        "source_release_authority_refs": [
            snapshots_by_ref[ref].authority_record_ref
            for ref in query.source_snapshot_refs
        ],
        "source_schema_fingerprints": [
            snapshots_by_ref[ref].schema_fingerprint
            for ref in query.source_snapshot_refs
        ],
        "permission_scope": query.contract.permission_scope,
        "semantic_scope_signature": (
            f"analysis-contract:sha256:{analysis_signature}"
        ),
        "rows_ref": rows.rows_ref,
        "rows_record_ref": rows.record_ref,
        "rows_record_digest": rows.record_digest,
        "rows_content_hash": rows.rows_content_hash,
        "completeness_report_ref": completeness.report_ref,
        "completeness_record_refs": [completeness.record_ref],
        "completeness_record_digests": [completeness.report_digest],
        "binding_record_refs": [binding.record_ref],
        "binding_record_digests": [binding.binding_digest],
    }
    return {**payload, "candidate_signature": canonical_digest(payload)}


def _resign(candidate, **changes):
    unsigned = {**candidate, **changes}
    unsigned.pop("candidate_signature", None)
    return {**unsigned, "candidate_signature": canonical_digest(unsigned)}


def _physical_claim_package(result, text="复用后的新结论"):
    binding = result.persistence_records["capability_binding_records"][0]
    evidence_ref = f"evidence:{binding.record_ref}"
    verified_claim = {
        "text": text,
        "claim_type": "comparative_change",
        "claim_strength": "observed",
        "evidence_refs": [evidence_ref],
    }
    return {
        "status": "complete",
        "admin_audit": {"verified_claims": [verified_claim]},
        "sections": [
            {
                "section_id": "summary",
                "payload": {"claims": [verified_claim]},
            },
            {
                "section_id": "evidence",
                "payload": {
                    "evidence": [
                        {
                            "evidence_ref": evidence_ref,
                            "binding_manifest_ref": binding.record_ref,
                        }
                    ]
                },
            },
        ],
    }


def _resigned_physical_decision(record, **changes):
    payload = {**record, **changes}
    return build_physical_query_reuse_decision_record(
        run_id=payload["run_id"],
        topic_id=payload["topic_id"],
        analysis_contract_ref=payload["analysis_contract_ref"],
        source_run_id=payload["source_run_id"],
        source_analysis_contract_ref=payload["source_analysis_contract_ref"],
        source_ref=payload["source_ref"],
        source_query_contract_ref=payload["source_query_contract_ref"],
        source_query_execution_record_ref=(
            payload["source_query_execution_record_ref"]
        ),
        source_completeness_record_refs=(
            payload["source_completeness_record_refs"]
        ),
        result_ref=payload["result_ref"],
        query_contract_ref=payload["query_contract_ref"],
        query_contract_signature=payload["query_contract_signature"],
        query_execution_record_ref=payload["query_execution_record_ref"],
        completeness_record_refs=payload["completeness_record_refs"],
        candidate_signature=payload["candidate_signature"],
        decision=payload["decision"],
        reason=payload["reason"],
    )


def _bundle_with_physical_decision(bundle, decision):
    previous = bundle["trusted_provenance_records"][0]
    provenance = build_trusted_claim_provenance_record(
        run_id=previous["run_id"],
        artifact_refs=previous["artifact_refs"],
        memory_refs=previous["memory_refs"],
        reuse_decisions=(decision,),
    )
    updated = {
        **bundle,
        "trusted_provenance_records": (provenance,),
    }
    if not bundle["verified_claims"]:
        return updated
    contexts = {
        item["manifest_id"]: item for item in bundle["context_manifests"]
    }
    evidence = {
        item["evidence_ref"]: item for item in bundle["evidence_manifests"]
    }
    claims = tuple(
        build_verified_claim_record(
            claim,
            run_id=claim["run_id"],
            context_manifest=contexts[claim["context_manifest_ref"]],
            evidence_by_ref=evidence,
            trusted_provenance=provenance,
        )
        for claim in bundle["verified_claims"]
    )
    return {
        **updated,
        "verified_claims": claims,
        "claim_links": tuple(
            {
                "claim_ref": claim["claim_ref"],
                "evidence_ref": evidence_ref,
                "context_manifest_ref": claim["context_manifest_ref"],
            }
            for claim in claims
            for evidence_ref in claim["evidence_refs"]
        ),
    }


def _bundle_with_resigned_query_provider_stats(bundle, **provider_changes):
    from tests.phase4.test_authoritative_query_chain import _resign_binding

    query = bundle["query_execution_records"][0]
    result_payload = dict(query.result_payload)
    result_payload["provider_stats"] = {
        **dict(result_payload.get("provider_stats") or {}),
        **provider_changes,
    }
    record_payload = dict(query.record_payload)
    record_payload["result"] = canonical_value(result_payload)
    record_payload = canonical_value(record_payload)
    digest = canonical_digest(record_payload)
    changed_query = replace(
        query,
        record_ref=f"query-execution:{query.result_ref}:{digest}",
        record_digest=digest,
        record_payload=record_payload,
        result_payload=canonical_value(result_payload),
    )

    changed_bindings = []
    binding_refs = {}
    for binding in bundle["capability_binding_records"]:
        if query.result_ref in binding.result_refs:
            prefix = ""
        elif query.result_ref in binding.validation_result_refs:
            prefix = "validation_"
        else:
            changed_bindings.append(binding)
            continue
        refs_field = f"{prefix}query_execution_record_refs"
        digests_field = f"{prefix}query_execution_record_digests"
        refs = tuple(
            changed_query.record_ref if ref == query.record_ref else ref
            for ref in getattr(binding, refs_field)
        )
        digests = tuple(
            changed_query.record_digest if ref == query.record_ref else item
            for ref, item in zip(
                getattr(binding, refs_field),
                getattr(binding, digests_field),
            )
        )
        binding_payload = dict(binding.binding_payload)
        binding_payload[refs_field] = refs
        binding_payload[digests_field] = digests
        changed = _resign_binding(
            binding,
            **{
                refs_field: refs,
                digests_field: digests,
                "binding_payload": binding_payload,
            },
        )
        binding_refs[binding.record_ref] = changed.record_ref
        changed_bindings.append(changed)

    evidence = tuple(
        {
            **manifest,
            "binding_record_ref": binding_refs.get(
                manifest["binding_record_ref"],
                manifest["binding_record_ref"],
            ),
        }
        for manifest in bundle["evidence_manifests"]
    )
    decision = _resigned_physical_decision(
        bundle["trusted_provenance_records"][0]["reuse_decisions"][0],
        query_execution_record_ref=changed_query.record_ref,
    )
    return _bundle_with_physical_decision(
        {
            **bundle,
            "query_execution_records": (changed_query,),
            "capability_binding_records": tuple(changed_bindings),
            "evidence_manifests": evidence,
        },
        decision,
    )


def _bundle_with_current_completeness_state(
    bundle,
    *,
    completeness_status,
    analysis_readiness,
):
    from tests.phase4.test_authoritative_query_chain import _resign_binding

    report = bundle["completeness_records"][0]
    report_payload = dict(report.report_payload)
    report_payload.update(
        {
            "completeness_status": completeness_status,
            "analysis_readiness": analysis_readiness,
        }
    )
    report_payload = canonical_value(report_payload)
    report_digest = canonical_digest(report_payload)
    changed_report = replace(
        report,
        record_ref=(
            f"completeness-record:{report.report_ref}:{report_digest}"
        ),
        report_digest=report_digest,
        report_payload=report_payload,
    )
    binding_ref_changes = {}
    changed_bindings = []
    for binding in bundle["capability_binding_records"]:
        if report.result_ref in binding.result_refs:
            prefix = ""
        elif report.result_ref in binding.validation_result_refs:
            prefix = "validation_"
        else:
            changed_bindings.append(binding)
            continue
        refs_field = f"{prefix}completeness_record_refs"
        digests_field = f"{prefix}completeness_record_digests"
        refs = tuple(
            changed_report.record_ref if ref == report.record_ref else ref
            for ref in getattr(binding, refs_field)
        )
        digests = tuple(
            changed_report.report_digest if ref == report.record_ref else digest
            for ref, digest in zip(
                getattr(binding, refs_field),
                getattr(binding, digests_field),
            )
        )
        binding_payload = dict(binding.binding_payload)
        binding_payload[refs_field] = refs
        binding_payload[digests_field] = digests
        changed = _resign_binding(
            binding,
            **{
                refs_field: refs,
                digests_field: digests,
                "binding_payload": binding_payload,
            },
        )
        binding_ref_changes[binding.record_ref] = changed.record_ref
        changed_bindings.append(changed)
    evidence = tuple(
        {
            **manifest,
            "binding_record_ref": binding_ref_changes.get(
                manifest["binding_record_ref"],
                manifest["binding_record_ref"],
            ),
            "completeness_record_refs": [
                changed_report.record_ref
                if ref == report.record_ref
                else ref
                for ref in manifest["completeness_record_refs"]
            ],
        }
        for manifest in bundle["evidence_manifests"]
    )
    decision = _resigned_physical_decision(
        bundle["trusted_provenance_records"][0]["reuse_decisions"][0],
        completeness_record_refs=(changed_report.record_ref,),
    )
    return _bundle_with_physical_decision(
        {
            **bundle,
            "completeness_records": (changed_report,),
            "capability_binding_records": tuple(changed_bindings),
            "evidence_manifests": evidence,
        },
        decision,
    )


def _bundle_with_current_snapshot_axis(bundle, **snapshot_changes):
    from bi_agent.runtime.evidence_authority import snapshot_authority_record
    from tests.phase4.test_authoritative_query_chain import _resign_binding

    snapshot_record = bundle["snapshot_records"][0]
    changed_snapshot_record = snapshot_authority_record(
        replace(snapshot_record.snapshot, **snapshot_changes)
    )
    query = bundle["query_execution_records"][0]
    snapshot_refs = tuple(query.source_snapshot_refs)
    record_refs = tuple(
        changed_snapshot_record.record_ref
        if ref == snapshot_record.snapshot_ref
        else record_ref
        for ref, record_ref in zip(
            snapshot_refs,
            query.source_snapshot_record_refs,
        )
    )
    record_digests = tuple(
        changed_snapshot_record.record_digest
        if ref == snapshot_record.snapshot_ref
        else record_digest
        for ref, record_digest in zip(
            snapshot_refs,
            query.source_snapshot_record_digests,
        )
    )
    record_payload = dict(query.record_payload)
    record_payload["source_snapshot_record_refs"] = record_refs
    record_payload["source_snapshot_record_digests"] = record_digests
    record_payload = canonical_value(record_payload)
    record_digest = canonical_digest(record_payload)
    changed_query = replace(
        query,
        record_ref=f"query-execution:{query.result_ref}:{record_digest}",
        record_digest=record_digest,
        record_payload=record_payload,
        source_snapshot_record_refs=record_refs,
        source_snapshot_record_digests=record_digests,
    )
    binding_ref_changes = {}
    changed_bindings = []
    for binding in bundle["capability_binding_records"]:
        if query.result_ref in binding.result_refs:
            prefix = ""
        elif query.result_ref in binding.validation_result_refs:
            prefix = "validation_"
        else:
            changed_bindings.append(binding)
            continue
        refs_field = f"{prefix}query_execution_record_refs"
        digests_field = f"{prefix}query_execution_record_digests"
        refs = tuple(
            changed_query.record_ref if ref == query.record_ref else ref
            for ref in getattr(binding, refs_field)
        )
        digests = tuple(
            changed_query.record_digest if ref == query.record_ref else digest
            for ref, digest in zip(
                getattr(binding, refs_field),
                getattr(binding, digests_field),
            )
        )
        binding_payload = dict(binding.binding_payload)
        binding_payload[refs_field] = refs
        binding_payload[digests_field] = digests
        changed = _resign_binding(
            binding,
            **{
                refs_field: refs,
                digests_field: digests,
                "binding_payload": binding_payload,
            },
        )
        binding_ref_changes[binding.record_ref] = changed.record_ref
        changed_bindings.append(changed)
    evidence = tuple(
        {
            **manifest,
            "binding_record_ref": binding_ref_changes.get(
                manifest["binding_record_ref"],
                manifest["binding_record_ref"],
            ),
        }
        for manifest in bundle["evidence_manifests"]
    )
    decision = _resigned_physical_decision(
        bundle["trusted_provenance_records"][0]["reuse_decisions"][0],
        query_execution_record_ref=changed_query.record_ref,
    )
    return _bundle_with_physical_decision(
        {
            **bundle,
            "query_execution_records": (changed_query,),
            "snapshot_records": (changed_snapshot_record,),
            "capability_binding_records": tuple(changed_bindings),
            "evidence_manifests": evidence,
        },
        decision,
    )


def _physical_reuse_bundle_fixture(
    *,
    current_run_id,
    answer_package,
    publication_mode="complete",
):
    runtime, current, request = _physical_reuse_result_fixture(
        current_run_id=current_run_id,
    )
    answer_package = {
        **dict(answer_package),
        "run_id": current_run_id,
    }
    bundle = runtime.build_persistence_bundle(
        current,
        answer_package=answer_package,
        request=request,
        artifact_path=(
            f"artifacts/phase7/{current_run_id}/answer_package.json"
        ),
        publication_mode=publication_mode,
    )
    bind_answer_package_artifact(
        bundle,
        run_id=current_run_id,
        answer_package=answer_package,
    )
    return runtime, current, request, bundle


def _physical_reuse_result_fixture(*, current_run_id):
    runtime, _, store, topic_id, signed = _runtime_fixture()
    source_run_id = f"{current_run_id}-source"
    source = runtime.execute(_source_request(source_run_id, topic_id))
    candidate = _candidate(runtime, source, signed)
    _publish_source(runtime, store, topic_id, source, candidate)
    current = runtime.execute(
        AnalysisRuntimeRequest.create(
            run_id=current_run_id,
            topic_id=topic_id,
            proposal=_proposal(),
            accepted_graph=("compare_periods",),
            as_of="2026-06-03T12:00:00+01:00",
            permission_scope="analyst",
            reuse_candidates=(candidate,),
        )
    )
    request = {
        "run_id": current_run_id,
        "thread_id": "thread-reuse",
        "topic_id": topic_id,
        "permission_context": {"role": "analyst"},
        "context_manifest": {
            "snapshot_version": "2026H1",
            "contract_versions": {"runtime": "contracts-v1"},
        },
    }
    return runtime, current, request


def _pg_candidate_authority_row(runtime, decision):
    from bi_agent.runtime.runtime_publication_index import (
        runtime_publication_index,
    )

    authority = runtime.store.resolve_result_candidate_authority(
        result_ref=decision["source_ref"],
        topic_id=decision["topic_id"],
    )
    record = authority["result_ref_record"]
    publication = runtime.store.analysis_runtime_records[
        authority["source_run_id"]
    ]
    completion_event = next(
        event
        for event in runtime.store.audit_events
        if event["event_type"] == "completed_material_authority_recorded"
        and event["run_id"] == authority["source_run_id"]
    )
    return {
        "topic_id": record["topic_id"],
        "result_ref": record["result_ref"],
        "snapshot_id": record["snapshot_id"],
        "contract_version": record["contract_version"],
        "permission_scope": record["permission_scope"],
        "semantic_scope": record["semantic_scope"],
        "result_ref_payload": record["payload"],
        "source_run_id": authority["source_run_id"],
        "run_thread_id": authority["run_thread_id"],
        "run_topic_id": authority["run_topic_id"],
        "run_status": authority["run_status"],
        "source_run_request": authority["source_run_request"],
        "analysis_contract": authority["analysis_contract"],
        "stored_analysis_contract_signature": authority[
            "stored_analysis_contract_signature"
        ],
        "source_publication_payload": runtime_publication_index(
            publication["payload"]
        ),
        "source_publication_digest": publication["digest"],
        "authority_record_payload": completion_event["payload"],
        "authority_record_ref": completion_event["ref"],
        "authority_event_run_id": completion_event["run_id"],
        "authority_event_thread_id": completion_event["thread_id"],
        "authority_event_topic_id": completion_event["topic_id"],
    }


class _NormalizedPgCandidateConnection:
    def __init__(self, runtime, decision):
        from tests.phase7.test_conversation_persistence import FakeConnection

        self._delegate = FakeConnection()
        self.root_row = _pg_candidate_authority_row(runtime, decision)
        self.source_run_id = decision["source_run_id"]
        self.publication_bundle = deepcopy(
            runtime.store.analysis_runtime_records[self.source_run_id]["payload"]
        )
        candidate = self.root_row["result_ref_payload"]
        resolver = runtime.evidence_resolver
        self.query = resolver.resolve_query_execution_record(
            candidate["query_execution_record_ref"]
        )
        self.rows = resolver.resolve_rows_record(candidate["rows_record_ref"])
        self.snapshots = {
            ref: resolver.resolve_snapshot(ref)
            for ref in candidate["source_snapshot_refs"]
        }
        self.completeness = {
            ref: resolver.resolve_completeness(ref)
            for ref in candidate["completeness_record_refs"]
        }
        self.bindings = {
            ref: resolver.resolve_capability_binding(ref)
            for ref in candidate["binding_record_refs"]
        }

    @property
    def statements(self):
        return self._delegate.statements

    @property
    def commits(self):
        return self._delegate.commits

    @property
    def rollbacks(self):
        return self._delegate.rollbacks

    def execute(self, statement, params=None):
        from bi_agent.runtime.runtime_persistence import authority_record_payload
        from bi_agent.runtime.runtime_publication_index import (
            RUNTIME_PUBLICATION_RECORD_GROUPS,
            runtime_publication_record_ref,
        )
        from tests.phase7.test_analysis_runtime_persistence import (
            _binding_resolver_row,
            _completeness_resolver_row,
            _query_resolver_row,
            _rows_resolver_row,
        )
        from tests.phase7.test_conversation_persistence import FakeCursor

        params = params or {}
        self._delegate.statements.append((statement, params))
        if "/* result_candidate_authority */" in statement:
            return FakeCursor([deepcopy(self.root_row)])
        if "/* result_candidate_publication_inventory */" in statement:
            wrapped_kinds = {
                "query_execution_records": "query_execution",
                "rows_records": "rows",
                "snapshot_records": "snapshot",
                "completeness_records": "completeness",
                "capability_binding_records": "capability_binding",
            }
            inventory = []
            for group in RUNTIME_PUBLICATION_RECORD_GROUPS:
                for record in self.publication_bundle[group]:
                    payload = deepcopy(record)
                    if group in wrapped_kinds:
                        payload = {
                            "kind": wrapped_kinds[group],
                            "record": payload,
                        }
                    inventory.append(
                        {
                            "record_group": group,
                            "record_ref": runtime_publication_record_ref(
                                group,
                                record,
                            ),
                            "owner_run_ids": [self.source_run_id],
                            "payload": payload,
                        }
                    )
            same_run_extra = getattr(
                self,
                "same_run_extra_inventory",
                None,
            )
            if (
                same_run_extra is not None
                and "JOIN waje_runtime.query_repair_attempts record\n"
                "      ON record.run_id = requested.run_id" in statement
            ):
                inventory.append(deepcopy(same_run_extra))
            return FakeCursor(inventory)
        if "/* completed_material_authority */" in statement:
            return FakeCursor(
                [
                    {
                        "analysis_contract_id": self.root_row[
                            "analysis_contract"
                        ]["analysis_contract_id"],
                        "analysis_run_id": self.source_run_id,
                        "stored_contract_signature": self.root_row[
                            "stored_analysis_contract_signature"
                        ],
                        "contract_payload": self.root_row[
                            "analysis_contract"
                        ],
                        "run_status": self.root_row["run_status"],
                        "run_thread_id": self.root_row["run_thread_id"],
                        "run_topic_id": self.root_row["run_topic_id"],
                        "run_request": self.root_row["source_run_request"],
                        "authority_record_payload": self.root_row[
                            "authority_record_payload"
                        ],
                        "authority_record_ref": self.root_row[
                            "authority_record_ref"
                        ],
                        "authority_event_run_id": self.root_row[
                            "authority_event_run_id"
                        ],
                        "authority_event_thread_id": self.root_row[
                            "authority_event_thread_id"
                        ],
                        "authority_event_topic_id": self.root_row[
                            "authority_event_topic_id"
                        ],
                    }
                ]
            )
        if "FROM waje_runtime.query_execution_authority q" in statement:
            row = _query_resolver_row(
                self.query,
                run_id=self.source_run_id,
            )
            row.update(
                {
                    "analysis_payload": json.dumps(
                        self.root_row["analysis_contract"]
                    ),
                    "stored_analysis_signature": self.root_row[
                        "stored_analysis_contract_signature"
                    ],
                    "thread_id": self.root_row["run_thread_id"],
                    "topic_id": self.root_row["run_topic_id"],
                }
            )
            return FakeCursor([row])
        if "FROM waje_runtime.rows_metadata_authority r" in statement:
            return FakeCursor(
                [_rows_resolver_row(self.rows, run_id=self.source_run_id)]
            )
        if "FROM waje_runtime.snapshot_authority s" in statement:
            record = self.snapshots.get(str(params.get("ref") or ""))
            if record is None:
                return FakeCursor([])
            return FakeCursor(
                [
                    {
                        "record_ref": record.record_ref,
                        "record_digest": record.record_digest,
                        "snapshot_ref": record.snapshot_ref,
                        "payload": json.dumps(
                            authority_record_payload("snapshot", record)
                        ),
                    }
                ]
            )
        if "FROM waje_runtime.query_completeness_reports c" in statement:
            record = self.completeness.get(str(params.get("ref") or ""))
            if record is None:
                return FakeCursor([])
            row = _completeness_resolver_row(
                record,
                self.query,
                run_id=self.source_run_id,
            )
            row.update(
                {
                    "analysis_payload": json.dumps(
                        self.root_row["analysis_contract"]
                    ),
                    "stored_analysis_signature": self.root_row[
                        "stored_analysis_contract_signature"
                    ],
                }
            )
            return FakeCursor([row])
        if "FROM waje_runtime.capability_binding_authority b" in statement:
            record = self.bindings.get(str(params.get("ref") or ""))
            if record is None:
                return FakeCursor([])
            row = _binding_resolver_row(
                record,
                run_id=self.source_run_id,
            )
            row.update(
                {
                    "analysis_payload": json.dumps(
                        self.root_row["analysis_contract"]
                    ),
                    "stored_analysis_signature": self.root_row[
                        "stored_analysis_contract_signature"
                    ],
                }
            )
            return FakeCursor([row])
        self._delegate.statements.pop()
        return self._delegate.execute(statement, params)

    def commit(self):
        return self._delegate.commit()

    def rollback(self):
        return self._delegate.rollback()


class AnalysisRuntimeReuseTest(unittest.TestCase):
    def test_claim_reuse_lineage_only_keeps_claim_result_refs(self):
        decisions = (
            {"decision_ref": "reuse:a", "result_ref": "result:a"},
            {"decision_ref": "reuse:b", "result_ref": "result:b"},
        )
        evidence = {
            "evidence:a": {"result_refs": ["result:a"]},
            "evidence:b": {"result_refs": ["result:b"]},
        }

        selected = _claim_physical_reuse_decisions(
            {"evidence_refs": ["evidence:a"]},
            evidence_by_ref=evidence,
            authoritative_reuse_decisions=decisions,
        )

        self.assertEqual(selected, (decisions[0],))

    def test_claim_scoped_physical_decisions_have_one_global_provenance_owner(self):
        runtime, _, store, topic_id, signed = _runtime_fixture()
        provider = _MultiQueryRowsRuntime()
        runtime.executor.runtime = provider
        proposal = _multi_query_proposal()
        accepted_graph = ("compare_periods", "pattern_scan")
        source = runtime.execute(
            AnalysisRuntimeRequest.create(
                run_id="run-multi-claim-source",
                topic_id=topic_id,
                proposal=proposal,
                accepted_graph=accepted_graph,
                as_of="2026-06-03T12:00:00+01:00",
                permission_scope="analyst",
            )
        )
        candidates = tuple(
            _candidate(runtime, source, signed, result_index=index)
            for index in range(len(source.query_results))
        )
        _publish_source(
            runtime,
            store,
            topic_id,
            source,
            candidates,
            proposal=proposal,
            accepted_graph=accepted_graph,
        )
        current = runtime.execute(
            AnalysisRuntimeRequest.create(
                run_id="run-multi-claim-current",
                topic_id=topic_id,
                proposal=proposal,
                accepted_graph=accepted_graph,
                as_of="2026-06-03T12:00:00+01:00",
                permission_scope="analyst",
                reuse_candidates=candidates,
            )
        )
        completeness_by_result = {
            record.result_ref: record
            for record in current.persistence_records["completeness_records"]
        }
        current = replace(
            current,
            reuse_decisions=tuple(
                _resigned_physical_decision(
                    decision,
                    completeness_record_refs=(
                        completeness_by_result[decision["result_ref"]].record_ref,
                    ),
                )
                for decision in current.reuse_decisions
            ),
        )
        bindings = tuple(current.persistence_records["capability_binding_records"])
        evidence = tuple(
            {
                "evidence_ref": f"evidence:{binding.record_ref}",
                "binding_manifest_ref": binding.record_ref,
            }
            for binding in bindings
        )
        verified_claims = [
            {
                "text": f"{binding.capability_id} 形成可验证结论。",
                "claim_type": binding.supported_claim_types[0],
                "claim_strength": "observed",
                "evidence_refs": [item["evidence_ref"]],
            }
            for binding, item in zip(bindings, evidence)
        ]
        answer_package = {
            "status": "complete",
            "admin_audit": {"verified_claims": verified_claims},
            "sections": [
                {
                    "section_id": "summary",
                    "payload": {"claims": verified_claims},
                },
                {
                    "section_id": "evidence",
                    "payload": {"evidence": list(evidence)},
                },
            ],
        }
        request = {
            "run_id": "run-multi-claim-current",
            "thread_id": "thread-reuse",
            "topic_id": topic_id,
            "permission_context": {"role": "analyst"},
            "context_manifest": {
                "snapshot_version": "2026H1",
                "contract_versions": {"runtime": "contracts-v1"},
            },
        }
        answer_package = {
            **answer_package,
            "run_id": request["run_id"],
        }
        bundle = runtime.build_persistence_bundle(
            current,
            answer_package=answer_package,
            request=request,
            artifact_path=(
                "artifacts/phase7/multi-claim-authority/answer_package.json"
            ),
        )
        bind_answer_package_artifact(
            bundle,
            run_id=request["run_id"],
            answer_package=answer_package,
        )

        self.assertEqual(len(current.reuse_decisions), 2)
        self.assertEqual(len(bundle["verified_claims"]), 2)
        self.assertEqual(
            store.save_analysis_runtime_records(run_id=request["run_id"], **bundle),
            "published",
        )
        persisted_decision_refs = [
            decision["decision_ref"]
            for provenance in bundle["trusted_provenance_records"]
            for decision in provenance["reuse_decisions"]
        ]
        self.assertCountEqual(
            persisted_decision_refs,
            [decision["decision_ref"] for decision in current.reuse_decisions],
        )
        self.assertTrue(
            all(
                len(claim["reuse_decisions"]) == 1
                for claim in bundle["verified_claims"]
            )
        )

    def test_exact_authority_candidate_materializes_current_run_without_provider_call(self):
        runtime, provider, store, topic_id, signed = _runtime_fixture()
        source = runtime.execute(_source_request("run-source", topic_id))
        candidate = _candidate(runtime, source, signed)
        _publish_source(runtime, store, topic_id, source, candidate)

        current = runtime.execute(
            AnalysisRuntimeRequest.create(
                run_id="run-current",
                topic_id=topic_id,
                proposal=_proposal(),
                accepted_graph=("compare_periods",),
                as_of="2026-06-03T12:00:00+01:00",
                permission_scope="analyst",
                reuse_candidates=(candidate,),
            )
        )

        self.assertEqual(provider.calls, 1)
        self.assertEqual(current.status, "ready")
        self.assertEqual(current.reuse_decisions[0]["decision"], "reuse")
        self.assertEqual(
            current.reuse_decisions[0]["source_ref"],
            source.query_results[0].result_ref,
        )
        self.assertNotEqual(
            current.query_results[0].result_ref,
            source.query_results[0].result_ref,
        )
        self.assertNotEqual(
            current.bound_capability_inputs["compare_periods"].binding_manifest_ref,
            source.bound_capability_inputs["compare_periods"].binding_manifest_ref,
        )
        decision = dict(current.reuse_decisions[0])
        self.assertEqual(
            decision["schema_version"],
            "physical-query-reuse-decision.v1",
        )
        self.assertEqual(decision["run_id"], "run-current")
        self.assertEqual(decision["topic_id"], topic_id)
        self.assertEqual(
            decision["analysis_contract_ref"],
            current.analysis_contract.analysis_contract_id,
        )
        self.assertEqual(
            decision["query_contract_signature"],
            current.query_contracts[0].contract_signature,
        )
        self.assertEqual(
            decision["source_run_id"],
            "run-source",
        )
        self.assertEqual(
            decision["source_query_execution_record_ref"],
            candidate["query_execution_record_ref"],
        )
        self.assertEqual(
            decision["source_completeness_record_refs"],
            candidate["completeness_record_refs"],
        )
        self.assertTrue(decision["query_execution_record_ref"])
        self.assertTrue(decision["completeness_record_refs"])
        validate_physical_query_reuse_decision_record(decision)
        tampered = {**decision, "reason": "same_query_probably_reusable"}
        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "physical_reuse_decision_reuse_reason_invalid",
        ):
            validate_physical_query_reuse_decision_record(tampered)

    def test_candidate_source_run_cannot_alias_current_run(self):
        runtime, _, store, topic_id, signed = _runtime_fixture()
        source = runtime.execute(_source_request("run-self-source", topic_id))
        candidate = _candidate(runtime, source, signed)
        _publish_source(runtime, store, topic_id, source, candidate)
        current_run_id = "run-self-current"
        self_candidate = _resign(candidate, source_run_id=current_run_id)
        source_authority = store.resolve_result_candidate_authority(
            result_ref=candidate["result_ref"],
            topic_id=topic_id,
        )
        store.resolve_result_candidate_authority = lambda **_: {
            **source_authority,
            "source_run_id": current_run_id,
        }
        request = _source_request(current_run_id, topic_id)
        snapshots = {
            item.snapshot_ref: item for item in runtime.catalog.snapshots()
        }

        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "reuse_candidate_source_run_alias",
        ):
            runtime._validate_reuse_candidate(
                self_candidate,
                request=request,
                contract=source.query_contracts[0],
                snapshots=snapshots,
            )

    def test_physical_decision_requires_distinct_source_run_for_every_outcome(self):
        base = {
            "run_id": "run-decision-current",
            "topic_id": "topic-decision-current",
            "analysis_contract_ref": "analysis:run-decision-current:1",
            "source_analysis_contract_ref": "analysis:run-decision-source:1",
            "source_ref": "result:decision-source",
            "source_query_contract_ref": "query:decision-source",
            "source_query_execution_record_ref": "query-execution:decision-source",
            "source_completeness_record_refs": (
                "completeness-record:decision-source",
            ),
            "result_ref": "result:decision-current",
            "query_contract_ref": "query:decision-current",
            "query_contract_signature": "a" * 64,
            "query_execution_record_ref": "query-execution:decision-current",
            "completeness_record_refs": (
                "completeness-record:decision-current",
            ),
            "candidate_signature": "b" * 64,
            "decision": "rerun",
            "reason": "candidate_authority_rejected",
        }
        for case, source_run_id, expected in (
            ("missing", "", "physical_reuse_decision_source_run_missing"),
            (
                "current_alias",
                base["run_id"],
                "physical_reuse_decision_source_run_alias",
            ),
        ):
            with self.subTest(case=case):
                with self.assertRaisesRegex(EvidenceIntegrityError, expected):
                    build_physical_query_reuse_decision_record(
                        **base,
                        source_run_id=source_run_id,
                    )

    def test_baseline_priority_change_reuses_same_canonical_physical_query_only(self):
        runtime, provider, store, topic_id, signed = _runtime_fixture()
        source = runtime.execute(
            _source_request(
                "run-baseline-source",
                topic_id,
                ("previous_day", "rolling_7_day_baseline"),
            )
        )
        candidate = _candidate(runtime, source, signed)
        _publish_source(runtime, store, topic_id, source, candidate)

        current = runtime.execute(
            AnalysisRuntimeRequest.create(
                run_id="run-baseline-current",
                topic_id=topic_id,
                proposal=_proposal(
                    ("rolling_7_day_baseline", "previous_day")
                ),
                accepted_graph=("compare_periods",),
                as_of="2026-06-03T12:00:00+01:00",
                permission_scope="analyst",
                reuse_candidates=(candidate,),
            )
        )

        self.assertEqual(provider.calls, 1)
        self.assertEqual(
            current.query_contracts[0].contract_signature,
            source.query_contracts[0].contract_signature,
        )
        self.assertNotEqual(
            analysis_contract_signature(current.analysis_contract),
            analysis_contract_signature(source.analysis_contract),
        )
        self.assertEqual(current.reuse_decisions[0]["decision"], "reuse")
        self.assertNotEqual(
            current.analysis_contract.analysis_contract_id,
            source.analysis_contract.analysis_contract_id,
        )
        self.assertNotEqual(
            current.query_results[0].result_ref,
            source.query_results[0].result_ref,
        )

        def claim_package(result, text):
            binding = result.persistence_records[
                "capability_binding_records"
            ][0]
            evidence_ref = f"evidence:{binding.record_ref}"
            verified_claim = {
                "text": text,
                "claim_type": "comparative_change",
                "claim_strength": "observed",
                "evidence_refs": [evidence_ref],
            }
            return {
                "status": "complete",
                "admin_audit": {"verified_claims": [verified_claim]},
                "sections": [
                    {
                        "section_id": "summary",
                        "payload": {"claims": [verified_claim]},
                    },
                    {
                        "section_id": "evidence",
                        "payload": {
                            "evidence": [
                                {
                                    "evidence_ref": evidence_ref,
                                    "binding_manifest_ref": binding.record_ref,
                                }
                            ]
                        },
                    },
                ],
            }

        source_bundle = runtime.build_persistence_bundle(
            source,
            answer_package=claim_package(source, "源分析结论"),
            request={
                "run_id": "run-baseline-source",
                "thread_id": "thread-reuse",
                "topic_id": topic_id,
                "permission_context": {"role": "analyst"},
            },
            artifact_path="artifacts/phase7/reuse-source/answer_package.json",
        )
        current_bundle = runtime.build_persistence_bundle(
            current,
            answer_package=claim_package(current, "追问后的新结论"),
            request={
                "run_id": "run-baseline-current",
                "thread_id": "thread-reuse",
                "topic_id": topic_id,
                "permission_context": {"role": "analyst"},
                "reuse_decisions": [
                    {
                        "source_ref": candidate["result_ref"],
                        "decision": "candidate",
                    }
                ],
            },
            artifact_path="artifacts/phase7/reuse-current/answer_package.json",
        )
        self.assertNotEqual(
            source_bundle["verified_claims"][0]["claim_ref"],
            current_bundle["verified_claims"][0]["claim_ref"],
        )
        self.assertEqual(
            current_bundle["trusted_provenance_records"][0]["reuse_decisions"],
            [dict(current.reuse_decisions[0])],
        )

    def test_zero_claim_run_keeps_final_physical_reuse_decision_provenance(self):
        _, current, request, bundle = _physical_reuse_bundle_fixture(
            current_run_id="run-zero-claim-reuse",
            answer_package={"status": "complete", "sections": []},
        )

        self.assertEqual(bundle["context_manifests"], ())
        self.assertEqual(bundle["verified_claims"], ())
        self.assertEqual(bundle["claim_links"], ())
        self.assertEqual(len(bundle["trusted_provenance_records"]), 1)
        provenance = bundle["trusted_provenance_records"][0]
        self.assertEqual(provenance["run_id"], request["run_id"])
        self.assertEqual(
            provenance["reuse_decisions"],
            [dict(current.reuse_decisions[0])],
        )

    def test_waiting_zero_claim_keeps_final_physical_reuse_decision_provenance(self):
        _, current, request, bundle = _physical_reuse_bundle_fixture(
            current_run_id="run-filtered-claim-reuse",
            answer_package={
                "status": "waiting_for_clarification",
                "sections": [],
            },
            publication_mode="waiting_for_clarification",
        )

        self.assertEqual(bundle["context_manifests"], ())
        self.assertEqual(bundle["verified_claims"], ())
        self.assertEqual(bundle["claim_links"], ())
        self.assertEqual(len(bundle["trusted_provenance_records"]), 1)
        provenance = bundle["trusted_provenance_records"][0]
        self.assertEqual(provenance["run_id"], request["run_id"])
        self.assertEqual(
            provenance["reuse_decisions"],
            [dict(current.reuse_decisions[0])],
        )

    def test_final_reuse_gate_binds_decision_to_current_cache_provenance(self):
        runtime, current, request = _physical_reuse_result_fixture(
            current_run_id="run-cache-gate",
        )
        query = current.persistence_records["query_execution_records"][0]
        result_payload = dict(query.result_payload)
        provider_stats = dict(result_payload.get("provider_stats") or {})
        cases = (
            ("cache_hit", False),
            ("cache_source", "unreviewed_cache"),
            ("source_result_ref", "result:other-source"),
            ("candidate_signature", "f" * 64),
        )
        for field, value in cases:
            with self.subTest(field=field):
                forged_query = replace(
                    query,
                    result_payload={
                        **result_payload,
                        "provider_stats": {
                            **provider_stats,
                            field: value,
                        },
                    },
                )
                forged_result = replace(
                    current,
                    persistence_records={
                        **dict(current.persistence_records),
                        "query_execution_records": (forged_query,),
                    },
                )

                with self.assertRaisesRegex(
                    EvidenceIntegrityError,
                    "analysis_runtime_reuse_decision_cache_mismatch",
                ):
                    runtime.build_persistence_bundle(
                        forged_result,
                        answer_package={"status": "complete", "sections": []},
                        request=request,
                        artifact_path=(
                            "artifacts/phase7/cache-gate/answer_package.json"
                        ),
                    )

    def test_store_rejects_resigned_physical_decision_lineage_and_owner_drift(self):
        runtime, current, request = _physical_reuse_result_fixture(
            current_run_id="run-store-physical-owner-gate",
        )
        bundles = {
            "zero_claim": runtime.build_persistence_bundle(
                current,
                answer_package={"status": "complete", "sections": []},
                request=request,
                artifact_path=(
                    "artifacts/phase7/store-owner-zero/answer_package.json"
                ),
            ),
            "claim": runtime.build_persistence_bundle(
                current,
                answer_package=_physical_claim_package(current),
                request=request,
                artifact_path=(
                    "artifacts/phase7/store-owner-claim/answer_package.json"
                ),
            ),
        }
        cases = (
            (
                "source_ref",
                {"source_ref": "result:resigned-other-source"},
                "runtime_persistence_reuse_decision_cache_mismatch",
            ),
            (
                "candidate_signature",
                {"candidate_signature": "f" * 64},
                "runtime_persistence_reuse_decision_cache_mismatch",
            ),
            (
                "query_execution_record_ref",
                {"query_execution_record_ref": "query-execution:resigned-other"},
                "runtime_persistence_reuse_decision_query_mismatch",
            ),
            (
                "completeness_record_refs",
                {
                    "completeness_record_refs": (
                        "completeness-record:resigned-other",
                    )
                },
                "runtime_persistence_reuse_decision_completeness_mismatch",
            ),
        )
        for publication_kind, bundle in bundles.items():
            current_decision = bundle["trusted_provenance_records"][0][
                "reuse_decisions"
            ][0]
            for field, changes, expected in cases:
                with self.subTest(publication=publication_kind, field=field):
                    decision = _resigned_physical_decision(
                        current_decision,
                        **changes,
                    )
                    invalid_bundle = _bundle_with_physical_decision(
                        bundle,
                        decision,
                    )
                    with self.assertRaisesRegex(EvidenceIntegrityError, expected):
                        InMemoryConversationStore().save_analysis_runtime_records(
                            run_id=request["run_id"],
                            **invalid_bundle,
                        )

    def test_store_closes_physical_reuse_source_candidate_authority(self):
        cases = (
            ("source_run_id", "run-forged-source"),
            (
                "source_analysis_contract_ref",
                "analysis:run-forged-source:1",
            ),
            ("source_query_contract_ref", "query:forged-source"),
            (
                "source_query_execution_record_ref",
                "query-execution:forged-source",
            ),
            (
                "source_completeness_record_refs",
                ("completeness-record:forged-source",),
            ),
        )
        for field, value in cases:
            with self.subTest(field=field):
                runtime, current, request = _physical_reuse_result_fixture(
                    current_run_id=f"run-store-source-gate-{field}",
                )
                bundle = runtime.build_persistence_bundle(
                    current,
                    answer_package=_physical_claim_package(current),
                    request=request,
                    artifact_path=(
                        f"artifacts/phase7/source-gate-{field}/answer_package.json"
                    ),
                )
                current_decision = bundle["trusted_provenance_records"][0][
                    "reuse_decisions"
                ][0]
                decision = _resigned_physical_decision(
                    current_decision,
                    **{field: value},
                )
                invalid_bundle = _bundle_with_physical_decision(bundle, decision)

                with self.assertRaisesRegex(
                    EvidenceIntegrityError,
                    "runtime_persistence_reuse_decision_source_authority_mismatch",
                ):
                    runtime.store.save_analysis_runtime_records(
                        run_id=request["run_id"],
                        **invalid_bundle,
                    )

    def test_store_requires_completed_material_authority_anchor_for_reuse_source(self):
        cases = (
            (
                "missing_event",
                lambda store, source_run_id: setattr(
                    store,
                    "_audit_events",
                    [
                        event
                        for event in store._audit_events
                        if not (
                            event.get("event_type")
                            == "completed_material_authority_recorded"
                            and event.get("run_id") == source_run_id
                        )
                    ],
                ),
            ),
            (
                "duplicate_event",
                lambda store, source_run_id: store._audit_events.append(
                    deepcopy(
                        next(
                            event
                            for event in store._audit_events
                            if event.get("event_type")
                            == "completed_material_authority_recorded"
                            and event.get("run_id") == source_run_id
                        )
                    )
                ),
            ),
            (
                "request_contract_drift",
                lambda store, source_run_id: store.runs[source_run_id][
                    "request"
                ]["analysis_contract"].update({"permission_scope": "viewer"}),
            ),
            (
                "request_material_drift",
                lambda store, source_run_id: store.runs[source_run_id][
                    "request"
                ]["material_authority"]["route_material_slots"].update(
                    {"diagnostic_tags": ["unreviewed"]}
                ),
            ),
            (
                "thread_owner_drift",
                lambda store, source_run_id: store.runs[source_run_id].update(
                    {"thread_id": "thread-forged"}
                ),
            ),
        )
        for case, mutate in cases:
            with self.subTest(case=case):
                runtime, current, request, bundle = _physical_reuse_bundle_fixture(
                    current_run_id=f"run-source-completion-anchor-{case}",
                    answer_package={"status": "complete", "sections": []},
                )
                source_run_id = current.reuse_decisions[0]["source_run_id"]
                mutate(runtime.store, source_run_id)

                with self.assertRaisesRegex(
                    EvidenceIntegrityError,
                    "runtime_persistence_reuse_decision_source_authority_missing",
                ):
                    runtime.store.save_analysis_runtime_records(
                        run_id=request["run_id"],
                        **bundle,
                    )

    def test_postgres_candidate_authority_rebuilds_compact_publication_index(self):
        from bi_agent.conversation.postgres_store import PostgresConversationStore

        runtime, current, request = _physical_reuse_result_fixture(
            current_run_id="run-pg-compact-publication-source"
        )
        decision = current.reuse_decisions[0]
        connection = _NormalizedPgCandidateConnection(runtime, decision)
        publication_index = connection.root_row["source_publication_payload"]

        self.assertEqual(
            set(publication_index),
            {"schema_version", "analysis_contract_id", "ordered_refs"},
        )
        self.assertNotEqual(
            connection.root_row["source_publication_digest"],
            canonical_digest(publication_index),
        )
        authority = PostgresConversationStore(
            connection
        ).resolve_result_candidate_authority(
            result_ref=decision["source_ref"],
            topic_id=request["topic_id"],
        )

        self.assertEqual(
            authority["result_ref_record"]["payload"]["candidate_signature"],
            decision["candidate_signature"],
        )
        sql = "\n".join(statement for statement, _ in connection.statements)
        self.assertIn("waje_runtime.query_execution_authority", sql)
        self.assertIn("waje_runtime.rows_metadata_authority", sql)
        self.assertIn("waje_runtime.snapshot_authority", sql)
        self.assertIn("waje_runtime.query_completeness_reports", sql)
        self.assertIn("waje_runtime.capability_binding_authority", sql)

    def test_postgres_candidate_authority_rejects_same_run_record_outside_compact_index(self):
        from bi_agent.conversation.postgres_store import PostgresConversationStore

        runtime, current, request = _physical_reuse_result_fixture(
            current_run_id="run-pg-compact-publication-extra-record"
        )
        decision = current.reuse_decisions[0]
        connection = _NormalizedPgCandidateConnection(runtime, decision)
        connection.same_run_extra_inventory = {
            "record_group": "repair_attempts",
            "record_ref": "repair:unindexed-same-run",
            "owner_run_ids": [decision["source_run_id"]],
            "payload": {
                "attempt_ref": "repair:unindexed-same-run",
                "failed_signature": "unindexed-signature",
                "action": "recompile_contract",
                "reason": "query_contract_validation_failed",
            },
        }

        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "result_candidate_source_publication_mismatch:normalized_unexpected",
        ):
            PostgresConversationStore(
                connection
            ).resolve_result_candidate_authority(
                result_ref=decision["source_ref"],
                topic_id=request["topic_id"],
            )

    def test_postgres_candidate_authority_rejects_full_publication_digest_drift(self):
        from bi_agent.conversation.postgres_store import PostgresConversationStore

        runtime, current, request = _physical_reuse_result_fixture(
            current_run_id="run-pg-publication-digest-drift"
        )
        decision = current.reuse_decisions[0]
        connection = _NormalizedPgCandidateConnection(runtime, decision)
        connection.root_row["source_publication_digest"] = "0" * 64

        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "result_candidate_source_publication_mismatch:digest",
        ):
            PostgresConversationStore(
                connection
            ).resolve_result_candidate_authority(
                result_ref=decision["source_ref"],
                topic_id=request["topic_id"],
            )

    def test_store_revalidates_current_and_source_query_semantics_for_physical_reuse(self):
        runtime, _, store, topic_id, signed = _runtime_fixture()
        source_run_id = "run-cache-equivalence-query-source"
        source = runtime.execute(_source_request(source_run_id, topic_id))
        candidate = _candidate(runtime, source, signed)
        _publish_source(runtime, store, topic_id, source, candidate)
        current_run_id = "run-cache-equivalence-query-current"
        current = runtime.execute(
            AnalysisRuntimeRequest.create(
                run_id=current_run_id,
                topic_id=topic_id,
                proposal=_proposal(("previous_day",)),
                accepted_graph=("compare_periods",),
                as_of="2026-06-03T12:00:00+01:00",
                permission_scope="analyst",
                reuse_candidates=(candidate,),
            )
        )
        self.assertEqual(current.reuse_decisions[0]["decision"], "rerun")
        request = {
            "run_id": current_run_id,
            "thread_id": "thread-reuse",
            "topic_id": topic_id,
            "permission_context": {"role": "analyst"},
            "context_manifest": {
                "snapshot_version": "2026H1",
                "contract_versions": {"runtime": "contracts-v1"},
            },
        }
        bundle = runtime.build_persistence_bundle(
            current,
            answer_package={"status": "complete", "sections": []},
            request=request,
            artifact_path=(
                "artifacts/phase7/cache-equivalence-query/answer_package.json"
            ),
        )
        bundle = _bundle_with_resigned_query_provider_stats(
            bundle,
            cache_hit=True,
            cache_source="validated_authoritative_query_chain",
            source_result_ref=candidate["result_ref"],
            candidate_signature=candidate["candidate_signature"],
        )
        forged = _resigned_physical_decision(
            bundle["trusted_provenance_records"][0]["reuse_decisions"][0],
            decision="reuse",
            reason="validated_authoritative_query_chain",
            candidate_signature=candidate["candidate_signature"],
        )
        bundle = _bundle_with_physical_decision(bundle, forged)

        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "runtime_persistence_reuse_decision_cache_equivalence_mismatch:query_contract",
        ):
            store.save_analysis_runtime_records(
                run_id=current_run_id,
                **bundle,
            )

    def test_store_compares_mappingproxy_query_contract_semantics(self):
        runtime, _, request, bundle = _physical_reuse_bundle_fixture(
            current_run_id="run-cache-equivalence-mappingproxy",
            answer_package={"status": "complete", "sections": []},
        )
        current_contract = bundle["query_contracts"][0]
        proxied_contract = replace(
            current_contract,
            query_parameters=MappingProxyType(
                dict(current_contract.query_parameters)
            ),
        )
        self.assertEqual(
            query_contract_signature(proxied_contract),
            current_contract.contract_signature,
        )

        self.assertEqual(
            runtime.store.save_analysis_runtime_records(
                run_id=request["run_id"],
                **{
                    **bundle,
                    "query_contracts": (proxied_contract,),
                },
            ),
            "published",
        )

    def test_store_revalidates_current_completeness_and_snapshot_axes_for_physical_reuse(self):
        cases = (
            (
                "completeness",
                lambda bundle: _bundle_with_current_completeness_state(
                    bundle,
                    completeness_status="incomplete",
                    analysis_readiness="partial",
                ),
                "completeness",
            ),
            (
                "release",
                lambda bundle: _bundle_with_current_snapshot_axis(
                    bundle,
                    release_ref="dataset-release:sha256:" + "f" * 64,
                ),
                "snapshot_release",
            ),
            (
                "schema",
                lambda bundle: _bundle_with_current_snapshot_axis(
                    bundle,
                    schema_fingerprint="schema:sha256:" + "e" * 64,
                ),
                "snapshot_release",
            ),
            (
                "row-count",
                lambda bundle: _bundle_with_current_snapshot_axis(
                    bundle,
                    row_count=bundle["snapshot_records"][0].snapshot.row_count + 1,
                ),
                "snapshot_release",
            ),
            (
                "rows-content-hash",
                lambda bundle: _bundle_with_current_snapshot_axis(
                    bundle,
                    rows_content_hash="f" * 64,
                ),
                "snapshot_release",
            ),
            (
                "physical-table",
                lambda bundle: _bundle_with_current_snapshot_axis(
                    bundle,
                    physical_table="analytics.snapshot_drift",
                ),
                "snapshot_release",
            ),
            (
                "permission-scopes",
                lambda bundle: _bundle_with_current_snapshot_axis(
                    bundle,
                    permission_scopes=("analyst", "viewer"),
                ),
                "snapshot_release",
            ),
            (
                "date-range",
                lambda bundle: _bundle_with_current_snapshot_axis(
                    bundle,
                    date_range=("2026-05-01", "2026-06-02"),
                ),
                "snapshot_release",
            ),
        )
        for case, mutate, component in cases:
            with self.subTest(case=case):
                runtime, _, request, bundle = _physical_reuse_bundle_fixture(
                    current_run_id=f"run-cache-equivalence-{case}",
                    answer_package={"status": "complete", "sections": []},
                )
                invalid = mutate(bundle)

                with self.assertRaisesRegex(
                    EvidenceIntegrityError,
                    (
                        "runtime_persistence_reuse_decision_"
                        f"cache_equivalence_mismatch:{component}"
                    ),
                ):
                    runtime.store.save_analysis_runtime_records(
                        run_id=request["run_id"],
                        **invalid,
                    )

    def test_in_memory_run_request_is_frozen_from_nested_caller_mutation(self):
        store = InMemoryConversationStore()
        request = {
            "material": {
                "nested": [
                    {"value": "original"},
                ]
            }
        }
        store.upsert_run(
            "run-request-freeze",
            thread_id="thread-request-freeze",
            topic_id="topic-request-freeze",
            status="running_workflow",
            request=request,
        )

        request["material"]["nested"][0]["value"] = "mutated"

        self.assertEqual(
            store.runs["run-request-freeze"]["request"]["material"][
                "nested"
            ][0]["value"],
            "original",
        )

    def test_store_rejects_resigned_candidate_not_backed_by_source_publication(self):
        runtime, current, request = _physical_reuse_result_fixture(
            current_run_id="run-store-source-publication-gate",
        )
        bundle = runtime.build_persistence_bundle(
            current,
            answer_package=_physical_claim_package(current),
            request=request,
            artifact_path=(
                "artifacts/phase7/source-publication-gate/answer_package.json"
            ),
        )
        source_decision = bundle["trusted_provenance_records"][0][
            "reuse_decisions"
        ][0]
        topic_records = runtime.store.result_refs[request["topic_id"]]
        source_record_index = next(
            index
            for index, record in enumerate(topic_records)
            if record.result_ref == source_decision["source_ref"]
        )
        source_record = topic_records[source_record_index]
        forged_candidate = _resign(
            source_record.payload,
            source_release_refs=["dataset-release:sha256:" + "f" * 64],
        )
        topic_records[source_record_index] = replace(
            source_record,
            payload=forged_candidate,
        )

        forged_bundle = _bundle_with_resigned_query_provider_stats(
            bundle,
            candidate_signature=forged_candidate["candidate_signature"],
        )
        forged_decision = _resigned_physical_decision(
            forged_bundle["trusted_provenance_records"][0]["reuse_decisions"][0],
            candidate_signature=forged_candidate["candidate_signature"],
        )
        forged_bundle = _bundle_with_physical_decision(
            forged_bundle,
            forged_decision,
        )

        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "result_candidate_source_publication_mismatch",
        ):
            runtime.store.save_analysis_runtime_records(
                run_id=request["run_id"],
                **forged_bundle,
            )

    def test_source_publication_candidate_requires_ready_succeeded_chain(self):
        cases = (
            (
                "query_execution",
                lambda payload: payload["query_execution_records"][0].update(
                    {"execution_status": "failed"}
                ),
            ),
            (
                "completeness",
                lambda payload: payload["completeness_records"][0][
                    "report_payload"
                ].update({"analysis_readiness": "partial"}),
            ),
            (
                "binding",
                lambda payload: payload["capability_binding_records"][0].update(
                    {"status": "blocked"}
                ),
            ),
        )
        for component, mutate in cases:
            with self.subTest(component=component):
                runtime, current, request = _physical_reuse_result_fixture(
                    current_run_id=f"run-source-readiness-{component}",
                )
                decision = current.reuse_decisions[0]
                source_run_id = decision["source_run_id"]
                publication = runtime.store.analysis_runtime_records[source_run_id]
                payload = canonical_value(publication["payload"])
                mutate(payload)
                runtime.store.analysis_runtime_records[source_run_id] = {
                    "digest": canonical_digest(payload),
                    "payload": payload,
                }

                with self.assertRaisesRegex(
                    EvidenceIntegrityError,
                    f"result_candidate_source_publication_mismatch:{component}",
                ):
                    runtime.store.resolve_result_candidate_authority(
                        result_ref=decision["source_ref"],
                        topic_id=request["topic_id"],
                    )

    def test_source_publication_rejects_query_contract_window_signature_drift(self):
        runtime, current, request = _physical_reuse_result_fixture(
            current_run_id="run-source-query-window-drift",
        )
        decision = current.reuse_decisions[0]
        source_run_id = decision["source_run_id"]
        publication = runtime.store.analysis_runtime_records[source_run_id]
        payload = canonical_value(publication["payload"])
        query_contract = payload["query_contracts"][0]
        query_contract["resolved_windows"][0]["start_inclusive"] = (
            "2026-05-30"
        )
        payload["query_execution_records"][0]["contract"] = canonical_value(
            query_contract
        )
        payload["query_execution_records"][0]["query_contract"] = canonical_value(
            query_contract
        )
        runtime.store.analysis_runtime_records[source_run_id] = {
            "digest": canonical_digest(payload),
            "payload": payload,
        }

        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "result_candidate_source_publication_mismatch:query_contract",
        ):
            runtime.store.resolve_result_candidate_authority(
                result_ref=decision["source_ref"],
                topic_id=request["topic_id"],
            )

    def test_source_publication_ignores_non_candidate_blocked_siblings(self):
        runtime, current, request = _physical_reuse_result_fixture(
            current_run_id="run-source-blocked-sibling",
        )
        decision = current.reuse_decisions[0]
        source_run_id = decision["source_run_id"]
        publication = runtime.store.analysis_runtime_records[source_run_id]
        payload = canonical_value(publication["payload"])
        completeness_sibling = {
            **payload["completeness_records"][0],
            "record_ref": "completeness-record:blocked-sibling",
            "report_digest": "e" * 64,
            "report_payload": {
                "completeness_status": "incomplete",
                "analysis_readiness": "partial",
            },
        }
        binding_sibling = {
            **payload["capability_binding_records"][0],
            "record_ref": "capability-binding:blocked-sibling",
            "binding_digest": "d" * 64,
            "status": "blocked",
        }
        payload["completeness_records"].insert(0, completeness_sibling)
        payload["capability_binding_records"].insert(0, binding_sibling)
        runtime.store.analysis_runtime_records[source_run_id] = {
            "digest": canonical_digest(payload),
            "payload": payload,
        }

        authority = runtime.store.resolve_result_candidate_authority(
            result_ref=decision["source_ref"],
            topic_id=request["topic_id"],
        )

        self.assertEqual(
            authority["result_ref_record"]["payload"]["candidate_signature"],
            decision["candidate_signature"],
        )

    def test_postgres_candidate_authority_rejects_contract_row_drift(self):
        from bi_agent.conversation.postgres_store import PostgresConversationStore

        runtime, current, request, bundle = _physical_reuse_bundle_fixture(
            current_run_id="run-pg-source-contract-row-gate",
            answer_package={"status": "complete", "sections": []},
        )
        decision = current.reuse_decisions[0]
        connection = _NormalizedPgCandidateConnection(runtime, decision)
        connection.root_row["analysis_contract"] = {
            **connection.root_row["analysis_contract"],
            "permission_scope": "viewer",
        }

        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "result_candidate_source_publication_mismatch:digest",
        ):
            PostgresConversationStore(connection).save_analysis_runtime_records(
                run_id=request["run_id"],
                **bundle,
            )

    def test_store_rejects_resigned_query_cache_flags_for_physical_provenance(self):
        runtime, current, request = _physical_reuse_result_fixture(
            current_run_id="run-store-physical-cache-gate",
        )
        bundles = {
            "zero_claim": runtime.build_persistence_bundle(
                current,
                answer_package={"status": "complete", "sections": []},
                request=request,
                artifact_path=(
                    "artifacts/phase7/store-cache-zero/answer_package.json"
                ),
            ),
            "claim": runtime.build_persistence_bundle(
                current,
                answer_package=_physical_claim_package(current),
                request=request,
                artifact_path=(
                    "artifacts/phase7/store-cache-claim/answer_package.json"
                ),
            ),
        }
        cases = (
            ("cache_hit", False),
            ("cache_source", "unreviewed_cache"),
            ("source_result_ref", "result:resigned-other-source"),
            ("candidate_signature", "f" * 64),
        )
        for publication_kind, bundle in bundles.items():
            for field, value in cases:
                with self.subTest(publication=publication_kind, field=field):
                    invalid_bundle = _bundle_with_resigned_query_provider_stats(
                        bundle,
                        **{field: value},
                    )
                    with self.assertRaisesRegex(
                        EvidenceIntegrityError,
                        "runtime_persistence_reuse_decision_cache_mismatch",
                    ):
                        InMemoryConversationStore().save_analysis_runtime_records(
                            run_id=request["run_id"],
                            **invalid_bundle,
                        )

    def test_waiting_projection_drops_decision_with_omitted_query_chain(self):
        runtime, current, request = _physical_reuse_result_fixture(
            current_run_id="run-waiting-omitted-reuse",
        )
        unbound_result = replace(
            current,
            persistence_records={
                **dict(current.persistence_records),
                "capability_binding_records": (),
            },
        )

        bundle = runtime.build_persistence_bundle(
            unbound_result,
            answer_package={
                "status": "waiting_for_clarification",
                "sections": [],
            },
            request=request,
            artifact_path=(
                "artifacts/phase7/waiting-omitted-reuse/answer_package.json"
            ),
            publication_mode="waiting_for_clarification",
        )

        self.assertEqual(bundle["query_execution_records"], ())
        self.assertEqual(bundle["completeness_records"], ())
        self.assertEqual(bundle["trusted_provenance_records"], ())

    def test_physical_provenance_cannot_downgrade_by_deleting_marker_fields(self):
        from bi_agent.runtime.claim_provenance import (
            build_trusted_claim_provenance_record,
        )

        decision = build_physical_query_reuse_decision_record(
            run_id="run-parser-current",
            topic_id="topic-parser-current",
            analysis_contract_ref="analysis:run-parser-current:1",
            source_run_id="run-parser-source",
            source_analysis_contract_ref="analysis:run-parser-source:1",
            source_ref="result:parser-source",
            source_query_contract_ref="query:parser-source",
            source_query_execution_record_ref="query-execution:parser-source",
            source_completeness_record_refs=(
                "completeness-record:parser-source",
            ),
            result_ref="result:parser-current",
            query_contract_ref="query:parser-current",
            query_contract_signature="a" * 64,
            query_execution_record_ref="query-execution:parser-current",
            completeness_record_refs=(
                "completeness-record:parser-current",
            ),
            candidate_signature="b" * 64,
            decision="reuse",
            reason="validated_authoritative_query_chain",
        )
        downgraded = {
            key: value
            for key, value in decision.items()
            if key not in {"schema_version", "decision_ref", "decision_digest"}
        }
        builders = (
            (
                "claim_provenance",
                lambda: build_trusted_claim_provenance_record(
                    run_id="run-parser-current",
                    reuse_decisions=(downgraded,),
                ),
            ),
            (
                "answer_context",
                lambda: AnswerPackageBuildContext.create(
                    request={
                        "run_id": "run-parser-current",
                        "thread_id": "thread-parser-current",
                        "topic_id": "topic-parser-current",
                        "reuse_decisions": [downgraded],
                    },
                    artifact_path=(
                        "artifacts/phase7/parser-current/answer_package.json"
                    ),
                ),
            ),
        )
        for builder, invoke in builders:
            with self.subTest(builder=builder):
                with self.assertRaisesRegex(
                    EvidenceIntegrityError,
                    "reuse_decision_shape_invalid",
                ):
                    invoke()

    def test_claimless_physical_reuse_provenance_reaches_pg_and_eval_projection(self):
        from bi_agent.conversation.postgres_store import PostgresConversationStore
        from tests.phase7.test_conversation_persistence import FakeConnection
        from tools.phase7.run_live_conversation_system_test import (
            _normalized_runtime_evaluation_projection,
        )

        scenarios = (
            (
                "zero_claim",
                {"status": "complete", "sections": []},
                "complete",
            ),
            (
                "waiting_zero_claim",
                {
                    "status": "waiting_for_clarification",
                    "sections": [],
                },
                "waiting_for_clarification",
            ),
        )
        for scenario, answer_package, publication_mode in scenarios:
            with self.subTest(scenario=scenario):
                runtime, current, request, bundle = _physical_reuse_bundle_fixture(
                    current_run_id=f"run-{scenario}-authority",
                    answer_package=answer_package,
                    publication_mode=publication_mode,
                )
                expected_decision = dict(current.reuse_decisions[0])

                self.assertEqual(
                    runtime.store.save_analysis_runtime_records(
                        run_id=request["run_id"],
                        **bundle,
                    ),
                    "published",
                )
                connection = _NormalizedPgCandidateConnection(
                    runtime,
                    expected_decision,
                )
                self.assertEqual(
                    PostgresConversationStore(
                        connection
                    ).save_analysis_runtime_records(
                        run_id=request["run_id"],
                        **bundle,
                    ),
                    "published",
                )
                sql = "\n".join(
                    statement for statement, _ in connection.statements
                )
                self.assertIn("waje_runtime.claim_provenance_records", sql)

                evaluation_bundle = canonical_value(bundle)
                projection = _normalized_runtime_evaluation_projection(
                    run_id=request["run_id"],
                    thread_id=request["thread_id"],
                    topic_id=request["topic_id"],
                    turn_id=f"turn-{scenario}-authority",
                    run_status="completed",
                    publication_digest=canonical_digest(evaluation_bundle),
                    bundle=evaluation_bundle,
                    stored_contract_signature=bundle["analysis_contract"][
                        "contract_signature"
                    ],
                    delivery_verifier={"status": "passed", "errors": []},
                    result_candidate_resolver=(
                        runtime.store.resolve_result_candidate_authority
                    ),
                )
                self.assertEqual(
                    projection["reuse_decisions"],
                    [expected_decision],
                )

    def test_claimed_physical_reuse_provenance_reaches_store(self):
        runtime, current, request = _physical_reuse_result_fixture(
            current_run_id="run-claimed-physical-authority",
        )
        answer_package = {
            **_physical_claim_package(current),
            "run_id": request["run_id"],
        }
        bundle = runtime.build_persistence_bundle(
            current,
            answer_package=answer_package,
            request=request,
            artifact_path=(
                "artifacts/phase7/claimed-physical-authority/answer_package.json"
            ),
        )
        bind_answer_package_artifact(
            bundle,
            run_id=request["run_id"],
            answer_package=answer_package,
        )

        self.assertEqual(len(bundle["verified_claims"]), 1)
        self.assertEqual(
            bundle["verified_claims"][0]["reuse_decisions"],
            [dict(current.reuse_decisions[0])],
        )
        self.assertEqual(
            runtime.store.save_analysis_runtime_records(
                run_id=request["run_id"],
                **bundle,
            ),
            "published",
        )

    def test_claimless_provenance_rejects_legacy_or_empty_reuse_decisions(self):
        from bi_agent.conversation.postgres_store import PostgresConversationStore
        from bi_agent.runtime.claim_provenance import (
            build_trusted_claim_provenance_record,
        )
        from tests.phase7.test_conversation_persistence import FakeConnection

        _, current, request, bundle = _physical_reuse_bundle_fixture(
            current_run_id="run-zero-claim-provenance-shape",
            answer_package={"status": "complete", "sections": []},
        )
        physical = dict(current.reuse_decisions[0])
        legacy = {"source_ref": "result:legacy", "decision": "reuse"}
        invalid_decisions = (
            ("empty", ()),
            ("legacy", (legacy,)),
            ("mixed", (physical, legacy)),
        )

        for case, decisions in invalid_decisions:
            provenance = build_trusted_claim_provenance_record(
                run_id=request["run_id"],
                artifact_refs=("artifact:zero-claim-audit",),
                memory_refs=("memory:zero-claim-audit",),
                reuse_decisions=decisions,
            )
            invalid_bundle = {
                **bundle,
                "trusted_provenance_records": (provenance,),
            }
            stores = (
                ("in_memory", InMemoryConversationStore()),
                (
                    "postgres",
                    PostgresConversationStore(FakeConnection()),
                ),
            )
            for store_kind, store in stores:
                with self.subTest(case=case, store=store_kind):
                    with self.assertRaisesRegex(
                        EvidenceIntegrityError,
                        "runtime_persistence_zero_claim_provenance_invalid",
                    ):
                        store.save_analysis_runtime_records(
                            run_id=request["run_id"],
                            **invalid_bundle,
                        )

    def test_no_candidate_keeps_fresh_execution_path(self):
        runtime, provider, _, topic_id, _ = _runtime_fixture()

        current = runtime.execute(_source_request("run-fresh", topic_id))

        self.assertEqual(provider.calls, 1)
        self.assertEqual(current.reuse_decisions, ())
        self.assertEqual(current.query_results[0].provider_stats.get("cache_hit"), None)

    def test_fresh_query_provenance_ignores_conversation_rerun_marker(self):
        runtime, _, store, topic_id, _ = _runtime_fixture()
        run_id = "run-fresh-conversation-rerun"
        current = runtime.execute(_source_request(run_id, topic_id))
        request = {
            "run_id": run_id,
            "thread_id": "thread-reuse",
            "topic_id": topic_id,
            "permission_context": {"role": "analyst"},
            "context_manifest": {"manifest_id": "context-current"},
            "reuse_decisions": [
                {
                    "source_ref": "",
                    "result_ref": "",
                    "decision": "rerun",
                    "reason": "no_prior_result_ref",
                    "can_support_claim": False,
                    "requires_rerun": True,
                }
            ],
        }

        answer_package = {
            **_physical_claim_package(current),
            "run_id": run_id,
        }
        bundle = runtime.build_persistence_bundle(
            current,
            answer_package=answer_package,
            request=request,
            artifact_path="artifacts/phase7/fresh-query/answer_package.json",
        )
        bind_answer_package_artifact(
            bundle,
            run_id=run_id,
            answer_package=answer_package,
        )

        expected = [{"source_ref": "context-current", "decision": "fresh"}]
        self.assertEqual(
            bundle["trusted_provenance_records"][0]["reuse_decisions"],
            expected,
        )
        self.assertEqual(bundle["verified_claims"][0]["reuse_decisions"], expected)
        store.upsert_run(
            run_id,
            thread_id="thread-reuse",
            topic_id=topic_id,
            status="running_workflow",
            request=request,
        )
        self.assertEqual(
            store.save_analysis_runtime_records(run_id=run_id, **bundle),
            "published",
        )

    def test_authority_candidate_drift_reruns_instead_of_trusting_payload(self):
        cases = (
            (
                "query_signature",
                lambda item: _resign(
                    item,
                    query_contract_signature="0" * 64,
                ),
                "reuse_candidate_query_authority_mismatch",
            ),
            (
                "source_release",
                lambda item: _resign(
                    item,
                    source_release_refs=["release:drift"],
                ),
                "reuse_candidate_snapshot_authority_mismatch",
            ),
            (
                "source_schema",
                lambda item: _resign(
                    item,
                    source_schema_fingerprints=["schema:drift"],
                ),
                "reuse_candidate_snapshot_authority_mismatch",
            ),
            (
                "rows_content",
                lambda item: _resign(
                    item,
                    rows_content_hash="1" * 64,
                ),
                "reuse_candidate_rows_authority_mismatch",
            ),
            (
                "completeness",
                lambda item: _resign(
                    item,
                    completeness_record_refs=["completeness-record:missing"],
                ),
                "reuse_candidate_completeness_record_missing",
            ),
            (
                "binding",
                lambda item: _resign(
                    item,
                    binding_record_refs=["capability-binding:missing"],
                ),
                "reuse_candidate_binding_record_missing",
            ),
        )
        for name, mutate, expected_reason in cases:
            with self.subTest(name=name):
                runtime, provider, store, topic_id, signed = _runtime_fixture()
                source = runtime.execute(
                    _source_request(f"run-drift-source-{name}", topic_id)
                )
                candidate = _candidate(runtime, source, signed)
                _publish_source(runtime, store, topic_id, source, candidate)

                current = runtime.execute(
                    AnalysisRuntimeRequest.create(
                        run_id=f"run-drift-current-{name}",
                        topic_id=topic_id,
                        proposal=_proposal(),
                        accepted_graph=("compare_periods",),
                        as_of="2026-06-03T12:00:00+01:00",
                        permission_scope="analyst",
                        reuse_candidates=(mutate(candidate),),
                    )
                )

                self.assertEqual(provider.calls, 2)
                self.assertEqual(current.reuse_decisions[0]["decision"], "rerun")
                self.assertIn(
                    expected_reason,
                    current.reuse_decisions[0]["reason"],
                )
                self.assertIsNot(
                    current.query_results[0].provider_stats.get("cache_hit"),
                    True,
                )
                validate_physical_query_reuse_decision_record(
                    current.reuse_decisions[0]
                )
                current_run_id = f"run-drift-current-{name}"
                answer_package = {
                    "run_id": current_run_id,
                    "status": "complete",
                    "sections": [],
                }
                rerun_bundle = runtime.build_persistence_bundle(
                    current,
                    answer_package=answer_package,
                    request={
                        "run_id": current_run_id,
                        "thread_id": "thread-reuse",
                        "topic_id": topic_id,
                        "permission_context": {"role": "analyst"},
                    },
                    artifact_path=(
                        f"artifacts/phase7/{current_run_id}/answer_package.json"
                    ),
                )
                bind_answer_package_artifact(
                    rerun_bundle,
                    run_id=current_run_id,
                    answer_package=answer_package,
                )
                self.assertEqual(
                    store.save_analysis_runtime_records(
                        run_id=current_run_id,
                        **rerun_bundle,
                    ),
                    "published",
                )

    def test_binding_owned_by_different_analysis_contract_reruns(self):
        runtime, provider, store, topic_id, signed = _runtime_fixture()
        source = runtime.execute(
            _source_request("run-binding-owner-source", topic_id)
        )
        original_binding = source.persistence_records[
            "capability_binding_records"
        ][0]
        foreign_plan = replace(
            source.capability_plans[0],
            analysis_contract_ref="analysis:foreign-run:1",
        )
        foreign_binding = (
            runtime.evidence_authority._runtime_writer().record_capability_binding(
                foreign_plan,
                original_binding.binding_payload,
            )
        )
        candidate = _resign(
            _candidate(runtime, source, signed),
            binding_record_refs=[foreign_binding.record_ref],
            binding_record_digests=[foreign_binding.binding_digest],
        )
        _publish_source(runtime, store, topic_id, source, candidate)

        current = runtime.execute(
            AnalysisRuntimeRequest.create(
                run_id="run-binding-owner-current",
                topic_id=topic_id,
                proposal=_proposal(),
                accepted_graph=("compare_periods",),
                as_of="2026-06-03T12:00:00+01:00",
                permission_scope="analyst",
                reuse_candidates=(candidate,),
            )
        )

        self.assertEqual(provider.calls, 2)
        self.assertEqual(current.reuse_decisions[0]["decision"], "rerun")
        self.assertIn(
            "result_candidate_source_publication_mismatch:binding",
            current.reuse_decisions[0]["reason"],
        )

    def test_in_memory_candidate_source_run_must_own_analysis_contract(self):
        runtime, provider, store, topic_id, signed = _runtime_fixture()
        source = runtime.execute(
            _source_request("run-contract-owner-a", topic_id)
        )
        candidate = _resign(
            _candidate(runtime, source, signed),
            source_run_id="run-contract-owner-b",
        )
        _publish_source(runtime, store, topic_id, source, candidate)
        store.upsert_run(
            "run-contract-owner-b",
            thread_id="thread-reuse",
            topic_id=topic_id,
            status="completed",
            request={
                "context_manifest": {
                    "snapshot_version": "2026H1",
                    "contract_versions": {"runtime": "contracts-v1"},
                }
            },
        )

        current = runtime.execute(
            AnalysisRuntimeRequest.create(
                run_id="run-contract-owner-current",
                topic_id=topic_id,
                proposal=_proposal(),
                accepted_graph=("compare_periods",),
                as_of="2026-06-03T12:00:00+01:00",
                permission_scope="analyst",
                reuse_candidates=(candidate,),
            )
        )

        self.assertEqual(provider.calls, 2)
        self.assertEqual(current.reuse_decisions[0]["decision"], "rerun")
        self.assertIn(
            "result_candidate_source_publication_mismatch:digest",
            current.reuse_decisions[0]["reason"],
        )

    def test_changed_required_window_set_reruns(self):
        runtime, provider, store, topic_id, signed = _runtime_fixture()
        source = runtime.execute(_source_request("run-window-source", topic_id))
        candidate = _candidate(runtime, source, signed)
        _publish_source(runtime, store, topic_id, source, candidate)

        current = runtime.execute(
            AnalysisRuntimeRequest.create(
                run_id="run-window-current",
                topic_id=topic_id,
                proposal=_proposal(("previous_day",)),
                accepted_graph=("compare_periods",),
                as_of="2026-06-03T12:00:00+01:00",
                permission_scope="analyst",
                reuse_candidates=(candidate,),
            )
        )

        self.assertEqual(provider.calls, 2)
        self.assertEqual(current.reuse_decisions[0]["decision"], "rerun")
        self.assertEqual(
            current.reuse_decisions[0]["reason"],
            "reuse_fixed_window_mismatch",
        )

    def test_changed_permission_scope_reruns(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        authority = RuntimeEvidenceAuthority(runtime_registry=registry)
        permission_snapshot = replace(
            snapshot("paid_order_success", "paid_success", "2026-07-04"),
            permission_scopes=("viewer", "analyst"),
        )
        catalog, release_resolver, signed = canonical_release_catalog(
            permission_snapshot
        )
        provider = _CountingRowsRuntime()
        store = InMemoryConversationStore()
        store.create_thread("thread-reuse", owner_id="analyst-1")
        topic = store.create_topic("thread-reuse", title="付费金额权限分析")
        runtime = AnalysisRuntime(
            catalog=catalog,
            registry=registry,
            executor=ClickHouseQueryExecutor(
                provider,
                evidence_resolver=authority,
                rows_loader=authority.rows_loader,
                evidence_writer=authority._runtime_writer(),
                release_resolver=release_resolver,
            ),
            release_resolver=release_resolver,
            evidence_authority=authority,
            store=store,
        )
        source = runtime.execute(
            _source_request("run-permission-source", topic.topic_id)
        )
        candidate = _candidate(runtime, source, signed)
        _publish_source(runtime, store, topic.topic_id, source, candidate)

        current = runtime.execute(
            AnalysisRuntimeRequest.create(
                run_id="run-permission-current",
                topic_id=topic.topic_id,
                proposal=_proposal(),
                accepted_graph=("compare_periods",),
                as_of="2026-06-03T12:00:00+01:00",
                permission_scope="viewer",
                reuse_candidates=(candidate,),
            )
        )

        self.assertEqual(provider.calls, 2)
        self.assertEqual(current.reuse_decisions[0]["decision"], "rerun")
        self.assertEqual(
            current.reuse_decisions[0]["reason"],
            "reuse_permission_scope_mismatch",
        )

    def test_changed_active_release_or_schema_reruns_current_compiled_contract(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        authority = RuntimeEvidenceAuthority(runtime_registry=registry)
        source_seed = replace(
            snapshot("paid_order_success", "paid_success", "2026-07-04"),
            logical_snapshot_id="paid-success-logical",
            load_revision="paid-success-load:sha256:source",
        )
        current_seed = replace(
            source_seed,
            snapshot_ref="snapshot:paid_order_success:2",
            schema_fingerprint="schema:paid_order_success:v2",
            load_revision="paid-success-load:sha256:current",
            rows_content_hash="c" * 64,
        )
        source_catalog, source_resolver, signed = canonical_release_catalog(
            source_seed
        )
        current_catalog, current_resolver, _ = canonical_release_catalog(
            current_seed
        )
        release_resolver = _CompositeReleaseResolver(
            source_resolver,
            current_resolver,
        )
        provider = _CountingRowsRuntime()
        store = InMemoryConversationStore()
        store.create_thread("thread-reuse", owner_id="analyst-1")
        topic = store.create_topic("thread-reuse", title="付费金额发布变更分析")
        active_catalog = [source_catalog]
        runtime = AnalysisRuntime(
            catalog=source_catalog,
            catalog_provider=lambda: active_catalog[0],
            registry=registry,
            executor=ClickHouseQueryExecutor(
                provider,
                evidence_resolver=authority,
                rows_loader=authority.rows_loader,
                evidence_writer=authority._runtime_writer(),
                release_resolver=release_resolver,
            ),
            release_resolver=release_resolver,
            evidence_authority=authority,
            store=store,
        )
        source = runtime.execute(
            _source_request("run-release-source", topic.topic_id)
        )
        candidate = _candidate(runtime, source, signed)
        _publish_source(runtime, store, topic.topic_id, source, candidate)

        active_catalog[0] = current_catalog
        current = runtime.execute(
            AnalysisRuntimeRequest.create(
                run_id="run-release-current",
                topic_id=topic.topic_id,
                proposal=_proposal(),
                accepted_graph=("compare_periods",),
                as_of="2026-06-03T12:00:00+01:00",
                permission_scope="analyst",
                reuse_candidates=(candidate,),
            )
        )

        self.assertEqual(provider.calls, 2)
        self.assertEqual(current.reuse_decisions[0]["decision"], "rerun")
        self.assertEqual(
            current.reuse_decisions[0]["reason"],
            "reuse_snapshot_ref_mismatch",
        )
        self.assertNotEqual(
            current.query_contracts[0].contract_signature,
            source.query_contracts[0].contract_signature,
        )
        validate_physical_query_reuse_decision_record(
            current.reuse_decisions[0]
        )

    def test_workflow_replaces_provisional_decision_before_claim_provenance(self):
        from bi_agent.runtime.langgraph_workflow import _fetch_runtime_rows
        from bi_agent.runtime.analysis_contract_compiler import (
            compile_analysis_contract,
        )
        from bi_agent.runtime.dataset_catalog import DatasetCatalog
        from bi_agent.runtime.runtime_contract_registry import (
            RuntimeContractRegistry,
        )

        final_decision = build_physical_query_reuse_decision_record(
            run_id="run-workflow-reuse",
            topic_id="topic-workflow-reuse",
            analysis_contract_ref="analysis:run-workflow-reuse:1",
            source_run_id="run-workflow-source",
            source_analysis_contract_ref="analysis:run-workflow-source:1",
            source_ref="result:source",
            source_query_contract_ref="query:source",
            source_query_execution_record_ref="query-execution:source",
            source_completeness_record_refs=("completeness-record:source",),
            result_ref="result:current",
            query_contract_ref="query:current",
            query_contract_signature="a" * 64,
            query_execution_record_ref="query-execution:current",
            completeness_record_refs=("completeness-record:current",),
            candidate_signature="b" * 64,
            decision="reuse",
            reason="validated_authoritative_query_chain",
        )
        candidate = {"schema_version": "result-reuse-candidate.v1"}

        class FakeAnalysisRuntime:
            def __init__(self):
                self.request = None
                self.registry = RuntimeContractRegistry.from_path(
                    "contracts/runtime/clickhouse-analysis-bindings.yaml"
                )

            def execute(self, request):
                self.request = request
                compiled = compile_analysis_contract(
                    run_id=request.run_id,
                    proposal=request.proposal,
                    accepted_capabilities=request.accepted_graph,
                    catalog=DatasetCatalog(()),
                    registry=self.registry,
                    as_of=request.as_of,
                    permission_scope=request.permission_scope,
                )
                return SimpleNamespace(
                    to_workflow_payload=lambda: {
                        "runtime_rows_by_intent": {},
                        "result_refs_by_intent": {},
                        "reuse_decisions": [final_decision],
                        "analysis_runtime_status": "ready",
                    },
                    bound_capability_inputs={},
                    repair_decisions=(),
                    query_results=(),
                    analysis_contract=compiled.analysis_contract,
                    query_contracts=compiled.query_contracts,
                    capability_plans=compiled.capability_plans,
                )

        analysis_runtime = FakeAnalysisRuntime()
        state = {
            "run_id": "run-workflow-reuse",
            "request": {
                "analysis_runtime": analysis_runtime,
                "run_id": "run-workflow-reuse",
                "topic_id": "topic-workflow-reuse",
                "role": "analyst",
                "analysis_context": {
                    "as_of": "2026-06-03T12:00:00+01:00",
                },
                "reuse_candidates": [candidate],
                "reuse_decisions": [
                    {
                        "source_ref": "result:source",
                        "decision": "candidate",
                    }
                ],
                "context_manifest": {
                    "manifest_id": "context-workflow-reuse",
                    "items": [],
                },
            },
            "analysis_route": {
                "requested_nodes": [],
                "analysis_requirements": {
                    "target_metrics": ["paid_amount"],
                    "baselines": ["previous_day"],
                },
            },
            "intent": {
                "question_family": "custom_baseline_comparison",
                "question_families": ["custom_baseline_comparison"],
                "target_metric": "paid_amount",
                "scope": {"type": "full_sample"},
                "target_semantic": "yesterday",
            },
            "validator_results": [],
        }

        _fetch_runtime_rows(state)
        build_context = AnswerPackageBuildContext.create(
            request=state["request"],
            artifact_path="artifacts/phase7/workflow-reuse/answer_package.json",
        )

        self.assertEqual(analysis_runtime.request.topic_id, "topic-workflow-reuse")
        self.assertEqual(
            tuple(dict(item) for item in analysis_runtime.request.reuse_candidates),
            (candidate,),
        )
        self.assertEqual(state["request"]["reuse_decisions"], [final_decision])
        self.assertEqual(
            build_context.trusted_provenance["reuse_decisions"],
            [final_decision],
        )
        tampered_provenance = {
            **build_context.trusted_provenance,
            "reuse_decisions": [
                {**final_decision, "source_ref": "result:forged"}
            ],
        }
        from bi_agent.runtime.claim_provenance import (
            validate_trusted_claim_provenance_record,
        )

        with self.assertRaises(EvidenceIntegrityError):
            validate_trusted_claim_provenance_record(tampered_provenance)
        downgraded_request = {
            **state["request"],
            "reuse_decisions": [
                {
                    **final_decision,
                    "schema_version": "legacy-reuse-decision.v0",
                }
            ],
        }
        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "physical_reuse_decision_schema_version_invalid",
        ):
            AnswerPackageBuildContext.create(
                request=downgraded_request,
                artifact_path=(
                    "artifacts/phase7/workflow-reuse/downgraded.json"
                ),
            )

    def test_older_exact_candidate_wins_after_newest_candidate_mismatch(self):
        runtime, provider, store, topic_id, signed = _runtime_fixture()
        source = runtime.execute(_source_request("run-multi-source", topic_id))
        exact = _candidate(runtime, source, signed)
        _publish_source(runtime, store, topic_id, source, exact)
        newest_mismatch = _resign(
            exact,
            query_contract_signature="2" * 64,
        )

        current = runtime.execute(
            AnalysisRuntimeRequest.create(
                run_id="run-multi-current",
                topic_id=topic_id,
                proposal=_proposal(),
                accepted_graph=("compare_periods",),
                as_of="2026-06-03T12:00:00+01:00",
                permission_scope="analyst",
                reuse_candidates=(newest_mismatch, exact),
            )
        )

        self.assertEqual(provider.calls, 1)
        self.assertEqual(current.reuse_decisions[0]["decision"], "reuse")
        self.assertEqual(
            current.reuse_decisions[0]["candidate_signature"],
            exact["candidate_signature"],
        )

    def test_malformed_or_extra_candidate_payload_fails_closed_to_rerun(self):
        for name, mutate in (
            (
                "missing",
                lambda item: {
                    key: value
                    for key, value in item.items()
                    if key != "rows_ref"
                },
            ),
            ("extra", lambda item: {**item, "unexpected": "value"}),
        ):
            with self.subTest(name=name):
                runtime, provider, store, topic_id, signed = _runtime_fixture()
                source = runtime.execute(
                    _source_request(f"run-shape-source-{name}", topic_id)
                )
                candidate = _candidate(runtime, source, signed)
                _publish_source(runtime, store, topic_id, source, candidate)

                current = runtime.execute(
                    AnalysisRuntimeRequest.create(
                        run_id=f"run-shape-current-{name}",
                        topic_id=topic_id,
                        proposal=_proposal(),
                        accepted_graph=("compare_periods",),
                        as_of="2026-06-03T12:00:00+01:00",
                        permission_scope="analyst",
                        reuse_candidates=(mutate(candidate),),
                    )
                )

                self.assertEqual(provider.calls, 2)
                self.assertEqual(current.reuse_decisions[0]["decision"], "rerun")
                self.assertEqual(
                    current.reuse_decisions[0]["reason"],
                    "reuse_candidate_shape_invalid",
                )

    def test_ownerless_reuse_hint_does_not_block_fresh_result_delivery(self):
        runtime, provider, _store, topic_id, _signed = _runtime_fixture()

        current = runtime.execute(
            AnalysisRuntimeRequest.create(
                run_id="run-ownerless-hint-current",
                topic_id=topic_id,
                proposal=_proposal(),
                accepted_graph=("compare_periods",),
                as_of="2026-06-03T12:00:00+01:00",
                permission_scope="analyst",
                reuse_candidates=({},),
            )
        )

        self.assertEqual(provider.calls, 1)
        self.assertTrue(current.query_results)
        self.assertEqual(current.reuse_decisions, ())
        self.assertTrue(current.persistence_records["query_execution_records"])

    def test_source_run_analysis_and_topic_authority_drift_reruns(self):
        cases = (
            (
                "source_run",
                lambda item: _resign(item, source_run_id="run-other"),
                None,
                "reuse_candidate_source_run_mismatch",
            ),
            (
                "analysis",
                lambda item: _resign(
                    item,
                    analysis_contract_signature="3" * 64,
                ),
                None,
                "reuse_candidate_analysis_authority_mismatch",
            ),
            (
                "topic",
                lambda item: item,
                "different_topic",
                "result_candidate_authority_missing",
            ),
        )
        for name, mutate, topic_mode, expected_reason in cases:
            with self.subTest(name=name):
                runtime, provider, store, topic_id, signed = _runtime_fixture()
                source = runtime.execute(
                    _source_request(f"run-owner-source-{name}", topic_id)
                )
                candidate = _candidate(runtime, source, signed)
                _publish_source(runtime, store, topic_id, source, candidate)
                current_topic_id = topic_id
                if topic_mode == "different_topic":
                    current_topic_id = store.create_topic(
                        "thread-reuse",
                        title="另一分析主题",
                    ).topic_id

                current = runtime.execute(
                    AnalysisRuntimeRequest.create(
                        run_id=f"run-owner-current-{name}",
                        topic_id=current_topic_id,
                        proposal=_proposal(),
                        accepted_graph=("compare_periods",),
                        as_of="2026-06-03T12:00:00+01:00",
                        permission_scope="analyst",
                        reuse_candidates=(mutate(candidate),),
                    )
                )

                self.assertEqual(provider.calls, 2)
                self.assertEqual(current.reuse_decisions[0]["decision"], "rerun")
                self.assertIn(expected_reason, current.reuse_decisions[0]["reason"])


if __name__ == "__main__":
    unittest.main()
