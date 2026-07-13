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
from bi_agent.runtime.evidence_authority import (
    RuntimeEvidenceAuthority,
    canonical_digest,
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

    def test_workflow_replaces_provisional_decision_before_claim_provenance(self):
        from bi_agent.runtime.langgraph_workflow import _fetch_runtime_rows
        from bi_agent.runtime.analysis_contract_compiler import (
            compile_analysis_contract,
        )
        from bi_agent.runtime.dataset_catalog import DatasetCatalog
        from bi_agent.runtime.runtime_contract_registry import (
            RuntimeContractRegistry,
        )

        final_decision = {
            "source_ref": "result:source",
            "result_ref": "result:current",
            "decision": "reuse",
            "reason": "validated_authoritative_query_chain",
            "can_support_claim": True,
            "requires_rerun": False,
        }
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
            [
                {
                    "source_ref": "result:source",
                    "result_ref": "result:current",
                    "decision": "reuse",
                }
            ],
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
