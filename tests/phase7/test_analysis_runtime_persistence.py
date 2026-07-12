from __future__ import annotations

from dataclasses import replace
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bi_agent.conversation.postgres_store import PostgresConversationStore
from bi_agent.conversation.store import InMemoryConversationStore
from bi_agent.runtime.evidence_authority import (
    EvidenceIntegrityError,
    canonical_value,
)
from tests.phase4.analysis_asset_fixtures import verified_dimension_scan_asset
from tests.phase7.test_conversation_persistence import FakeConnection


def _contains_key(value, key):
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_key(item, key) for item in value)
    return False


def _fixture_content(
    *,
    query_ref="query:channel-scan",
    snapshot_ref="snapshot:paid:1",
    analysis_contract_ref="analysis:asset-fixture:1",
):
    _, content = verified_dimension_scan_asset(
        rows=(
            {"window_id": "target", "period": "2026-06-02", "channel": "app", "amount": 12},
            {"window_id": "baseline", "period": "2026-06-01", "channel": "app", "amount": 10},
        ),
        required_fields=("window_id", "window_role", "observation_key", "paid_amount", "channel"),
        resolved_windows={
            "target": {"start_inclusive": "2026-06-02", "end_exclusive": "2026-06-03", "timezone": "Africa/Lagos"},
            "baseline": {"start_inclusive": "2026-06-01", "end_exclusive": "2026-06-02", "timezone": "Africa/Lagos"},
        },
        query_ref=query_ref,
        snapshot_ref=snapshot_ref,
        analysis_contract_ref=analysis_contract_ref,
    )
    return content


def _authority_bundle(
    *,
    run_id="run-task9",
    thread_id="thread-task9",
    topic_id="topic-task9",
    query_ref="query:channel-scan",
    snapshot_ref="snapshot:paid:1",
    analysis_contract_ref="analysis:asset-fixture:1",
    evidence_ref="evidence:task9:segment",
    repair_attempt_ref="repair:task9:1",
):
    from bi_agent.runtime.analysis_contracts import (
        AnalysisContract,
        analysis_contract_signature,
    )
    from bi_agent.runtime.claim_provenance import (
        build_context_manifest_record,
        build_trusted_claim_provenance_record,
        build_verified_claim_record,
    )
    from bi_agent.runtime.answer_package import _authority_bound_claim_projections
    from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry

    content = _fixture_content(
        query_ref=query_ref,
        snapshot_ref=snapshot_ref,
        analysis_contract_ref=analysis_contract_ref,
    )
    resolver = content["evidence_resolver"]
    release_resolver = content["release_resolver"]
    binding = resolver.resolve_capability_binding(content["binding_manifest_ref"])
    result_refs = (*binding.result_refs, *binding.validation_result_refs)
    query_records = tuple(resolver.resolve_query_execution(ref) for ref in result_refs)
    rows_records = tuple(resolver.resolve_rows(item.rows_ref) for item in query_records)
    completeness_records = tuple(
        resolver.resolve_completeness(ref)
        for ref in (*binding.completeness_record_refs, *binding.validation_completeness_record_refs)
    )
    snapshot_refs = tuple(dict.fromkeys(
        ref for record in query_records for ref in record.source_snapshot_refs
    ))
    snapshot_records = tuple(resolver.resolve_snapshot(ref) for ref in snapshot_refs)
    windows_by_id = {}
    metrics_by_id = {}
    dimensions_by_id = {}
    for record in query_records:
        for window in record.contract.resolved_windows:
            windows_by_id.setdefault(window.window_id, window)
        for metric in record.contract.metric_bindings:
            metrics_by_id.setdefault(metric.metric_id, metric)
        for dimension in record.contract.dimension_bindings:
            dimensions_by_id.setdefault(dimension.dimension_id, dimension)
    analysis_contract = AnalysisContract(
        analysis_contract_id=binding.analysis_contract_ref,
        contract_version="1",
        question_families=("segment_or_factor_attribution",),
        target_metric_refs=tuple(
            metric.contract_ref for metric in metrics_by_id.values()
        ),
        claim_intents=tuple(binding.supported_claim_types),
        scope={"type": "full_sample"},
        business_timezone="Africa/Lagos",
        as_of="2026-06-03T12:00:00+01:00",
        resolved_windows=tuple(windows_by_id.values()),
        metric_bindings=tuple(metrics_by_id.values()),
        dimension_bindings=tuple(dimensions_by_id.values()),
        dataset_requirements=tuple(
            dict.fromkeys(record.snapshot.dataset_id for record in snapshot_records)
        ),
        capability_requirements=(binding.capability_id,),
        permission_scope=query_records[0].contract.permission_scope,
    ).to_dict()
    analysis_contract["contract_signature"] = analysis_contract_signature(
        analysis_contract
    )
    result_refs = list(result_refs)
    completeness_refs = list(
        (*binding.completeness_record_refs, *binding.validation_completeness_record_refs)
    )
    context_manifest = build_context_manifest_record(
        run_id=run_id,
        thread_id=thread_id,
        topic_id=topic_id,
        sources=(
            {"type": "evidence", "ref": evidence_ref, "can_support_claim": True},
            *(
                {"type": "completeness", "ref": ref, "can_support_claim": True}
                for ref in completeness_refs
            ),
        ),
    )
    evidence_manifest = {
        "evidence_ref": evidence_ref,
        "binding_record_ref": binding.record_ref,
        "result_refs": result_refs,
        "completeness_record_refs": completeness_refs,
        "context_manifest_ref": context_manifest["manifest_id"],
    }
    provenance = build_trusted_claim_provenance_record(
        run_id=run_id,
        artifact_refs=("artifact:task9",),
        memory_refs=("memory:task9",),
        reuse_decisions=({"source_ref": "asset:task9", "decision": "reuse"},),
    )
    draft_claim = {
        "text": "目标期渠道贡献有变化。",
        "claim_type": "segment_contribution_or_mix_shift",
        "claim_strength": "observed",
        "evidence_refs": (evidence_ref,),
        "numbers": {"paid_amount": 12.0},
        "fact_selectors": {
            "paid_amount": {
                "result_ref": binding.result_refs[0],
                "window_id": "target",
                "dimensions": {"channel": "app"},
            }
        },
    }
    factual_claims, projection_errors = _authority_bound_claim_projections(
        claims=(draft_claim,),
        accepted_indexes=(0,),
        evidence=(_evidence_for_binding(binding, evidence_ref=evidence_ref),),
        evidence_resolver=resolver,
        rows_loader=resolver.rows_loader,
        runtime_registry=RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        ),
        release_resolver=release_resolver,
    )
    if projection_errors or len(factual_claims) != 1:
        raise AssertionError((projection_errors, factual_claims))
    verified_claim = build_verified_claim_record(
        factual_claims[0],
        run_id=run_id,
        context_manifest=context_manifest,
        evidence_by_ref={evidence_ref: evidence_manifest},
        trusted_provenance=provenance,
    )
    return {
        "analysis_contract": analysis_contract,
        "query_contracts": tuple(record.contract for record in query_records),
        "query_execution_records": query_records,
        "rows_records": rows_records,
        "snapshot_records": snapshot_records,
        "completeness_records": completeness_records,
        "capability_binding_records": (binding,),
        "evidence_manifests": (evidence_manifest,),
        "context_manifests": (context_manifest,),
        "trusted_provenance_records": (provenance,),
        "verified_claims": (verified_claim,),
        "claim_links": ({
            "claim_ref": verified_claim["claim_ref"],
            "evidence_ref": evidence_ref,
            "context_manifest_ref": context_manifest["manifest_id"],
        },),
        "repair_attempts": ({
            "attempt_ref": repair_attempt_ref,
            "failed_signature": "failed-signature",
            "action": "recompile_contract",
            "reason": "query_contract_validation_failed",
        },),
    }


def _evidence_for_binding(binding, *, evidence_ref="evidence:task9:segment"):
    dedupe = lambda values: tuple(dict.fromkeys(values))
    return {
        "evidence_ref": evidence_ref,
        "capability_id": binding.capability_id,
        "analysis_contract_ref": binding.analysis_contract_ref,
        "capability_contract_ref": binding.plan_payload["capability_contract_ref"],
        "query_contract_refs": dedupe((*binding.query_contract_refs, *binding.validation_query_contract_refs)),
        "result_refs": dedupe((*binding.result_refs, *binding.validation_result_refs)),
        "query_execution_record_refs": dedupe((*binding.query_execution_record_refs, *binding.validation_query_execution_record_refs)),
        "query_execution_record_digests": dedupe((*binding.query_execution_record_digests, *binding.validation_query_execution_record_digests)),
        "rows_metadata_record_refs": dedupe((*binding.rows_metadata_record_refs, *binding.validation_rows_metadata_record_refs)),
        "rows_metadata_record_digests": dedupe((*binding.rows_metadata_record_digests, *binding.validation_rows_metadata_record_digests)),
        "completeness_report_refs": dedupe((*binding.completeness_report_refs, *binding.validation_completeness_report_refs)),
        "completeness_record_refs": dedupe((*binding.completeness_record_refs, *binding.validation_completeness_record_refs)),
        "completeness_record_digests": dedupe((*binding.completeness_record_digests, *binding.validation_completeness_record_digests)),
        "source_snapshot_refs": dedupe((*binding.source_snapshot_refs, *binding.validation_source_snapshot_refs)),
        "supported_evidence_types": binding.supported_evidence_types,
        "supported_claim_types": binding.supported_claim_types,
        "maximum_claim_strength": binding.maximum_claim_strength,
        "maximum_claim_strength_rank": binding.maximum_claim_strength_rank,
        "claim_strength_taxonomy_version": binding.claim_strength_taxonomy_version,
        "input_status": binding.status,
        "input_completeness_statuses": binding.input_completeness_statuses,
        "binding_manifest_ref": binding.record_ref,
        "binding_manifest_digest": binding.binding_digest,
        "evidence_type": "statistical_association",
        "strength": "observed",
        "wording_limit": "supported",
        "typed_payload": {"paid_amount": 12.0},
        "limitations": (),
        "artifact_refs": ("artifact:task9",),
        "memory_refs": ("memory:task9",),
    }


def _query_resolver_row(record, *, run_id="run-task9"):
    from bi_agent.runtime.runtime_persistence import authority_record_payload

    analysis = _analysis_envelope_for_contracts(
        record.contract.analysis_contract_ref,
        (record.contract,),
    )
    return {
        "record_ref": record.record_ref,
        "record_digest": record.record_digest,
        "result_ref": record.result_ref,
        "query_contract_ref": record.query_contract_ref,
        "rows_ref": record.rows_ref,
        "payload": json.dumps(authority_record_payload("query_execution", record)),
        "authority_run_id": run_id,
        "run_result_ref": record.result_ref,
        "run_id": run_id,
        "run_query_contract_id": record.query_contract_ref,
        "execution_status": record.execution_status,
        "run_query_hash": record.query_hash,
        "run_rows_ref": record.rows_ref,
        "run_completeness_report_ref": record.completeness_report_ref,
        "result_payload": json.dumps(canonical_value(record.result_payload)),
        "contract_query_id": record.query_contract_ref,
        "contract_run_id": run_id,
        "analysis_contract_id": record.contract.analysis_contract_ref,
        "stored_contract_signature": record.contract_signature,
        "contract_payload": json.dumps(canonical_value(record.query_contract)),
        "analysis_run_id": run_id,
        "stored_analysis_signature": analysis["contract_signature"],
        "analysis_payload": json.dumps(analysis),
        "analysis_run_id_actual": run_id,
        "thread_id": "thread-task9",
        "topic_id": "topic-task9",
    }


def _completeness_resolver_row(record, query_record, *, run_id="run-task9"):
    from bi_agent.runtime.runtime_persistence import authority_record_payload

    query_row = _query_resolver_row(query_record, run_id=run_id)
    report = canonical_value(record.report_payload)
    return {
        "record_ref": record.record_ref,
        "report_ref": record.report_ref,
        "report_digest": record.report_digest,
        "result_ref": record.result_ref,
        "query_contract_ref": record.query_contract_ref,
        "payload": json.dumps(authority_record_payload("completeness", record)),
        "authority_run_id": run_id,
        "stored_completeness_status": report["completeness_status"],
        "stored_analysis_readiness": report["analysis_readiness"],
        "run_id": run_id,
        "run_query_contract_id": query_record.query_contract_ref,
        "run_result_ref": query_record.result_ref,
        "run_completeness_report_ref": query_record.completeness_report_ref,
        "contract_run_id": run_id,
        "contract_query_id": query_record.query_contract_ref,
        "analysis_contract_id": query_record.contract.analysis_contract_ref,
        "stored_contract_signature": query_record.contract_signature,
        "contract_payload": query_row["contract_payload"],
        "analysis_run_id": run_id,
        "stored_analysis_signature": query_row["stored_analysis_signature"],
        "analysis_payload": query_row["analysis_payload"],
        "analysis_run_id_actual": run_id,
    }


def _binding_resolver_row(record, *, run_id="run-task9"):
    from bi_agent.runtime.runtime_persistence import authority_record_payload

    analysis = _analysis_envelope_for_contracts(
        record.analysis_contract_ref,
        (),
        capabilities=(record.capability_id,),
        claim_intents=record.supported_claim_types,
    )
    return {
        "record_ref": record.record_ref,
        "binding_digest": record.binding_digest,
        "capability_id": record.capability_id,
        "claim_strength_taxonomy_version": record.claim_strength_taxonomy_version,
        "maximum_claim_strength_rank": record.maximum_claim_strength_rank,
        "payload": json.dumps(authority_record_payload("capability_binding", record)),
        "authority_run_id": run_id,
        "stored_analysis_contract_id": record.analysis_contract_ref,
        "analysis_run_id": run_id,
        "stored_analysis_signature": analysis["contract_signature"],
        "analysis_payload": json.dumps(analysis),
        "analysis_run_id_actual": run_id,
    }


def _rows_resolver_row(record, *, run_id="run-task9"):
    from bi_agent.runtime.runtime_persistence import authority_record_payload

    return {
        "record_ref": record.record_ref,
        "record_digest": record.record_digest,
        "rows_ref": record.rows_ref,
        "rows_content_hash": record.rows_content_hash,
        "row_count": record.row_count,
        "unique_key_fields": list(record.unique_key_fields),
        "storage_ref": record.storage_ref,
        "payload": json.dumps(authority_record_payload("rows", record)),
        "authority_run_id": run_id,
        "query_rows_ref": record.rows_ref,
        "run_id": run_id,
        "run_rows_ref": record.rows_ref,
    }


def _analysis_envelope_for_contracts(
    analysis_contract_id,
    contracts,
    *,
    capabilities=("segment_contribution",),
    claim_intents=("segment_contribution_or_mix_shift",),
):
    from bi_agent.runtime.analysis_contracts import (
        AnalysisContract,
        analysis_contract_signature,
    )

    windows = {}
    metrics = {}
    dimensions = {}
    for contract in contracts:
        for window in contract.resolved_windows:
            windows.setdefault(window.window_id, window)
        for metric in contract.metric_bindings:
            metrics.setdefault(metric.metric_id, metric)
        for dimension in contract.dimension_bindings:
            dimensions.setdefault(dimension.dimension_id, dimension)
    payload = AnalysisContract(
        analysis_contract_id=analysis_contract_id,
        contract_version="1",
        question_families=("segment_or_factor_attribution",),
        target_metric_refs=tuple(item.contract_ref for item in metrics.values()),
        claim_intents=tuple(claim_intents),
        scope={"type": "full_sample"},
        business_timezone="Africa/Lagos",
        as_of="2026-06-03T12:00:00+01:00",
        resolved_windows=tuple(windows.values()),
        metric_bindings=tuple(metrics.values()),
        dimension_bindings=tuple(dimensions.values()),
        dataset_requirements=tuple(
            dict.fromkeys(
                item.dataset_id
                for contract in contracts
                for item in (*contract.metric_bindings, *contract.dimension_bindings)
            )
        ),
        capability_requirements=tuple(capabilities),
        permission_scope=contracts[0].permission_scope if contracts else "analyst",
    ).to_dict()
    payload["contract_signature"] = analysis_contract_signature(payload)
    return payload


def _use_high_value_claim_ceiling(bundle, *, claim_intents=("candidate_driver",)):
    from bi_agent.runtime.evidence_authority import (
        _deep_freeze,
        canonical_digest,
        canonical_value,
    )
    from bi_agent.runtime.analysis_contracts import analysis_contract_signature
    from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry

    registry = RuntimeContractRegistry.from_path(
        "contracts/runtime/clickhouse-analysis-bindings.yaml"
    )
    capability_id = "high_value_user_contribution"
    capability = registry.capability_inputs(capability_id)
    original = bundle["capability_binding_records"][0]
    plan = canonical_value(original.plan_payload)
    plan.update(
        {
            "capability_id": capability_id,
            "capability_contract_ref": registry.capability_contract_ref(
                capability_id
            ),
            "capability_contract_version": registry.contract_version,
            "capability_contract_signature": registry.capability_contract_signature(
                capability_id
            ),
            "supported_claim_types": capability["supported_claim_types"],
            "supported_evidence_types": capability["supported_evidence_types"],
            "maximum_claim_strength": capability["maximum_claim_strength"],
            "maximum_claim_strength_rank": registry.maximum_claim_strength_rank(
                capability["maximum_claim_strength"]
            ),
            "claim_strength_taxonomy_version": registry.claim_strength_taxonomy_version,
        }
    )
    binding_payload = canonical_value(original.binding_payload)
    binding_payload.update(
        {
            "supported_claim_types": capability["supported_claim_types"],
            "supported_evidence_types": capability["supported_evidence_types"],
            "maximum_claim_strength": capability["maximum_claim_strength"],
            "maximum_claim_strength_rank": registry.maximum_claim_strength_rank(
                capability["maximum_claim_strength"]
            ),
            "claim_strength_taxonomy_version": registry.claim_strength_taxonomy_version,
        }
    )
    digest = canonical_digest({"plan": plan, "binding": binding_payload})
    binding = replace(
        original,
        record_ref=f"capability-binding:{capability_id}:{digest}",
        binding_digest=digest,
        capability_id=capability_id,
        capability_contract_version=registry.contract_version,
        capability_contract_signature=registry.capability_contract_signature(
            capability_id
        ),
        supported_claim_types=tuple(capability["supported_claim_types"]),
        supported_evidence_types=tuple(capability["supported_evidence_types"]),
        maximum_claim_strength=capability["maximum_claim_strength"],
        maximum_claim_strength_rank=registry.maximum_claim_strength_rank(
            capability["maximum_claim_strength"]
        ),
        claim_strength_taxonomy_version=registry.claim_strength_taxonomy_version,
        plan_payload=_deep_freeze(plan),
        binding_payload=_deep_freeze(binding_payload),
    )
    bundle["capability_binding_records"] = (binding,)
    bundle["evidence_manifests"] = tuple(
        {**item, "binding_record_ref": binding.record_ref}
        for item in bundle["evidence_manifests"]
    )
    analysis = {
        **bundle["analysis_contract"],
        "capability_requirements": [capability_id],
        "claim_intents": list(claim_intents),
    }
    analysis["contract_signature"] = analysis_contract_signature(analysis)
    bundle["analysis_contract"] = analysis
    return binding


class AnalysisRuntimePersistenceTest(unittest.TestCase):
    def test_descriptive_relative_day_baselines_are_canonicalized(self):
        from bi_agent.runtime.langgraph_workflow import _canonical_baselines

        self.assertEqual(
            _canonical_baselines(
                (
                    "前一日（前天）的收入值",
                    "与前天相比",
                    "前一个完整业务日",
                )
            ),
            ("previous_day",),
        )

    def test_event_claim_projection_and_client_provenance_ignore_raw_injection(self):
        from bi_agent.runtime.answer_package import (
            build_answer_package,
            reverify_answer_package_for_delivery,
        )
        from bi_agent.runtime.capability_execution import bind_capability_inputs
        from bi_agent.runtime.claim_provenance import (
            build_trusted_claim_provenance_record,
        )
        from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry
        from tests.phase4.test_authoritative_query_chain import (
            _event_authority_context,
            _evidence_from_bound_dashboard_input,
        )

        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        context = _event_authority_context(registry)
        bound = bind_capability_inputs(
            context["plan"],
            results={context["contract"].query_contract_id: context["result"]},
            reports={context["contract"].query_contract_id: context["report"]},
            evidence_authority=context["authority"],
            runtime_registry=registry,
            release_resolver=context["release_resolver"],
        )
        evidence = _evidence_from_bound_dashboard_input(bound)
        evidence.update(
            {
                "evidence_type": bound.supported_evidence_types[0],
                "strength": "context_only",
                "typed_payload": {"events": [{"event_id": "forged-event"}]},
            }
        )
        claim = {
            "text": "Forged event caused the outcome.",
            "claim_strength": "context_only",
            "claim_type": "candidate_mechanism",
            "evidence_refs": (evidence["evidence_ref"],),
            "artifact_refs": ("artifact:forged",),
            "memory_refs": ("memory:forged",),
            "context_manifest_ref": "context:forged",
        }
        trusted = build_trusted_claim_provenance_record(
            run_id="run-task9-event",
            artifact_refs=("artifact:event-reviewed",),
        )
        package = build_answer_package(
            run_id="run-task9-event",
            draft_claims=(claim,),
            evidence=(evidence,),
            evidence_resolver=context["authority"],
            rows_loader=context["authority"].rows_loader,
            runtime_registry=registry,
            release_resolver=context["release_resolver"],
            checkpoint_events=(), proposed_graph=(), accepted_graph=(),
            rejected_or_degraded_mutations=(), validator_results=(),
            sql_text="", sql_hash="e" * 64, artifact_audit={},
            answer_text="Authoritative event context was observed.",
            final_business_summary="Authoritative event context was observed.",
            context_manifest={"thread_id": "thread-event", "topic_id": "topic-event"},
            trusted_claim_provenance_records=(trusted,),
        )
        self.assertIn(
            package["admin_audit"]["verifier"]["status"],
            {"passed", "passed_with_warnings"},
            package["admin_audit"]["verifier"],
        )
        internal_claim = package["sections"][0]["payload"]["claims"][0]
        self.assertNotIn("forged", str(internal_claim).lower())
        self.assertEqual(internal_claim["artifact_refs"], ["artifact:event-reviewed"])
        client = reverify_answer_package_for_delivery(
            package,
            evidence_resolver=context["authority"],
            rows_loader=context["authority"].rows_loader,
            runtime_registry=registry,
            release_resolver=context["release_resolver"],
        )
        self.assertTrue(
            client["sections"][0]["payload"]["claims"], client["admin_audit"]
        )
        client_claim = client["sections"][0]["payload"]["claims"][0]
        self.assertEqual(client_claim["claim_ref"], internal_claim["claim_ref"])
        self.assertNotIn("forged", str(client_claim).lower())

    def test_common_claim_provenance_drops_untrusted_raw_refs(self):
        from bi_agent.runtime.claim_provenance import (
            build_context_manifest_record,
            build_trusted_claim_provenance_record,
            build_verified_claim_record,
        )

        evidence = {
            "evidence:accepted": {
                "evidence_ref": "evidence:accepted",
                "result_refs": ("result:accepted",),
                "completeness_record_refs": ("complete:accepted",),
            }
        }
        context = build_context_manifest_record(
            run_id="run-claim",
            thread_id="thread-claim",
            topic_id="topic-claim",
            sources=(
                {"type": "evidence", "ref": "evidence:accepted", "can_support_claim": True},
                {"type": "completeness", "ref": "complete:accepted", "can_support_claim": True},
            ),
        )
        trusted = build_trusted_claim_provenance_record(
            run_id="run-claim",
            artifact_refs=("artifact:trusted",),
            memory_refs=("memory:trusted",),
            reuse_decisions=({"source_ref": "asset:trusted", "decision": "reuse"},),
        )
        record = build_verified_claim_record(
            {
                "text": "authoritative factual projection",
                "claim_type": "comparative_change",
                "claim_strength": "observed",
                "evidence_refs": ("evidence:accepted",),
                "result_refs": ("result:forged",),
                "artifact_refs": ("artifact:forged",),
                "memory_refs": ("memory:forged",),
                "reuse_decisions": ({"source_ref": "asset:forged", "decision": "reuse"},),
                "context_manifest_ref": "context:forged",
                "claim_ref": "claim:forged",
                "claim_id": "claim-id:forged",
            },
            run_id="run-claim",
            context_manifest=context,
            evidence_by_ref=evidence,
            trusted_provenance=trusted,
        )
        self.assertEqual(record["result_refs"], ["result:accepted"])
        self.assertEqual(record["artifact_refs"], ["artifact:trusted"])
        self.assertEqual(record["memory_refs"], ["memory:trusted"])
        self.assertEqual(record["context_manifest_ref"], context["manifest_id"])
        self.assertNotIn("forged", str(record))

    def test_analysis_contract_rejects_well_shaped_but_fake_signature(self):
        bundle = _authority_bundle()
        bundle["analysis_contract"] = {
            **bundle["analysis_contract"],
            "contract_signature": "f" * 64,
        }
        connection = FakeConnection()
        with self.assertRaisesRegex(
            EvidenceIntegrityError, "runtime_persistence_analysis_contract_signature_invalid"
        ):
            PostgresConversationStore(connection).save_analysis_runtime_records(
                run_id="run-task9", **bundle
            )
        self.assertEqual(connection.statements, [])

    def test_full_analysis_contract_round_trips_through_strict_typed_parser(self):
        from bi_agent.runtime.analysis_contracts import analysis_contract_from_dict

        payload = _authority_bundle()["analysis_contract"]
        parsed = analysis_contract_from_dict(
            {key: value for key, value in payload.items() if key != "contract_signature"}
        )

        self.assertEqual(parsed.to_dict(), {
            key: value for key, value in payload.items() if key != "contract_signature"
        })

    def test_analysis_contract_allows_same_metric_across_distinct_datasets(self):
        from bi_agent.runtime.analysis_contracts import analysis_contract_from_dict

        payload = {
            key: value
            for key, value in _authority_bundle()["analysis_contract"].items()
            if key != "contract_signature"
        }
        primary = dict(payload["metric_bindings"][0])
        secondary = {
            **primary,
            "dataset_id": "market_dashboard",
            "contract_ref": "contracts/sources/market-dashboard.source.yaml@0.2",
            "expression": "sum(paid_amount)",
        }
        primary_dimension = dict(payload["dimension_bindings"][0])
        secondary_dimension = {
            **primary_dimension,
            "dataset_id": "market_dashboard",
            "contract_ref": "contracts/sources/market-dashboard.source.yaml@0.2",
        }
        payload["metric_bindings"] = [primary, secondary]
        payload["dimension_bindings"] = [
            primary_dimension,
            secondary_dimension,
        ]
        payload["dataset_requirements"] = [
            *payload["dataset_requirements"],
            "market_dashboard",
        ]

        parsed = analysis_contract_from_dict(payload)

        self.assertEqual(
            [(item.metric_id, item.dataset_id) for item in parsed.metric_bindings],
            [
                (primary["metric_id"], primary["dataset_id"]),
                (primary["metric_id"], "market_dashboard"),
            ],
        )
        self.assertEqual(
            [
                (item.dimension_id, item.dataset_id)
                for item in parsed.dimension_bindings
            ],
            [
                (primary_dimension["dimension_id"], primary_dimension["dataset_id"]),
                (primary_dimension["dimension_id"], "market_dashboard"),
            ],
        )

    def test_analysis_contract_rejects_duplicate_binding_within_one_dataset(self):
        from bi_agent.runtime.analysis_contracts import analysis_contract_from_dict

        payload = {
            key: value
            for key, value in _authority_bundle()["analysis_contract"].items()
            if key != "contract_signature"
        }
        payload["metric_bindings"] = [
            payload["metric_bindings"][0],
            dict(payload["metric_bindings"][0]),
        ]

        with self.assertRaisesRegex(
            ValueError, "analysis_contract.metric_bindings:duplicate"
        ):
            analysis_contract_from_dict(payload)

    def test_each_capability_binding_validates_only_its_query_subset(self):
        from bi_agent.runtime.analysis_contracts import analysis_contract_from_dict
        from bi_agent.runtime.runtime_persistence import (
            _validate_capability_binding_analysis_closure,
        )

        bundle = _authority_bundle()
        analysis = analysis_contract_from_dict(
            {
                key: value
                for key, value in bundle["analysis_contract"].items()
                if key != "contract_signature"
            }
        )
        own_query = bundle["query_contracts"][0]
        unrelated_query = replace(
            own_query,
            query_contract_id="query:unrelated-capability:1",
            query_role_ref="query-role:unrelated-capability:1",
        )

        _validate_capability_binding_analysis_closure(
            analysis,
            bundle["capability_binding_records"][0],
            {
                **{
                    query.query_contract_id: query
                    for query in bundle["query_contracts"]
                },
                unrelated_query.query_contract_id: unrelated_query,
            },
        )

    def test_analysis_contract_envelope_rejects_shape_and_nested_drift(self):
        from bi_agent.runtime.analysis_contracts import analysis_contract_signature

        mutations = {
            "id_only": lambda payload: {
                "analysis_contract_id": payload["analysis_contract_id"]
            },
            "unknown_key": lambda payload: {**payload, "unknown": "drift"},
            "missing_key": lambda payload: {
                key: value for key, value in payload.items()
                if key != "question_families"
            },
            "metric_nested_drift": lambda payload: {
                **payload,
                "metric_bindings": [
                    {**payload["metric_bindings"][0], "expression": "sum(forged)"},
                    *payload["metric_bindings"][1:],
                ],
            },
            "metric_nested_unknown": lambda payload: {
                **payload,
                "metric_bindings": [
                    {**payload["metric_bindings"][0], "unknown": "drift"},
                    *payload["metric_bindings"][1:],
                ],
            },
            "metric_nested_type": lambda payload: {
                **payload,
                "metric_bindings": [
                    {
                        **payload["metric_bindings"][0],
                        "reconciliation_tolerance": 0,
                    },
                    *payload["metric_bindings"][1:],
                ],
            },
            "window_nested_missing": lambda payload: {
                **payload,
                "resolved_windows": [
                    {
                        key: value
                        for key, value in payload["resolved_windows"][0].items()
                        if key != "timezone"
                    },
                    *payload["resolved_windows"][1:],
                ],
            },
            "window_boundary": lambda payload: {
                **payload,
                "resolved_windows": [
                    {
                        **payload["resolved_windows"][0],
                        "start_inclusive": "2026-01-01",
                    },
                    *payload["resolved_windows"][1:],
                ],
            },
            "dimension_boundary": lambda payload: {
                **payload,
                "dimension_bindings": [
                    {
                        **payload["dimension_bindings"][0],
                        "source_field": "forged_dimension",
                    },
                    *payload["dimension_bindings"][1:],
                ],
            },
            "dataset_boundary": lambda payload: {
                **payload,
                "dataset_requirements": ["other_dataset"],
            },
            "permission_boundary": lambda payload: {
                **payload,
                "permission_scope": "admin",
            },
            "capability_boundary": lambda payload: {
                **payload,
                "capability_requirements": ["other_capability"],
            },
            "claim_boundary": lambda payload: {
                **payload,
                "claim_intents": ["causal_effect"],
            },
        }
        for case_id, mutate in mutations.items():
            with self.subTest(case_id=case_id):
                bundle = _authority_bundle()
                changed = mutate(deepcopy(bundle["analysis_contract"]))
                changed["contract_signature"] = analysis_contract_signature(changed)
                bundle["analysis_contract"] = changed
                with self.assertRaises(EvidenceIntegrityError):
                    InMemoryConversationStore().save_analysis_runtime_records(
                        run_id="run-task9", **bundle
                    )

    def test_inmemory_audit_append_failure_leaves_no_runtime_state(self):
        class FailingAuditList(list):
            def append(self, value):
                raise RuntimeError("injected_audit_append_failure")

        store = InMemoryConversationStore()
        store._audit_events = FailingAuditList()

        with self.assertRaisesRegex(RuntimeError, "injected_audit_append_failure"):
            store.save_analysis_runtime_records(
                run_id="run-task9", **_authority_bundle()
            )

        self.assertEqual(store.analysis_runtime_records, {})
        self.assertEqual(dict(store.analysis_runtime_authority), {})
        self.assertEqual(store.audit_events, [])

    def test_zero_claim_run_persists_authority_without_claim_context(self):
        bundle = _authority_bundle()
        bundle["context_manifests"] = ()
        bundle["trusted_provenance_records"] = ()
        bundle["verified_claims"] = ()
        bundle["claim_links"] = ()
        bundle["evidence_manifests"] = tuple(
            {**item, "context_manifest_ref": ""}
            for item in bundle["evidence_manifests"]
        )
        store = InMemoryConversationStore()

        self.assertEqual(
            store.save_analysis_runtime_records(run_id="run-task9", **bundle),
            "published",
        )
        self.assertEqual(store.analysis_runtime_authority["verified_claim"], {})
        self.assertEqual(store.analysis_runtime_authority["claim_evidence_link"], {})
        self.assertEqual(
            store.audit_events[-1]["event_type"],
            "analysis_runtime_records_persisted",
        )

    def test_preexecution_clarification_persists_metric_backed_unbound_intent(self):
        from bi_agent.runtime.analysis_contracts import analysis_contract_signature

        bundle = _authority_bundle()
        claim_intent = bundle["analysis_contract"]["claim_intents"][0]
        analysis = {
            **bundle["analysis_contract"],
            "contract_gaps": [{
                "gap_type": "contract_partial",
                "gap_id": "window:unsupported_baseline:business_choice",
                "dataset_id": "",
                "affected_capabilities": list(
                    bundle["analysis_contract"]["capability_requirements"]
                ),
                "affected_claim_types": [claim_intent],
                "owner": "contract_owner",
                "repair_options": [
                    "choose_supported_window",
                    "clarify_window_contract",
                ],
                "requires_clarification": True,
                "diagnostic_context": {},
            }],
        }
        analysis["contract_signature"] = analysis_contract_signature(analysis)
        bundle.update(
            {
                "analysis_contract": analysis,
                "query_contracts": (),
                "query_execution_records": (),
                "rows_records": (),
                "snapshot_records": (),
                "completeness_records": (),
                "capability_binding_records": (),
                "evidence_manifests": (),
                "context_manifests": (),
                "trusted_provenance_records": (),
                "verified_claims": (),
                "claim_links": (),
                "repair_attempts": (),
            }
        )

        self.assertEqual(
            InMemoryConversationStore().save_analysis_runtime_records(
                run_id="run-task9", **bundle
            ),
            "published",
        )

    def test_source_unbound_terminal_boundary_persists_zero_claim_contract(self):
        from bi_agent.runtime.analysis_contracts import analysis_contract_signature

        bundle = _authority_bundle()
        claim_intent = bundle["analysis_contract"]["claim_intents"][0]
        boundary_claim_intent = "contract_coverage_and_trust_boundary"
        analysis = {
            **bundle["analysis_contract"],
            "claim_intents": [claim_intent, boundary_claim_intent],
            "capability_requirements": [
                *bundle["analysis_contract"]["capability_requirements"],
                "data_quality_profile",
            ],
            "contract_gaps": [{
                "gap_type": "source_unbound",
                "gap_id": "dataset:paid_order_success:source_unbound",
                "dataset_id": "paid_order_success",
                "affected_capabilities": [
                    *bundle["analysis_contract"]["capability_requirements"],
                    "data_quality_profile",
                ],
                "affected_claim_types": [claim_intent, boundary_claim_intent],
                "owner": "data_owner",
                "repair_options": ["register_dataset_snapshot", "bind_source"],
                "requires_clarification": False,
                "diagnostic_context": {},
            }],
        }
        analysis["contract_signature"] = analysis_contract_signature(analysis)
        bundle.update(
            {
                "analysis_contract": analysis,
                "query_contracts": (),
                "query_execution_records": (),
                "rows_records": (),
                "snapshot_records": (),
                "completeness_records": (),
                "capability_binding_records": (),
                "evidence_manifests": (),
                "context_manifests": (),
                "trusted_provenance_records": (),
                "verified_claims": (),
                "claim_links": (),
                "repair_attempts": (),
            }
        )

        self.assertEqual(
            InMemoryConversationStore().save_analysis_runtime_records(
                run_id="run-task9", **bundle
            ),
            "published",
        )

    def test_ready_claim_persists_when_omitted_sibling_has_separate_claim_chain(self):
        from bi_agent.runtime.analysis_contracts import analysis_contract_signature

        bundle = _authority_bundle()
        ready_claim_type = bundle["verified_claims"][0]["claim_type"]
        omitted_claim_type = "formula_component_contribution"
        analysis = {
            **bundle["analysis_contract"],
            "claim_intents": [ready_claim_type, omitted_claim_type],
            "capability_requirements": [
                *bundle["analysis_contract"]["capability_requirements"],
                "formula_decompose",
            ],
            "contract_gaps": [{
                "gap_type": "source_unbound",
                "gap_id": "dataset:paid_order_success:source_unbound",
                "dataset_id": "paid_order_success",
                "affected_capabilities": ["formula_decompose"],
                "affected_claim_types": [omitted_claim_type],
                "owner": "data_owner",
                "repair_options": ["register_dataset_snapshot", "bind_source"],
                "requires_clarification": True,
                "diagnostic_context": {},
            }],
        }
        analysis["contract_signature"] = analysis_contract_signature(analysis)
        bundle["analysis_contract"] = analysis

        self.assertEqual(
            InMemoryConversationStore().save_analysis_runtime_records(
                run_id="run-task9", **bundle
            ),
            "published",
        )

    def test_overlapping_claim_label_does_not_cross_authorize_omitted_capability(self):
        from bi_agent.runtime.analysis_contracts import analysis_contract_signature

        bundle = _authority_bundle()
        claim_type = bundle["verified_claims"][0]["claim_type"]
        analysis = {
            **bundle["analysis_contract"],
            "capability_requirements": [
                *bundle["analysis_contract"]["capability_requirements"],
                "formula_decompose",
            ],
            "contract_gaps": [{
                "gap_type": "source_unbound",
                "gap_id": "dataset:paid_order_success:source_unbound",
                "dataset_id": "paid_order_success",
                "affected_capabilities": ["formula_decompose"],
                "affected_claim_types": [claim_type],
                "owner": "data_owner",
                "repair_options": ["register_dataset_snapshot", "bind_source"],
                "requires_clarification": True,
                "diagnostic_context": {},
            }],
        }
        analysis["contract_signature"] = analysis_contract_signature(analysis)
        bundle["analysis_contract"] = analysis

        self.assertEqual(
            InMemoryConversationStore().save_analysis_runtime_records(
                run_id="run-task9", **bundle
            ),
            "published",
        )

    def test_global_or_ready_capability_gap_blocks_ready_claim(self):
        from bi_agent.runtime.analysis_contracts import analysis_contract_signature

        for affected_kind in ("global", "ready"):
            bundle = _authority_bundle()
            affected = (
                []
                if affected_kind == "global"
                else [bundle["analysis_contract"]["capability_requirements"][0]]
            )
            with self.subTest(affected=affected):
                claim_type = bundle["verified_claims"][0]["claim_type"]
                analysis = {
                    **bundle["analysis_contract"],
                    "contract_gaps": [{
                        "gap_type": "contract_partial",
                        "gap_id": "claim_authority:incomplete",
                        "dataset_id": "",
                        "affected_capabilities": affected,
                        "affected_claim_types": [claim_type],
                        "owner": "contract_owner",
                        "repair_options": ["bind_capability_claim_types"],
                        "requires_clarification": True,
                        "diagnostic_context": {},
                    }],
                }
                analysis["contract_signature"] = analysis_contract_signature(analysis)
                bundle["analysis_contract"] = analysis

                with self.assertRaisesRegex(
                    EvidenceIntegrityError,
                    "runtime_persistence_verified_claim_gap_blocked",
                ):
                    InMemoryConversationStore().save_analysis_runtime_records(
                        run_id="run-task9", **bundle
                    )

    def test_queryless_contract_capability_persists_zero_claim_boundary(self):
        from bi_agent.runtime.analysis_contracts import analysis_contract_signature

        bundle = _authority_bundle()
        analysis = {
            **bundle["analysis_contract"],
            "claim_intents": ["recurring_pattern_existence"],
            "capability_requirements": ["answer_verify", "evidence_reduce"],
            "dataset_requirements": [],
            "metric_bindings": [],
            "dimension_bindings": [],
            "contract_gaps": [{
                "gap_type": "contract_partial",
                "gap_id": "metric:paid_amount:source_ambiguous",
                "dataset_id": "",
                "affected_capabilities": ["analysis_contract"],
                "affected_claim_types": [],
                "owner": "contract_owner",
                "repair_options": [
                    "select_dataset_requirement",
                    "clarify_source_scope",
                ],
                "requires_clarification": True,
                "diagnostic_context": {},
            }],
        }
        analysis["contract_signature"] = analysis_contract_signature(analysis)
        bundle.update(
            {
                "analysis_contract": analysis,
                "query_contracts": (),
                "query_execution_records": (),
                "rows_records": (),
                "snapshot_records": (),
                "completeness_records": (),
                "capability_binding_records": (),
                "evidence_manifests": (),
                "context_manifests": (),
                "trusted_provenance_records": (),
                "verified_claims": (),
                "claim_links": (),
                "repair_attempts": (),
            }
        )

        self.assertEqual(
            InMemoryConversationStore().save_analysis_runtime_records(
                run_id="run-task9", **bundle
            ),
            "published",
        )

    def test_queryless_contract_capability_without_boundary_is_unsupported(self):
        from bi_agent.runtime.analysis_contracts import analysis_contract_signature

        bundle = _authority_bundle()
        analysis = {
            **bundle["analysis_contract"],
            "claim_intents": ["recurring_pattern_existence"],
            "capability_requirements": ["answer_verify", "evidence_reduce"],
            "dataset_requirements": [],
            "metric_bindings": [],
            "dimension_bindings": [],
            "contract_gaps": [],
        }
        analysis["contract_signature"] = analysis_contract_signature(analysis)
        bundle.update(
            {
                "analysis_contract": analysis,
                "query_contracts": (),
                "query_execution_records": (),
                "rows_records": (),
                "snapshot_records": (),
                "completeness_records": (),
                "capability_binding_records": (),
                "evidence_manifests": (),
                "context_manifests": (),
                "trusted_provenance_records": (),
                "verified_claims": (),
                "claim_links": (),
                "repair_attempts": (),
            }
        )

        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "runtime_persistence_analysis_claim_intent_unsupported",
        ):
            InMemoryConversationStore().save_analysis_runtime_records(
                run_id="run-task9", **bundle
            )

    def test_metric_backed_intent_persists_with_structured_terminal_boundary(self):
        from bi_agent.runtime.analysis_contracts import analysis_contract_signature
        from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry

        bundle = _authority_bundle()
        claim_intents = list(
            bundle["analysis_contract"]["metric_bindings"][0]["claim_types"]
        )
        metric_id = bundle["analysis_contract"]["metric_bindings"][0]["metric_id"]
        analysis = {
            **bundle["analysis_contract"],
            "claim_intents": claim_intents,
            "capability_requirements": ["answer_verify"],
            "contract_gaps": [{
                "gap_type": "contract_partial",
                "gap_id": f"metric:{metric_id}:source_ambiguous",
                "dataset_id": "",
                "affected_capabilities": ["analysis_contract"],
                "affected_claim_types": claim_intents,
                "owner": "contract_owner",
                "repair_options": [
                    "select_dataset_requirement",
                    "clarify_source_scope",
                ],
                "requires_clarification": True,
                "diagnostic_context": {},
            }],
        }
        analysis["contract_signature"] = analysis_contract_signature(analysis)
        bundle.update(
            {
                "analysis_contract": analysis,
                "query_contracts": (),
                "query_execution_records": (),
                "rows_records": (),
                "snapshot_records": (),
                "completeness_records": (),
                "capability_binding_records": (),
                "evidence_manifests": (),
                "context_manifests": (),
                "trusted_provenance_records": (),
                "verified_claims": (),
                "claim_links": (),
                "repair_attempts": (),
            }
        )

        self.assertEqual(
            InMemoryConversationStore().save_analysis_runtime_records(
                run_id="run-task9", **bundle
            ),
            "published",
        )
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        bound_metric_id = "active_users"
        bound_source = registry.metric_sources(bound_metric_id)["market_dashboard"]
        active_binding = {
            **analysis["metric_bindings"][0],
            **{
                key: value
                for key, value in bound_source.items()
                if key in analysis["metric_bindings"][0]
            },
            "metric_id": bound_metric_id,
        }
        unbound_metric_id = "paid_amount"
        unbound_sources = registry.metric_sources(unbound_metric_id)
        linked_claim_intents = ["comparative_change", "source_reconciliation"]
        compiler_linked_analysis = {
            **analysis,
            "claim_intents": linked_claim_intents,
            "metric_bindings": [active_binding],
            "target_metric_refs": [
                active_binding["contract_ref"],
                *(
                    source["contract_ref"]
                    for source in unbound_sources.values()
                ),
            ],
            "contract_gaps": [{
                **analysis["contract_gaps"][0],
                "gap_id": (
                    f"metric:{unbound_metric_id}:source_ambiguous:"
                    f"{','.join(unbound_sources)}"
                ),
                "affected_claim_types": linked_claim_intents,
                "diagnostic_context": {
                    "item_kind": "metric",
                    "item_id": unbound_metric_id,
                    "claim_intents": linked_claim_intents,
                },
            }],
        }
        compiler_linked_analysis["contract_signature"] = analysis_contract_signature(
            compiler_linked_analysis
        )
        bundle["analysis_contract"] = compiler_linked_analysis
        self.assertEqual(
            InMemoryConversationStore().save_analysis_runtime_records(
                run_id="run-task9-compiler-linked", **bundle
            ),
            "published",
        )
        unbounded_analysis = {
            **analysis,
            "contract_gaps": [],
        }
        unbounded_analysis["contract_signature"] = analysis_contract_signature(
            unbounded_analysis
        )
        bundle["analysis_contract"] = unbounded_analysis
        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "runtime_persistence_analysis_claim_intent_unsupported",
        ):
            InMemoryConversationStore().save_analysis_runtime_records(
                run_id="run-task9-unbounded", **bundle
            )
        for gap in (
            {
                **analysis["contract_gaps"][0],
                "gap_id": "metric:unrelated_metric:source_ambiguous",
                "affected_claim_types": claim_intents,
                "diagnostic_context": {
                    "item_kind": "metric",
                    "item_id": "unrelated_metric",
                    "claim_intents": claim_intents,
                },
            },
            {
                **analysis["contract_gaps"][0],
                "gap_id": "dataset:unrelated_dataset:source_unbound",
                "gap_type": "source_unbound",
                "dataset_id": "unrelated_dataset",
                "affected_claim_types": claim_intents,
            },
        ):
            unrelated_analysis = {
                **analysis,
                "contract_gaps": [gap],
            }
            unrelated_analysis["contract_signature"] = analysis_contract_signature(
                unrelated_analysis
            )
            bundle["analysis_contract"] = unrelated_analysis
            with self.assertRaisesRegex(
                EvidenceIntegrityError,
                "runtime_persistence_analysis_claim_intent_unsupported",
            ):
                InMemoryConversationStore().save_analysis_runtime_records(
                    run_id=f"run-task9-{gap['gap_id']}", **bundle
                )

    def test_zero_claim_postgres_run_publishes_and_replays_without_claim_rows(self):
        bundle = _authority_bundle()
        bundle["context_manifests"] = ()
        bundle["trusted_provenance_records"] = ()
        bundle["verified_claims"] = ()
        bundle["claim_links"] = ()
        bundle["evidence_manifests"] = tuple(
            {**item, "context_manifest_ref": ""}
            for item in bundle["evidence_manifests"]
        )
        connection = FakeConnection()
        store = PostgresConversationStore(connection)

        self.assertEqual(
            store.save_analysis_runtime_records(run_id="run-task9", **bundle),
            "published",
        )
        self.assertEqual(
            store.save_analysis_runtime_records(run_id="run-task9", **bundle),
            "replayed",
        )
        self.assertEqual(connection.commits, 1)
        self.assertEqual(
            sum(
                params.get("event_type") == "analysis_runtime_records_persisted"
                for _, params in connection.statements
            ),
            1,
        )

    def test_zero_claim_client_package_keeps_typed_degradation_without_persistence_failure(self):
        from bi_agent.runtime.answer_package import build_answer_package

        package = build_answer_package(
            run_id="run-task9-zero",
            draft_claims=(),
            evidence=(),
            checkpoint_events=(),
            proposed_graph=(),
            accepted_graph=(),
            rejected_or_degraded_mutations=({"code": "no_verified_claims"},),
            validator_results=(),
            sql_text="",
            sql_hash="",
            artifact_audit={},
        )

        self.assertEqual(package["sections"][0]["payload"]["claims"], [])
        self.assertEqual(
            package["rejected_or_degraded_mutations"],
            [{"code": "no_verified_claims"}],
        )
        self.assertNotIn("persistence_failed", str(package))

    def test_forged_high_value_plan_with_dimension_scan_is_rejected(self):
        bundle = _authority_bundle()
        _use_high_value_claim_ceiling(bundle, claim_intents=("candidate_driver",))
        bundle["context_manifests"] = ()
        bundle["trusted_provenance_records"] = ()
        bundle["verified_claims"] = ()
        bundle["claim_links"] = ()
        bundle["evidence_manifests"] = tuple(
            {**item, "context_manifest_ref": ""}
            for item in bundle["evidence_manifests"]
        )

        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "runtime_persistence_binding_plan_semantics_invalid",
        ):
            InMemoryConversationStore().save_analysis_runtime_records(
                run_id="run-task9", **bundle
            )

    def test_task3_high_value_compiler_plan_passes_shared_semantic_validator(self):
        from datetime import datetime
        from bi_agent.runtime.analysis_contract_compiler import compile_analysis_contract
        from bi_agent.runtime.authoritative_query_chain import (
            validate_capability_plan_semantics,
        )
        from bi_agent.runtime.dataset_catalog import DatasetCatalog
        from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry
        from tests.phase4.test_analysis_contract_compiler import snapshot

        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        outcome = compile_analysis_contract(
            run_id="run-task9-high-value-plan",
            proposal={
                "target_metrics": ["paid_amount"],
                "claim_intents": ["candidate_driver"],
            },
            accepted_capabilities=("high_value_user_contribution",),
            catalog=DatasetCatalog(
                (snapshot("paid_order_success", "paid", "2026-07-04"),)
            ),
            registry=registry,
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            permission_scope="analyst",
        )
        plan = outcome.capability_plans[0]

        self.assertEqual(outcome.analysis_contract.claim_intents, ("candidate_driver",))
        self.assertEqual(
            plan.supported_claim_types,
            ("candidate_driver", "concentration"),
        )
        validate_capability_plan_semantics(
            plan,
            registry,
            {item.query_contract_id: item for item in outcome.query_contracts},
        )

    def test_verified_claim_must_fit_analysis_intent_and_linked_binding_ceiling(self):
        from bi_agent.runtime.analysis_contracts import analysis_contract_from_dict
        from bi_agent.runtime.claim_provenance import build_verified_claim_record
        from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry
        from bi_agent.runtime.runtime_persistence import (
            _validate_verified_claim_contract_boundary,
        )

        bundle = _authority_bundle()
        _use_high_value_claim_ceiling(bundle, claim_intents=("candidate_driver",))
        original = bundle["verified_claims"][0]
        factual = {
            key: value
            for key, value in original.items()
            if key not in {
                "claim_ref", "claim_digest", "run_id", "context_manifest_ref",
                "result_refs", "completeness_record_refs", "artifact_refs",
                "memory_refs", "reuse_decisions", "provenance_record_ref",
            }
        }
        factual["claim_type"] = "concentration"
        claim = build_verified_claim_record(
            factual,
            run_id="run-task9",
            context_manifest=bundle["context_manifests"][0],
            evidence_by_ref={
                item["evidence_ref"]: item for item in bundle["evidence_manifests"]
            },
            trusted_provenance=bundle["trusted_provenance_records"][0],
        )
        bundle["verified_claims"] = (claim,)
        bundle["claim_links"] = ({
            "claim_ref": claim["claim_ref"],
            "evidence_ref": claim["evidence_refs"][0],
            "context_manifest_ref": claim["context_manifest_ref"],
        },)

        with self.assertRaisesRegex(
            EvidenceIntegrityError, "runtime_persistence_verified_claim_intent_mismatch"
        ):
            _validate_verified_claim_contract_boundary(
                claim,
                analysis=analysis_contract_from_dict({
                    key: value
                    for key, value in bundle["analysis_contract"].items()
                    if key != "contract_signature"
                }),
                unbound_claim_intents=set(),
                evidence_by_ref={
                    item["evidence_ref"]: item
                    for item in bundle["evidence_manifests"]
                },
                bindings_by_ref={
                    item.record_ref: item
                    for item in bundle["capability_binding_records"]
                },
                registry=RuntimeContractRegistry.from_path(
                    "contracts/runtime/clickhouse-analysis-bindings.yaml"
                ),
            )

    def test_verified_claim_strength_must_fit_linked_binding_ceiling(self):
        from bi_agent.runtime.analysis_contracts import analysis_contract_from_dict
        from bi_agent.runtime.claim_provenance import build_verified_claim_record
        from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry
        from bi_agent.runtime.runtime_persistence import (
            _validate_verified_claim_contract_boundary,
        )

        bundle = _authority_bundle()
        _use_high_value_claim_ceiling(bundle, claim_intents=("candidate_driver",))
        original = bundle["verified_claims"][0]
        factual = {
            key: value
            for key, value in original.items()
            if key not in {
                "claim_ref", "claim_digest", "run_id", "context_manifest_ref",
                "result_refs", "completeness_record_refs", "artifact_refs",
                "memory_refs", "reuse_decisions", "provenance_record_ref",
            }
        }
        factual.update({"claim_type": "candidate_driver", "claim_strength": "strong"})
        claim = build_verified_claim_record(
            factual,
            run_id="run-task9",
            context_manifest=bundle["context_manifests"][0],
            evidence_by_ref={
                item["evidence_ref"]: item for item in bundle["evidence_manifests"]
            },
            trusted_provenance=bundle["trusted_provenance_records"][0],
        )
        bundle["verified_claims"] = (claim,)
        bundle["claim_links"] = ({
            "claim_ref": claim["claim_ref"],
            "evidence_ref": claim["evidence_refs"][0],
            "context_manifest_ref": claim["context_manifest_ref"],
        },)

        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "runtime_persistence_verified_claim_strength_ceiling_exceeded",
        ):
            _validate_verified_claim_contract_boundary(
                claim,
                analysis=analysis_contract_from_dict({
                    key: value
                    for key, value in bundle["analysis_contract"].items()
                    if key != "contract_signature"
                }),
                unbound_claim_intents=set(),
                evidence_by_ref={
                    item["evidence_ref"]: item
                    for item in bundle["evidence_manifests"]
                },
                bindings_by_ref={
                    item.record_ref: item
                    for item in bundle["capability_binding_records"]
                },
                registry=RuntimeContractRegistry.from_path(
                    "contracts/runtime/clickhouse-analysis-bindings.yaml"
                ),
            )

    def test_unbound_claim_intent_gap_allows_only_zero_claim_publication(self):
        from bi_agent.runtime.analysis_contracts import analysis_contract_signature

        for has_gap, should_pass in ((True, True), (False, False)):
            with self.subTest(has_gap=has_gap):
                bundle = _authority_bundle()
                analysis = {
                    **bundle["analysis_contract"],
                    "claim_intents": ["unbound_claim_intent"],
                    "contract_gaps": ([{
                        "gap_type": "contract_partial",
                        "gap_id": "claim_intents:unbound",
                        "dataset_id": "",
                        "affected_capabilities": ["segment_contribution"],
                        "affected_claim_types": ["unbound_claim_intent"],
                        "owner": "contract_owner",
                        "repair_options": [
                            "bind_capability_claim_types",
                            "bind_metric_claim_types",
                            "clarify_claim_intent",
                        ],
                        "requires_clarification": True,
                        "diagnostic_context": {},
                    }] if has_gap else []),
                }
                analysis["contract_signature"] = analysis_contract_signature(analysis)
                bundle["analysis_contract"] = analysis
                bundle["context_manifests"] = ()
                bundle["trusted_provenance_records"] = ()
                bundle["verified_claims"] = ()
                bundle["claim_links"] = ()
                bundle["evidence_manifests"] = tuple(
                    {**item, "context_manifest_ref": ""}
                    for item in bundle["evidence_manifests"]
                )
                store = InMemoryConversationStore()
                if should_pass:
                    self.assertEqual(
                        store.save_analysis_runtime_records(
                            run_id="run-task9", **bundle
                        ),
                        "published",
                    )
                else:
                    with self.assertRaisesRegex(
                        EvidenceIntegrityError,
                        "runtime_persistence_unbound_claim_intent_gap_invalid",
                    ):
                        store.save_analysis_runtime_records(
                            run_id="run-task9", **bundle
                        )

    def test_unbound_sentinel_rejects_mixed_claims_and_fake_gap_ids(self):
        from bi_agent.runtime.analysis_contracts import analysis_contract_signature

        canonical_gap = {
            "gap_type": "contract_partial",
            "gap_id": "claim_intents:unbound",
            "dataset_id": "",
            "affected_capabilities": ["segment_contribution"],
            "affected_claim_types": ["unbound_claim_intent"],
            "owner": "contract_owner",
            "repair_options": [
                "bind_capability_claim_types",
                "bind_metric_claim_types",
                "clarify_claim_intent",
            ],
            "requires_clarification": True,
            "diagnostic_context": {},
        }
        mixed = _authority_bundle()
        analysis = {
            **mixed["analysis_contract"],
            "claim_intents": [
                "segment_contribution_or_mix_shift",
                "unbound_claim_intent",
            ],
            "contract_gaps": [canonical_gap],
        }
        analysis["contract_signature"] = analysis_contract_signature(analysis)
        mixed["analysis_contract"] = analysis
        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "runtime_persistence_verified_claim_gap_blocked",
        ):
            InMemoryConversationStore().save_analysis_runtime_records(
                run_id="run-task9", **mixed
            )

    def test_unbound_sentinel_is_wildcard_only_for_its_affected_capability(self):
        from bi_agent.runtime.analysis_contracts import analysis_contract_signature

        bundle = _authority_bundle()
        ready_capability = bundle["analysis_contract"]["capability_requirements"][0]
        analysis = {
            **bundle["analysis_contract"],
            "claim_intents": [
                bundle["verified_claims"][0]["claim_type"],
                "unbound_claim_intent",
            ],
            "capability_requirements": [ready_capability, "formula_decompose"],
            "contract_gaps": [{
                "gap_type": "contract_partial",
                "gap_id": "claim_intents:unbound",
                "dataset_id": "",
                "affected_capabilities": ["formula_decompose"],
                "affected_claim_types": ["unbound_claim_intent"],
                "owner": "contract_owner",
                "repair_options": [
                    "bind_capability_claim_types",
                    "bind_metric_claim_types",
                    "clarify_claim_intent",
                ],
                "requires_clarification": True,
                "diagnostic_context": {},
            }],
        }
        analysis["contract_signature"] = analysis_contract_signature(analysis)
        bundle["analysis_contract"] = analysis

        self.assertEqual(
            InMemoryConversationStore().save_analysis_runtime_records(
                run_id="run-task9", **bundle
            ),
            "published",
        )

        analysis["contract_gaps"][0]["affected_capabilities"] = [ready_capability]
        analysis["contract_signature"] = analysis_contract_signature(analysis)
        bundle["analysis_contract"] = analysis
        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "runtime_persistence_verified_claim_gap_blocked",
        ):
            InMemoryConversationStore().save_analysis_runtime_records(
                run_id="run-task9", **bundle
            )

        canonical_gap = dict(analysis["contract_gaps"][0])
        fake = _authority_bundle()
        analysis = {
            **fake["analysis_contract"],
            "claim_intents": ["unbound_claim_intent"],
            "contract_gaps": [{
                **canonical_gap,
                "gap_id": "fake-prefix-unbound-suffix",
            }],
        }
        analysis["contract_signature"] = analysis_contract_signature(analysis)
        fake["analysis_contract"] = analysis
        fake["context_manifests"] = ()
        fake["trusted_provenance_records"] = ()
        fake["verified_claims"] = ()
        fake["claim_links"] = ()
        fake["evidence_manifests"] = tuple(
            {**item, "context_manifest_ref": ""}
            for item in fake["evidence_manifests"]
        )
        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "runtime_persistence_unbound_claim_intent_gap_invalid",
        ):
            InMemoryConversationStore().save_analysis_runtime_records(
                run_id="run-task9", **fake
            )

    def test_compiler_unsupported_claim_gap_allows_zero_claim_publication(self):
        from bi_agent.runtime.analysis_contracts import analysis_contract_signature

        bundle = _authority_bundle()
        analysis = {
            **bundle["analysis_contract"],
            "claim_intents": ["unbound_claim_intent"],
            "contract_gaps": [{
                "gap_type": "contract_partial",
                "gap_id": "claim_intent:causal_effect:unsupported",
                "dataset_id": "",
                "affected_capabilities": ["segment_contribution"],
                "affected_claim_types": ["causal_effect"],
                "owner": "contract_owner",
                "repair_options": [
                    "choose_supported_claim_intent",
                    "clarify_claim_intent",
                ],
                "requires_clarification": True,
                "diagnostic_context": {},
            }],
        }
        analysis["contract_signature"] = analysis_contract_signature(analysis)
        bundle["analysis_contract"] = analysis
        bundle["context_manifests"] = ()
        bundle["trusted_provenance_records"] = ()
        bundle["verified_claims"] = ()
        bundle["claim_links"] = ()
        bundle["evidence_manifests"] = tuple(
            {**item, "context_manifest_ref": ""}
            for item in bundle["evidence_manifests"]
        )

        self.assertEqual(
            InMemoryConversationStore().save_analysis_runtime_records(
                run_id="run-task9", **bundle
            ),
            "published",
        )

    def test_ordinary_contract_gap_does_not_force_zero_claims(self):
        from bi_agent.runtime.analysis_contracts import analysis_contract_signature

        bundle = _authority_bundle()
        analysis = {
            **bundle["analysis_contract"],
            "contract_gaps": [{
                "gap_type": "contract_partial",
                "gap_id": "optional_context:unavailable",
                "dataset_id": "",
                "affected_capabilities": ["segment_contribution"],
                "affected_claim_types": [],
                "owner": "contract_owner",
                "repair_options": ["bind_optional_context"],
                "requires_clarification": False,
                "diagnostic_context": {},
            }],
        }
        analysis["contract_signature"] = analysis_contract_signature(analysis)
        bundle["analysis_contract"] = analysis

        self.assertEqual(
            InMemoryConversationStore().save_analysis_runtime_records(
                run_id="run-task9", **bundle
            ),
            "published",
        )

    def test_artifact_loader_rechecks_actual_bytes_after_preflight_stat(self):
        from bi_agent.runtime.evidence_authority import canonical_rows_storage_ref
        from bi_agent.runtime.runtime_persistence import ClickHouseArtifactRowsPayloadLoader

        with tempfile.TemporaryDirectory() as tmp:
            rows = ({"window_id": "target"},)
            storage_ref = canonical_rows_storage_ref(rows)
            digest = storage_ref.rsplit(":", 1)[-1]
            path = Path(tmp) / f"{digest}.json"
            path.write_text(json.dumps(rows), encoding="utf-8")
            oversized = json.dumps([{"window_id": "target", "pad": "x" * 64}]).encode()

            with patch.object(Path, "read_bytes", return_value=oversized):
                with self.assertRaisesRegex(
                    EvidenceIntegrityError, "rows_payload_too_large"
                ):
                    ClickHouseArtifactRowsPayloadLoader(
                        artifact_root=tmp, max_bytes=32
                    ).load_rows(storage_ref)

    def test_exact_inmemory_replay_adds_no_second_audit(self):
        store = InMemoryConversationStore()
        bundle = _authority_bundle()
        self.assertEqual(
            store.save_analysis_runtime_records(run_id="run-task9", **bundle),
            "published",
        )
        audit_count = len(store.audit_events)
        self.assertEqual(
            store.save_analysis_runtime_records(run_id="run-task9", **bundle),
            "replayed",
        )
        self.assertEqual(len(store.audit_events), audit_count)

    def test_exact_postgres_replay_writes_no_second_audit_or_commit(self):
        connection = FakeConnection()
        store = PostgresConversationStore(connection)
        bundle = _authority_bundle()
        self.assertEqual(
            store.save_analysis_runtime_records(run_id="run-task9", **bundle),
            "published",
        )
        audit_count = sum(
            params.get("event_type") == "analysis_runtime_records_persisted"
            for _, params in connection.statements
        )
        statement_count = len(connection.statements)
        self.assertEqual(
            store.save_analysis_runtime_records(run_id="run-task9", **bundle),
            "replayed",
        )
        self.assertEqual(connection.commits, 1)
        self.assertEqual(
            sum(
                params.get("event_type") == "analysis_runtime_records_persisted"
                for _, params in connection.statements
            ),
            audit_count,
        )
        replay_sql = "\n".join(
            statement for statement, _ in connection.statements[statement_count:]
        )
        self.assertNotIn("INSERT INTO waje_runtime.analysis_contracts", replay_sql)

    def test_conflicting_postgres_publication_for_same_run_is_rejected_prewrite(self):
        connection = FakeConnection()
        store = PostgresConversationStore(connection)
        self.assertEqual(
            store.save_analysis_runtime_records(
                run_id="run-task9", **_authority_bundle()
            ),
            "published",
        )
        conflicting = _authority_bundle()
        conflicting["repair_attempts"] = ({
            **conflicting["repair_attempts"][0],
            "reason": "completeness_validation_failed",
        },)
        statement_count = len(connection.statements)
        audit_count = sum(
            params.get("event_type") == "analysis_runtime_records_persisted"
            for _, params in connection.statements
        )

        with self.assertRaisesRegex(
            EvidenceIntegrityError, "analysis_runtime_publication_conflict"
        ):
            store.save_analysis_runtime_records(run_id="run-task9", **conflicting)

        self.assertEqual(connection.commits, 1)
        self.assertEqual(connection.rollbacks, 1)
        self.assertEqual(
            sum(
                params.get("event_type") == "analysis_runtime_records_persisted"
                for _, params in connection.statements
            ),
            audit_count,
        )
        conflict_sql = "\n".join(
            statement for statement, _ in connection.statements[statement_count:]
        )
        self.assertNotIn("INSERT INTO waje_runtime.analysis_contracts", conflict_sql)

    def test_verified_claim_link_set_and_context_sources_must_be_exact(self):
        for mutation, code in (
            (
                lambda bundle: bundle.__setitem__("claim_links", ()),
                "runtime_persistence_claim_evidence_links_mismatch",
            ),
            (
                lambda bundle: bundle["context_manifests"][0]["sources"].append(
                    {"type": "evidence", "ref": "evidence:injected", "can_support_claim": True}
                ),
                "context_manifest_integrity_invalid",
            ),
            (
                lambda bundle: bundle["verified_claims"][0].__setitem__(
                    "artifact_refs", ["artifact:injected"]
                ),
                "verified_claim_integrity_invalid",
            ),
        ):
            with self.subTest(code=code):
                bundle = _authority_bundle()
                mutation(bundle)
                connection = FakeConnection()
                with self.assertRaisesRegex(EvidenceIntegrityError, code):
                    PostgresConversationStore(connection).save_analysis_runtime_records(
                        run_id="run-task9", **bundle
                    )
                self.assertEqual(connection.statements, [])

    def test_artifact_rows_loader_is_bounded_and_normalizes_parse_failures(self):
        from bi_agent.runtime.runtime_persistence import ClickHouseArtifactRowsPayloadLoader

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            loader = ClickHouseArtifactRowsPayloadLoader(
                artifact_root=root,
                max_bytes=32,
                max_rows=1,
                max_nesting_depth=3,
            )
            cases = (
                (b"[" + b" " * 40 + b"]", "rows_payload_too_large"),
                (b"not-json", "rows_payload_json_invalid"),
                (json.dumps([{"a": 1}, {"a": 2}]).encode(), "rows_payload_row_limit_exceeded"),
                (json.dumps([{"a": {"b": {"c": 1}}}]).encode(), "rows_payload_nesting_limit_exceeded"),
            )
            for index, (payload, code) in enumerate(cases):
                digest = f"{index + 1:064x}"
                (root / f"{digest}.json").write_bytes(payload)
                with self.subTest(code=code), self.assertRaisesRegex(
                    EvidenceIntegrityError, code
                ):
                    loader.load_rows(f"rows-storage:sha256:{digest}")

    def test_answer_package_keeps_internal_authority_audit_and_verified_claim_refs(self):
        from bi_agent.runtime.answer_package import (
            build_answer_package,
            reverify_answer_package_for_delivery,
        )
        from bi_agent.runtime.claim_provenance import (
            build_trusted_claim_provenance_record,
        )
        from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry

        content = _fixture_content()
        resolver = content["evidence_resolver"]
        binding = resolver.resolve_capability_binding(content["binding_manifest_ref"])
        evidence = _evidence_for_binding(binding)
        claim = {
            "text": "目标期渠道贡献有变化。",
            "claim_strength": "observed",
            "claim_type": "segment_contribution_or_mix_shift",
            "evidence_refs": (evidence["evidence_ref"],),
            "numbers": {"paid_amount": 12.0},
            "fact_selectors": {
                "paid_amount": {
                    "result_ref": binding.result_refs[0],
                    "window_id": "target",
                    "dimensions": {"channel": "app"},
                }
            },
        }
        bundle = _authority_bundle()
        package = build_answer_package(
            run_id="run-task9-answer",
            draft_claims=(claim,),
            evidence=(evidence,),
            evidence_resolver=resolver,
            rows_loader=resolver.rows_loader,
            runtime_registry=RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
            release_resolver=content["release_resolver"],
            checkpoint_events=(),
            proposed_graph=(),
            accepted_graph=(),
            rejected_or_degraded_mutations=(),
            validator_results=(),
            sql_text="SELECT aggregate",
            sql_hash="a" * 64,
            artifact_audit={},
            answer_text=claim["text"],
            final_business_summary=claim["text"],
            analysis_contract=bundle["analysis_contract"],
            query_contracts=bundle["query_contracts"],
            query_results=bundle["query_execution_records"],
            completeness_reports=bundle["completeness_records"],
            capability_execution_plans=(binding.plan_payload,),
            repair_attempts=bundle["repair_attempts"],
            context_manifest={
                "thread_id": "thread-task9-answer",
                "topic_id": "topic-task9-answer",
            },
            trusted_claim_provenance_records=(
                build_trusted_claim_provenance_record(
                    run_id="run-task9-answer",
                    artifact_refs=("artifact:task9",),
                    memory_refs=("memory:task9",),
                    reuse_decisions=(
                        {"source_ref": "asset:task9", "decision": "reuse"},
                    ),
                ),
            ),
        )

        self.assertEqual(
            package["admin_audit"]["verifier"]["status"],
            "passed",
            package["admin_audit"]["verifier"],
        )
        for key in (
            "analysis_contract", "query_contracts", "query_results",
            "completeness_reports", "capability_execution_plans", "repair_attempts",
        ):
            self.assertTrue(package["admin_audit"][key], key)
        published = package["sections"][0]["payload"]["claims"][0]
        self.assertTrue(published["claim_ref"].startswith("claim:sha256:"))
        self.assertEqual(
            published["context_manifest_ref"],
            package["admin_audit"]["context_manifest"]["manifest_id"],
        )
        self.assertTrue(published["result_refs"])
        self.assertEqual(published["artifact_refs"], ["artifact:task9"])
        self.assertEqual(published["memory_refs"], ["memory:task9"])
        source_refs = {
            item["ref"] for item in package["admin_audit"]["context_manifest"]["sources"]
        }
        self.assertIn("evidence:task9:segment", source_refs)
        client = reverify_answer_package_for_delivery(
            package,
            evidence_resolver=resolver,
            rows_loader=resolver.rows_loader,
            runtime_registry=RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
            release_resolver=content["release_resolver"],
        )
        self.assertEqual(set(client["admin_audit"]), {"verifier"})
        self.assertNotIn("SELECT aggregate", str(client))
        self.assertNotIn("failed-signature", str(client))

    def test_postgres_persists_complete_authority_chain_in_one_commit(self):
        connection = FakeConnection()
        store = PostgresConversationStore(connection)

        store.save_analysis_runtime_records(run_id="run-task9", **_authority_bundle())

        sql = "\n".join(statement for statement, _ in connection.statements)
        for table in (
            "analysis_contracts", "query_contracts", "query_runs",
            "query_execution_authority", "rows_metadata_authority",
            "snapshot_authority", "query_completeness_reports",
            "capability_binding_authority", "evidence_manifests",
            "claim_evidence_links", "query_repair_attempts",
        ):
            self.assertIn(f"waje_runtime.{table}", sql)
        self.assertEqual(
            sum(
                params.get("event_type") == "analysis_runtime_records_persisted"
                for _, params in connection.statements
            ),
            1,
        )
        self.assertEqual(connection.commits, 1)

    def test_injected_write_failure_rolls_back_everything(self):
        connection = FakeConnection(fail_execute_at=5)
        with self.assertRaisesRegex(RuntimeError, "execute failed"):
            PostgresConversationStore(connection).save_analysis_runtime_records(
                run_id="run-task9", **_authority_bundle()
            )
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)

    def test_commit_failure_rolls_back(self):
        connection = FakeConnection(fail_commit=True)
        with self.assertRaisesRegex(RuntimeError, "commit failed"):
            PostgresConversationStore(connection).save_analysis_runtime_records(
                run_id="run-task9", **_authority_bundle()
            )
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)

    def test_conflicting_postgres_immutable_ref_rolls_back(self):
        class ConflictConnection(FakeConnection):
            def execute(self, statement, params=None):
                cursor = super().execute(statement, params)
                if "waje_runtime.analysis_contracts" in statement:
                    cursor.rowcount = 0
                return cursor

        connection = ConflictConnection()
        with self.assertRaisesRegex(EvidenceIntegrityError, "authority_ref_collision:analysis_contract"):
            PostgresConversationStore(connection).save_analysis_runtime_records(
                run_id="run-task9", **_authority_bundle()
            )
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)

    def test_preflight_rejects_missing_chain_before_database_write(self):
        bundle = _authority_bundle()
        bundle["rows_records"] = bundle["rows_records"][:-1]
        connection = FakeConnection()
        with self.assertRaisesRegex(EvidenceIntegrityError, "runtime_persistence_rows_record_missing"):
            PostgresConversationStore(connection).save_analysis_runtime_records(
                run_id="run-task9", **bundle
            )
        self.assertEqual(connection.statements, [])

    def test_preflight_rejects_valid_snapshot_record_from_different_revision(self):
        bundle = _authority_bundle()
        _, drifted = verified_dimension_scan_asset(
            rows=(
                {"window_id": "target", "period": "2026-06-02", "channel": "app", "amount": 12},
                {"window_id": "baseline", "period": "2026-06-01", "channel": "app", "amount": 10},
            ),
            required_fields=("window_id", "window_role", "observation_key", "paid_amount", "channel"),
            resolved_windows={
                "target": {"start_inclusive": "2026-06-02", "end_exclusive": "2026-06-03", "timezone": "Africa/Lagos"},
                "baseline": {"start_inclusive": "2026-06-01", "end_exclusive": "2026-06-02", "timezone": "Africa/Lagos"},
            },
            schema_fingerprint="schema-different-valid-revision",
        )
        snapshot_ref = bundle["snapshot_records"][0].snapshot_ref
        bundle["snapshot_records"] = (
            drifted["evidence_resolver"].resolve_snapshot(snapshot_ref),
        )
        connection = FakeConnection()
        with self.assertRaisesRegex(
            EvidenceIntegrityError, "runtime_persistence_snapshot_record_link_mismatch"
        ):
            PostgresConversationStore(connection).save_analysis_runtime_records(
                run_id="run-task9", **bundle
            )
        self.assertEqual(connection.statements, [])

    def test_identical_replay_is_idempotent_and_conflicting_ref_is_rejected(self):
        store = InMemoryConversationStore()
        bundle = _authority_bundle()
        store.save_analysis_runtime_records(run_id="run-task9", **bundle)
        store.save_analysis_runtime_records(run_id="run-task9", **bundle)
        changed = dict(bundle)
        changed["analysis_contract"] = {**bundle["analysis_contract"], "extra": "drift"}
        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "runtime_persistence_analysis_contract_shape_invalid",
        ):
            store.save_analysis_runtime_records(run_id="run-task9", **changed)

    def test_postgres_resolver_rehydrates_and_validates_all_typed_records(self):
        from bi_agent.runtime.runtime_persistence import (
            PostgresRuntimeEvidenceResolver,
            authority_record_payload,
        )

        bundle = _authority_bundle()
        record = bundle["query_execution_records"][0]
        row = _query_resolver_row(record)
        resolved = PostgresRuntimeEvidenceResolver(FakeConnection(rows=[row])).resolve_query_execution(
            record.result_ref
        )
        self.assertEqual(resolved, record)

        row["record_digest"] = "0" * 64
        with self.assertRaisesRegex(EvidenceIntegrityError, "query_execution_record_(column|digest)_mismatch"):
            PostgresRuntimeEvidenceResolver(FakeConnection(rows=[row])).resolve_query_execution(
                record.result_ref
            )

    def test_postgres_resolver_rehydrates_every_task6_record_kind(self):
        from bi_agent.runtime.runtime_persistence import (
            PostgresRuntimeEvidenceResolver,
            authority_record_payload,
        )

        bundle = _authority_bundle()
        cases = (
            (
                "snapshot",
                bundle["snapshot_records"][0],
                "resolve_snapshot",
                "snapshot_ref",
                ("record_ref", "record_digest", "snapshot_ref"),
            ),
            (
                "rows",
                bundle["rows_records"][0],
                "resolve_rows_record",
                "record_ref",
                (
                    "record_ref", "record_digest", "rows_ref", "rows_content_hash",
                    "row_count", "unique_key_fields", "storage_ref",
                ),
            ),
            (
                "completeness",
                bundle["completeness_records"][0],
                "resolve_completeness",
                "record_ref",
                ("record_ref", "report_ref", "report_digest", "result_ref", "query_contract_ref"),
            ),
            (
                "capability_binding",
                bundle["capability_binding_records"][0],
                "resolve_capability_binding",
                "record_ref",
                (
                    "record_ref", "binding_digest", "capability_id",
                    "claim_strength_taxonomy_version", "maximum_claim_strength_rank",
                ),
            ),
        )
        for kind, record, method, selector, columns in cases:
            with self.subTest(kind=kind):
                if kind == "completeness":
                    query_record = next(
                        item
                        for item in bundle["query_execution_records"]
                        if item.result_ref == record.result_ref
                    )
                    row = _completeness_resolver_row(record, query_record)
                elif kind == "capability_binding":
                    row = _binding_resolver_row(record)
                elif kind == "rows":
                    row = _rows_resolver_row(record)
                else:
                    row = {name: getattr(record, name) for name in columns}
                if "unique_key_fields" in row:
                    row["unique_key_fields"] = list(row["unique_key_fields"])
                row.setdefault(
                    "payload", json.dumps(authority_record_payload(kind, record))
                )
                resolved = getattr(
                    PostgresRuntimeEvidenceResolver(FakeConnection(rows=[row])), method
                )(getattr(record, selector))
                self.assertEqual(resolved, record)

    def test_postgres_resolver_validates_persisted_claim_identity_and_provenance(self):
        from bi_agent.runtime.runtime_persistence import (
            PostgresRuntimeEvidenceResolver,
        )

        bundle = _authority_bundle()
        claim = bundle["verified_claims"][0]
        provenance = bundle["trusted_provenance_records"][0]
        claim_row = {
            key: claim[key]
            for key in (
                "claim_ref", "claim_digest", "run_id", "context_manifest_ref",
                "provenance_record_ref",
            )
        }
        claim_row["payload"] = json.dumps(claim)
        provenance_row = {
            key: provenance[key]
            for key in ("record_ref", "record_digest", "run_id")
        }
        provenance_row["payload"] = json.dumps(provenance)

        resolved_claim = PostgresRuntimeEvidenceResolver(
            FakeConnection(rows=[claim_row])
        ).resolve_verified_claim(claim["claim_ref"])
        resolved_provenance = PostgresRuntimeEvidenceResolver(
            FakeConnection(rows=[provenance_row])
        ).resolve_claim_provenance(provenance["record_ref"])

        self.assertEqual(canonical_value(resolved_claim), canonical_value(claim))
        self.assertEqual(
            canonical_value(resolved_provenance), canonical_value(provenance)
        )

        claim_row["claim_digest"] = "0" * 64
        with self.assertRaisesRegex(
            EvidenceIntegrityError, "verified_claim_column_mismatch:claim_digest"
        ):
            PostgresRuntimeEvidenceResolver(
                FakeConnection(rows=[claim_row])
            ).resolve_verified_claim(claim["claim_ref"])

    def test_postgres_resolver_rejects_payload_kind_spoof(self):
        from bi_agent.runtime.runtime_persistence import (
            PostgresRuntimeEvidenceResolver,
            authority_record_payload,
        )

        record = _authority_bundle()["rows_records"][0]
        row = _rows_resolver_row(record)
        row["payload"] = json.dumps(
            {**authority_record_payload("rows", record), "kind": "snapshot"}
        )
        with self.assertRaisesRegex(EvidenceIntegrityError, "rows_record_kind_mismatch"):
            PostgresRuntimeEvidenceResolver(FakeConnection(rows=[row])).resolve_rows(
                record.rows_ref
            )

    def test_postgres_query_resolver_rejects_cross_run_join_drift(self):
        from bi_agent.runtime.runtime_persistence import (
            PostgresRuntimeEvidenceResolver,
            authority_record_payload,
        )

        bundle = _authority_bundle()
        record = bundle["query_execution_records"][0]
        row = _query_resolver_row(record)
        row["authority_run_id"] = "run-crossed"
        with self.assertRaisesRegex(
            EvidenceIntegrityError, "query_execution_run_membership_mismatch"
        ):
            PostgresRuntimeEvidenceResolver(FakeConnection(rows=[row])).resolve_query_execution(
                record.result_ref
            )

    def test_postgres_query_resolver_rejects_incomplete_analysis_envelope(self):
        from bi_agent.runtime.analysis_contracts import analysis_contract_signature
        from bi_agent.runtime.runtime_persistence import PostgresRuntimeEvidenceResolver

        bundle = _authority_bundle()
        record = bundle["query_execution_records"][0]
        row = _query_resolver_row(record)
        incomplete = {"analysis_contract_id": record.contract.analysis_contract_ref}
        incomplete["contract_signature"] = analysis_contract_signature(incomplete)
        row["analysis_payload"] = json.dumps(incomplete)
        row["stored_analysis_signature"] = incomplete["contract_signature"]

        with self.assertRaisesRegex(
            EvidenceIntegrityError, "query_execution_analysis_contract_mismatch"
        ):
            PostgresRuntimeEvidenceResolver(
                FakeConnection(rows=[row])
            ).resolve_query_execution(record.result_ref)

    def test_postgres_completeness_binding_and_rows_reject_join_mirror_drift(self):
        from bi_agent.runtime.runtime_persistence import PostgresRuntimeEvidenceResolver

        bundle = _authority_bundle()
        report = bundle["completeness_records"][0]
        query = next(
            item
            for item in bundle["query_execution_records"]
            if item.result_ref == report.result_ref
        )
        completeness_row = _completeness_resolver_row(report, query)
        completeness_row["stored_analysis_readiness"] = "blocked"
        binding = bundle["capability_binding_records"][0]
        binding_row = _binding_resolver_row(binding)
        binding_row["authority_run_id"] = "run-crossed"
        rows_record = bundle["rows_records"][0]
        rows_row = _rows_resolver_row(rows_record)
        rows_row["run_rows_ref"] = "rows:crossed"
        cases = (
            (
                FakeConnection(rows=[completeness_row]),
                "resolve_completeness",
                report.record_ref,
                "completeness_join_mirror_mismatch",
            ),
            (
                FakeConnection(rows=[binding_row]),
                "resolve_capability_binding",
                binding.record_ref,
                "capability_binding_run_membership_mismatch",
            ),
            (
                FakeConnection(rows=[rows_row]),
                "resolve_rows",
                rows_record.rows_ref,
                "rows_record_run_membership_mismatch",
            ),
        )
        for connection, method, ref, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(
                EvidenceIntegrityError, code
            ):
                getattr(PostgresRuntimeEvidenceResolver(connection), method)(ref)

    def test_postgres_resolver_rejects_unknown_authority_payload_keys(self):
        from bi_agent.runtime.runtime_persistence import (
            PostgresRuntimeEvidenceResolver,
            authority_record_payload,
        )

        record = _authority_bundle()["rows_records"][0]
        payload = {**authority_record_payload("rows", record), "unreviewed": True}
        row = _rows_resolver_row(record)
        row["payload"] = json.dumps(payload)
        with self.assertRaisesRegex(
            EvidenceIntegrityError, "rows_record_payload_keys_invalid"
        ):
            PostgresRuntimeEvidenceResolver(FakeConnection(rows=[row])).resolve_rows(
                record.rows_ref
            )

    def test_postgres_parameters_never_contain_aggregate_rows_payload(self):
        connection = FakeConnection()
        PostgresConversationStore(connection).save_analysis_runtime_records(
            run_id="run-task9", **_authority_bundle()
        )
        for statement, params in connection.statements:
            if not any(
                table in statement
                for table in ("query_runs", "query_execution_authority", "rows_metadata_authority")
            ) or "payload" not in params:
                continue
            payload = json.loads(params["payload"])
            self.assertFalse(_contains_key(payload, "rows"), statement)

    def test_latest_completeness_alias_and_immutable_record_use_distinct_queries(self):
        from bi_agent.runtime.runtime_persistence import (
            PostgresRuntimeEvidenceResolver,
            authority_record_payload,
        )

        bundle = _authority_bundle()
        record = bundle["completeness_records"][0]
        query_record = next(
            item
            for item in bundle["query_execution_records"]
            if item.result_ref == record.result_ref
        )
        row = _completeness_resolver_row(record, query_record)
        connection = FakeConnection(rows=[row])
        resolver = PostgresRuntimeEvidenceResolver(connection)
        self.assertEqual(resolver.resolve_completeness(record.record_ref), record)
        self.assertEqual(resolver.resolve_latest_completeness(record.report_ref), record)
        sql = "\n".join(statement for statement, _ in connection.statements)
        self.assertIn("record_ref =", sql)
        self.assertIn("report_ref =", sql)
        self.assertIn("ORDER BY c.created_at DESC", sql)

    def test_rows_payload_loader_keeps_payload_out_of_postgres_and_validates_artifact(self):
        from bi_agent.runtime.runtime_persistence import ClickHouseArtifactRowsPayloadLoader

        bundle = _authority_bundle()
        record = bundle["rows_records"][0]
        authority = verified_dimension_scan_asset(
            rows=(
                {"window_id": "target", "period": "2026-06-02", "channel": "app", "amount": 12},
                {"window_id": "baseline", "period": "2026-06-01", "channel": "app", "amount": 10},
            ),
            required_fields=("window_id", "window_role", "observation_key", "paid_amount", "channel"),
            resolved_windows={
                "target": {"start_inclusive": "2026-06-02", "end_exclusive": "2026-06-03", "timezone": "Africa/Lagos"},
                "baseline": {"start_inclusive": "2026-06-01", "end_exclusive": "2026-06-02", "timezone": "Africa/Lagos"},
            },
        )[1]["evidence_resolver"]
        payload = authority.rows_loader.load_rows(record.storage_ref)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"{record.storage_ref.split(':')[-1]}.json"
            path.write_text(json.dumps(list(payload)), encoding="utf-8")
            loader = ClickHouseArtifactRowsPayloadLoader(artifact_root=tmp)
            self.assertEqual(loader.load_rows_record(record), payload)
            path.write_text(json.dumps([*payload, dict(payload[0])]), encoding="utf-8")
            with self.assertRaisesRegex(EvidenceIntegrityError, "rows_payload_(storage_hash|count|hash|unique_key)"):
                loader.load_rows_record(record)

    def test_rows_payload_loader_rejects_path_traversal_and_unsafe_clickhouse_locator(self):
        from bi_agent.runtime.runtime_persistence import ClickHouseArtifactRowsPayloadLoader

        loader = ClickHouseArtifactRowsPayloadLoader(artifact_root="artifacts")
        for locator in ("../secret", "clickhouse:db.table;DROP:rev:hash", "clickhouse:db.table:wrong-revision"):
            with self.subTest(locator=locator), self.assertRaisesRegex(
                EvidenceIntegrityError, "rows_storage_ref_(invalid|unsafe|revision)"
            ):
                loader.load_rows(locator)

    def test_clickhouse_rows_locator_pins_revision_and_content_hash(self):
        from bi_agent.runtime.evidence_authority import canonical_rows_hash
        from bi_agent.runtime.runtime_persistence import ClickHouseArtifactRowsPayloadLoader

        rows = ({"window_id": "target", "amount": 12},)
        revision = "a" * 64
        locator = (
            "clickhouse-rows:analytics.runtime_rows:"
            f"{revision}:{canonical_rows_hash(rows, ())}"
        )

        class FakeClickHouse:
            def __init__(self, returned_revision):
                self.returned_revision = returned_revision

            def load_rows(self, *, table, revision):
                self.table = table
                self.requested_revision = revision
                return {"revision": self.returned_revision, "rows": list(rows)}

        client = FakeClickHouse(revision)
        loaded = ClickHouseArtifactRowsPayloadLoader(
            artifact_root="artifacts", clickhouse=client
        ).load_rows(locator)
        self.assertEqual(loaded, rows)
        self.assertEqual(client.table, "analytics.runtime_rows")
        self.assertEqual(client.requested_revision, revision)

        with self.assertRaisesRegex(EvidenceIntegrityError, "rows_storage_ref_revision_mismatch"):
            ClickHouseArtifactRowsPayloadLoader(
                artifact_root="artifacts", clickhouse=FakeClickHouse("c" * 64)
            ).load_rows(locator)


if __name__ == "__main__":
    unittest.main()
