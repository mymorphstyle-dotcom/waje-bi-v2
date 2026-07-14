from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace
import unittest

from bi_agent.conversation.store import InMemoryConversationStore
from bi_agent.runtime.analysis_contracts import analysis_contract_signature
from bi_agent.runtime.analysis_runtime import (
    AnalysisRuntime,
    AnalysisRuntimeRequest,
    AnswerPackageBuildContext,
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


def _publish_source(runtime, store, topic_id, result, candidate):
    analysis_signature = analysis_contract_signature(result.analysis_contract)
    source_request_payload = {
        "context_manifest": {
            "snapshot_version": "2026H1",
            "contract_versions": {"runtime": "contracts-v1"},
        }
    }
    store.upsert_run(
        result.analysis_contract.analysis_contract_id.split(":")[1],
        thread_id="thread-reuse",
        topic_id=topic_id,
        status="completed",
        request=source_request_payload,
    )
    bundle = runtime.build_persistence_bundle(
        result,
        answer_package={"status": "draft", "sections": []},
        request={
            "run_id": result.analysis_contract.analysis_contract_id.split(":")[1],
            "thread_id": "thread-reuse",
            "topic_id": topic_id,
            "permission_context": {"role": "analyst"},
            **source_request_payload,
        },
        artifact_path="artifacts/phase7/source-reuse/answer_package.json",
    )
    store.save_analysis_runtime_records(
        run_id=result.analysis_contract.analysis_contract_id.split(":")[1],
        **bundle,
    )
    source_result = result.query_results[0]
    store.add_result_ref(
        topic_id,
        result_ref=source_result.result_ref,
        snapshot_id="2026H1",
        contract_version="contracts-v1",
        permission_scope="analyst",
        semantic_scope=f"analysis-contract:sha256:{analysis_signature}",
        payload=candidate,
    )


def _candidate(runtime, result, signed_snapshots):
    source_result = result.query_results[0]
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
    return {
        "status": "complete",
        "sections": [
            {
                "section_id": "summary",
                "payload": {
                    "claims": [
                        {
                            "text": text,
                            "claim_type": "comparative_change",
                            "claim_strength": "observed",
                            "evidence_refs": [evidence_ref],
                        }
                    ]
                },
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


def _resigned_physical_decision(decision, **changes):
    payload = {**decision, **changes}
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


def _physical_reuse_bundle_fixture(
    *,
    current_run_id,
    answer_package,
    publication_mode="complete",
):
    runtime, current, request = _physical_reuse_result_fixture(
        current_run_id=current_run_id,
    )
    bundle = runtime.build_persistence_bundle(
        current,
        answer_package=answer_package,
        request=request,
        artifact_path=(
            f"artifacts/phase7/{current_run_id}/answer_package.json"
        ),
        publication_mode=publication_mode,
    )
    return current, request, bundle


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


class AnalysisRuntimeReuseTest(unittest.TestCase):
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
            return {
                "status": "complete",
                "sections": [
                    {
                        "section_id": "summary",
                        "payload": {
                            "claims": [
                                {
                                    "text": text,
                                    "claim_type": "comparative_change",
                                    "claim_strength": "observed",
                                    "evidence_refs": [evidence_ref],
                                }
                            ]
                        },
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
        current, request, bundle = _physical_reuse_bundle_fixture(
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

    def test_waiting_claim_filter_keeps_final_physical_reuse_decision_provenance(self):
        current, request, bundle = _physical_reuse_bundle_fixture(
            current_run_id="run-filtered-claim-reuse",
            answer_package={
                "status": "waiting_for_clarification",
                "sections": [
                    {
                        "section_id": "summary",
                        "payload": {
                            "claims": [
                                {
                                    "text": "等待澄清后再发布。",
                                    "claim_type": "comparative_change",
                                    "claim_strength": "observed",
                                    "evidence_refs": [],
                                }
                            ]
                        },
                    }
                ],
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
                "filtered_claim",
                {
                    "status": "waiting_for_clarification",
                    "sections": [
                        {
                            "section_id": "summary",
                            "payload": {
                                "claims": [
                                    {
                                        "text": "等待澄清后再发布。",
                                        "claim_type": "comparative_change",
                                        "claim_strength": "observed",
                                        "evidence_refs": [],
                                    }
                                ]
                            },
                        }
                    ],
                },
                "waiting_for_clarification",
            ),
        )
        for scenario, answer_package, publication_mode in scenarios:
            with self.subTest(scenario=scenario):
                current, request, bundle = _physical_reuse_bundle_fixture(
                    current_run_id=f"run-{scenario}-authority",
                    answer_package=answer_package,
                    publication_mode=publication_mode,
                )
                expected_decision = dict(current.reuse_decisions[0])

                self.assertEqual(
                    InMemoryConversationStore().save_analysis_runtime_records(
                        run_id=request["run_id"],
                        **bundle,
                    ),
                    "published",
                )
                connection = FakeConnection()
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
                )
                self.assertEqual(
                    projection["reuse_decisions"],
                    [expected_decision],
                )

    def test_claimed_physical_reuse_provenance_reaches_store(self):
        runtime, current, request = _physical_reuse_result_fixture(
            current_run_id="run-claimed-physical-authority",
        )
        bundle = runtime.build_persistence_bundle(
            current,
            answer_package=_physical_claim_package(current),
            request=request,
            artifact_path=(
                "artifacts/phase7/claimed-physical-authority/answer_package.json"
            ),
        )

        self.assertEqual(len(bundle["verified_claims"]), 1)
        self.assertEqual(
            bundle["verified_claims"][0]["reuse_decisions"],
            [dict(current.reuse_decisions[0])],
        )
        self.assertEqual(
            InMemoryConversationStore().save_analysis_runtime_records(
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

        current, request, bundle = _physical_reuse_bundle_fixture(
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
                rerun_bundle = runtime.build_persistence_bundle(
                    current,
                    answer_package={"status": "complete", "sections": []},
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
                self.assertEqual(
                    InMemoryConversationStore().save_analysis_runtime_records(
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
            "reuse_candidate_binding_owner_mismatch",
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
            "result_candidate_analysis_contract_missing",
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
