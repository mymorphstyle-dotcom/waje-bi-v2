from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import json
from types import SimpleNamespace

import pytest

from bi_agent.runtime.analysis_contracts import AnalysisContract, ContractGap
from bi_agent.runtime.current_data_coverage import current_data_coverage_cases
from bi_agent.runtime.dataset_catalog import build_dataset_release_authority_record, dataset_snapshot_release_ref
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)


def _analysis_contract_gap_authority(
    gaps: list[dict[str, object]], required_capabilities: list[str]
) -> dict[str, object]:
    typed_gaps = tuple(
        ContractGap(
            gap_type=str(gap["gap_type"]),
            gap_id=str(gap["gap_id"]),
            dataset_id=str(gap.get("dataset_id") or ""),
            affected_capabilities=tuple(required_capabilities),
            affected_claim_types=(),
            owner=str(gap["owner"]),
            repair_options=("repair_contract_boundary",),
            requires_clarification=False,
            diagnostic_context={},
        )
        for gap in gaps
    )
    return AnalysisContract(
        analysis_contract_id="analysis-contract:obligation-review-test",
        contract_version="1",
        question_families=("data_quality_or_evidence_review",),
        target_metric_refs=(),
        claim_intents=(),
        scope={},
        business_timezone="Europe/London",
        as_of="2026-06-03T12:00:00+01:00",
        resolved_windows=(),
        metric_bindings=(),
        dimension_bindings=(),
        dataset_requirements=tuple(
            dict.fromkeys(
                str(gap.get("dataset_id") or "") for gap in gaps if gap.get("dataset_id")
            )
        ),
        capability_requirements=tuple(required_capabilities),
        permission_scope="analyst",
        contract_gaps=typed_gaps,
    ).to_dict()


def _run_matched_contract_authority(
    contract: dict[str, object], *, run_id="run-obligation-review"
) -> dict[str, object]:
    persisted = json.loads(json.dumps(contract))
    persisted["analysis_contract_id"] = f"analysis:{run_id}:1"
    planned = json.loads(json.dumps(persisted))
    return {
        "run_id": run_id,
        "admin_audit": {
            "analysis_contract": persisted,
            "compiler_runtime_plan": {"analysis_contract": planned},
        },
    }


def test_platform_suite_covers_public_families_current_roles_and_boundaries():
    from tools.phase7.run_live_conversation_system_test import load_cases

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    cases = load_cases("evals/phase7/existing_data_coverage_scenarios.yaml")
    scenarios = [turn for case in cases for turn in case["turns"]]

    assert {turn["scenario"]["question_family"] for turn in scenarios} == set(
        registry.question_family_ids
    )
    assert {
        "paid_order_success",
        "market_dashboard",
        "market_dashboard_channel",
        "gameplay",
        "gameplay_channel",
        "external_event",
    } <= {
        dataset
        for turn in scenarios
        for dataset in turn["scenario"]["expected_dataset_states"]
    }
    boundary_types = {
        turn["scenario"].get("terminal_boundary") for turn in scenarios
    }
    assert {"permission_blocked", "contract_allowed_partial"} <= boundary_types
    assert any(turn["scenario"].get("reuse") == "required" for turn in scenarios)
    assert any(
        turn["scenario"].get("clarification_resume") == "required"
        for turn in scenarios
    )
    assert not any(
        "final_answer_contains" in turn.get("expect", {})
        for turn in scenarios
    )


def test_suite_selector_keeps_fixed_eight_and_platform_tracks_distinct():
    from tools.phase7.run_live_conversation_system_test import load_suite_cases

    fixed = load_suite_cases("fixed-eight")
    platform = load_suite_cases("platform-current-data")

    assert [case["id"] for case in fixed] == [
        "paid_amount_revenue_diagnostics_8_question_set"
    ]
    assert len(fixed[0]["turns"]) == 8
    assert {case["group"] for case in platform} == {"platform_current_data"}


def test_platform_suite_applies_fixed_authority_clock_to_every_case():
    from tools.phase7.run_live_conversation_system_test import load_suite_cases

    contexts = [case.get("analysis_context") for case in load_suite_cases("platform-current-data")]
    assert contexts
    assert all(
        context == {
            "as_of": "2026-06-03T12:00:00+01:00",
            "target_date": "2026-06-02",
            "previous_day": "2026-06-01",
            "rolling_7_day_start": "2026-05-26",
            "rolling_7_day_end": "2026-06-01",
            "same_weekday_last_week": "2026-05-26",
            "pattern_history_start": "2026-01-01",
            "anomaly_history_start": "2026-05-03",
        }
        for context in contexts
    )


def test_platform_positive_reuse_keeps_physical_query_material_and_reorders_priority():
    from tools.phase7.run_live_conversation_system_test import load_suite_cases

    case = next(
        item
        for item in load_suite_cases("platform-current-data")
        if item["id"] == "platform_baseline_reuse"
    )
    first, second = (turn["scenario"] for turn in case["turns"])

    assert first["target_metrics"] == second["target_metrics"] == ["paid_amount"]
    assert first["scope"] == second["scope"] == {"type": "full_sample"}
    assert first["permission_scope"] == second["permission_scope"] == "analyst"
    assert first["expected_dataset_states"] == second["expected_dataset_states"]
    assert set(first["baselines"]) == set(second["baselines"])
    assert first["baselines"] != second["baselines"]
    assert second["expected_reuse"] == {
        "capability_id": "market_health_compare",
        "dataset_ids": ["market_dashboard"],
    }
    assert second["expected_capability_states"] == {
        "compare_periods": "snapshot_unavailable_as_of",
        "market_health_compare": "executable",
    }
    assert second["excluded_inputs"] == {
        "paid_order_success": "snapshot_unavailable_as_of"
    }


def _authoritative_reuse_review_fixture():
    from bi_agent.runtime.analysis_runtime import AnalysisRuntimeRequest
    from tests.phase7.test_analysis_runtime_reuse import (
        _candidate,
        _proposal,
        _publish_source,
        _runtime_fixture,
        _source_request,
    )

    runtime, provider, store, topic_id, signed = _runtime_fixture()
    source = runtime.execute(_source_request("run-reuse-review-source", topic_id))
    candidate = _candidate(runtime, source, signed)
    _publish_source(runtime, store, topic_id, source, candidate)
    current = runtime.execute(
        AnalysisRuntimeRequest.create(
            run_id="run-reuse-review-current",
            topic_id=topic_id,
            proposal=_proposal(("rolling_7_day_baseline", "previous_day")),
            accepted_graph=("compare_periods",),
            as_of="2026-06-03T12:00:00+01:00",
            permission_scope="analyst",
            reuse_candidates=(candidate,),
        )
    )
    store.upsert_run(
        "run-reuse-review-current",
        thread_id="thread-reuse",
        topic_id=topic_id,
        status="completed",
        request={},
    )
    binding = next(
        item
        for item in current.persistence_records["capability_binding_records"]
        if item.capability_id == "compare_periods"
    )
    source_binding = next(
        item
        for item in source.persistence_records["capability_binding_records"]
        if item.capability_id == "compare_periods"
    )
    authority = _run_matched_contract_authority(
        current.analysis_contract.to_dict(),
        run_id="run-reuse-review-current",
    )
    authority["admin_audit"]["reuse_decisions"] = [
        dict(current.reuse_decisions[0])
    ]
    authority["sections"] = [{
        "section_id": "evidence",
        "payload": {
            "evidence": [{
                "evidence_ref": "evidence:reuse-review-current",
                "binding_manifest_ref": binding.record_ref,
                "binding_manifest_digest": binding.binding_digest,
                "result_refs": list(binding.result_refs),
            }]
        },
    }]
    return SimpleNamespace(
        authority=authority,
        registry=runtime.registry,
        resolver=runtime.evidence_resolver,
        rows_loader=runtime.rows_loader,
        release_resolver=runtime.release_resolver,
        runtime=runtime,
        provider=provider,
        store=store,
        thread_id="thread-reuse",
        topic_id=topic_id,
        source_run_id="run-reuse-review-source",
        current_run_id="run-reuse-review-current",
        source_result_ref=source.query_results[0].result_ref,
        current_result_ref=current.query_results[0].result_ref,
        query_contract_ref=current.query_contracts[0].query_contract_id,
        source=source,
        current=current,
        source_binding=source_binding,
        current_binding=binding,
        signed_snapshots=signed,
    )


def _authoritative_market_reuse_review_fixture():
    from bi_agent.conversation.store import InMemoryConversationStore
    from bi_agent.runtime.analysis_runtime import AnalysisRuntime, AnalysisRuntimeRequest
    from bi_agent.runtime.evidence_authority import RuntimeEvidenceAuthority
    from bi_agent.runtime.query_executor import ClickHouseQueryExecutor
    from tests.phase4.test_analysis_contract_compiler import (
        _market_dashboard_snapshots,
        canonical_release_catalog,
    )
    from tests.phase7.test_analysis_runtime_reuse import (
        _CountingRowsRuntime,
        _candidate,
        _publish_source,
    )

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    evidence_authority = RuntimeEvidenceAuthority(runtime_registry=registry)
    market, channel = _market_dashboard_snapshots()
    catalog, release_resolver, signed = canonical_release_catalog(market, channel)
    provider = _CountingRowsRuntime()
    store = InMemoryConversationStore()
    store.create_thread("thread-reuse", owner_id="analyst-1")
    topic = store.create_topic("thread-reuse", title="经营盘基线复用")
    runtime = AnalysisRuntime(
        catalog=catalog,
        registry=registry,
        executor=ClickHouseQueryExecutor(
            provider,
            evidence_resolver=evidence_authority,
            rows_loader=evidence_authority.rows_loader,
            evidence_writer=evidence_authority._runtime_writer(),
            release_resolver=release_resolver,
        ),
        release_resolver=release_resolver,
        evidence_authority=evidence_authority,
        store=store,
    )

    def request(run_id, baselines, *, reuse_candidates=()):
        return AnalysisRuntimeRequest.create(
            run_id=run_id,
            topic_id=topic.topic_id,
            proposal={
                "question_families": ["custom_baseline_comparison"],
                "target_metrics": ["paid_amount"],
                "claim_intents": ["comparative_change"],
                "scope": {"type": "full_sample"},
                "target_semantic": "yesterday",
                "baselines": list(baselines),
            },
            accepted_graph=("market_health_compare",),
            as_of="2026-06-03T12:00:00+01:00",
            permission_scope="analyst",
            reuse_candidates=reuse_candidates,
        )

    source = runtime.execute(
        request(
            "run-market-reuse-review-source",
            ("previous_day", "rolling_7_day_baseline"),
        )
    )
    candidate = _candidate(runtime, source, signed)
    _publish_source(runtime, store, topic.topic_id, source, candidate)
    current = runtime.execute(
        request(
            "run-market-reuse-review-current",
            ("rolling_7_day_baseline", "previous_day"),
            reuse_candidates=(candidate,),
        )
    )
    store.upsert_run(
        "run-market-reuse-review-current",
        thread_id="thread-reuse",
        topic_id=topic.topic_id,
        status="completed",
        request={},
    )
    binding = next(
        item
        for item in current.persistence_records["capability_binding_records"]
        if item.capability_id == "market_health_compare"
    )
    authority = _run_matched_contract_authority(
        current.analysis_contract.to_dict(),
        run_id="run-market-reuse-review-current",
    )
    authority["admin_audit"]["reuse_decisions"] = [
        dict(current.reuse_decisions[0])
    ]
    authority["sections"] = [{
        "section_id": "evidence",
        "payload": {
            "evidence": [{
                "evidence_ref": "evidence:market-reuse-review-current",
                "binding_manifest_ref": binding.record_ref,
                "binding_manifest_digest": binding.binding_digest,
                "result_refs": [
                    *binding.result_refs,
                    *binding.validation_result_refs,
                ],
            }]
        },
    }]
    return SimpleNamespace(
        authority=authority,
        registry=registry,
        resolver=evidence_authority,
        rows_loader=evidence_authority.rows_loader,
        release_resolver=release_resolver,
        store=store,
        thread_id="thread-reuse",
        topic_id=topic.topic_id,
        source_run_id="run-market-reuse-review-source",
        current_run_id="run-market-reuse-review-current",
        source_result_ref=source.query_results[0].result_ref,
        current_result_ref=current.query_results[0].result_ref,
        query_contract_ref=current.query_contracts[0].query_contract_id,
    )


def _reuse_case_lineage(
    fixture,
    *,
    current_run_id=None,
    current_topic_id=None,
    prior_runs=None,
):
    return {
        "thread_id": fixture.thread_id,
        "current_run_id": current_run_id or fixture.current_run_id,
        "current_topic_id": current_topic_id or fixture.topic_id,
        "prior_runs": list(
            prior_runs
            if prior_runs is not None
            else ({
                "run_id": fixture.source_run_id,
                "thread_id": fixture.thread_id,
                "topic_id": fixture.topic_id,
                "status": "completed",
            },)
        ),
    }


def _runtime_evaluation_projection_fixture():
    fixture = _authoritative_reuse_review_fixture()
    run_id = fixture.current_run_id
    evidence_ref = f"evidence:{fixture.current_binding.record_ref}"
    bundle = fixture.runtime.build_persistence_bundle(
        fixture.current,
        answer_package={
            "sections": [
                {
                    "section_id": "summary",
                    "payload": {"claims": [{
                        "text": "复用后结论",
                        "claim_type": "comparative_change",
                        "claim_strength": "observed",
                        "evidence_refs": [evidence_ref],
                    }]},
                },
                {
                    "section_id": "evidence",
                    "payload": {"evidence": [{
                        "evidence_ref": evidence_ref,
                        "binding_manifest_ref": fixture.current_binding.record_ref,
                    }]},
                },
            ],
        },
        request={
            "run_id": run_id,
            "thread_id": fixture.thread_id,
            "topic_id": fixture.topic_id,
            "permission_context": {"role": "analyst"},
            "reuse_decisions": [dict(fixture.current.reuse_decisions[0])],
        },
        artifact_path="artifacts/phase7/runtime-eval/answer_package.json",
    )
    fixture.store.save_analysis_runtime_records(run_id=run_id, **bundle)
    fixture.store.add_audit_event(
        "delivery_verifier_completed",
        thread_id=fixture.thread_id,
        topic_id=fixture.topic_id,
        run_id=run_id,
        ref=run_id,
        payload={
            "status": "passed",
            "errors": [],
            "warnings": [],
            "accepted_claim_indexes": [],
            "rejected_claim_indexes": [],
        },
    )
    return fixture, run_id


def _rebind_runtime_publication_digest(fixture, run_id):
    publication = fixture.store.analysis_runtime_records[run_id]
    event = next(
        item
        for item in fixture.store._audit_events
        if item["event_type"] == "analysis_runtime_records_persisted"
        and item["run_id"] == run_id
    )
    event["payload"]["bundle_digest"] = publication["digest"]


_RUNTIME_EVALUATION_RECORD_REF_FIELDS = {
    "query_contracts": "query_contract_id",
    "query_execution_records": "record_ref",
    "rows_records": "record_ref",
    "snapshot_records": "record_ref",
    "completeness_records": "record_ref",
    "capability_binding_records": "record_ref",
    "evidence_manifests": "evidence_ref",
    "context_manifests": "manifest_id",
    "trusted_provenance_records": "record_ref",
    "verified_claims": "claim_ref",
    "repair_attempts": "attempt_ref",
}


def _runtime_evaluation_record_ref(group, payload):
    if group == "claim_links":
        return f"{payload['claim_ref']}\x1f{payload['evidence_ref']}"
    return str(payload[_RUNTIME_EVALUATION_RECORD_REF_FIELDS[group]])


def _postgres_runtime_evaluation_backend(fixture, run_id):
    publication = fixture.store.analysis_runtime_records[run_id]
    runtime_bundle = publication["payload"]
    contract_ref = publication["payload"]["analysis_contract"][
        "analysis_contract_id"
    ]
    persisted_event = next(
        event
        for event in fixture.store.audit_events
        if event["event_type"] == "analysis_runtime_records_persisted"
        and event["run_id"] == run_id
    )
    delivery_event = next(
        event
        for event in fixture.store.audit_events
        if event["event_type"] == "delivery_verifier_completed"
        and event["run_id"] == run_id
    )
    groups = {
        group: [
            {
                "owner_run_ids": [run_id],
                "payload": deepcopy(payload),
            }
            for payload in runtime_bundle[group]
        ]
        for group in (*_RUNTIME_EVALUATION_RECORD_REF_FIELDS, "claim_links")
    }
    record_refs = {
        group: [
            _runtime_evaluation_record_ref(group, payload)
            for payload in runtime_bundle[group]
        ]
        for group in groups
    }
    row = {
        "run_id": run_id,
        "thread_id": fixture.thread_id,
        "turn_id": "turn-runtime-eval",
        "topic_id": fixture.topic_id,
        "run_status": "completed",
        "publication_run_id": run_id,
        "publication_topic_id": fixture.topic_id,
        "publication_digest": publication["digest"],
        "stored_contract_signature": publication["payload"]["analysis_contract"][
            "contract_signature"
        ],
        "analysis_contract_count": 1,
        "publication_index": {
            "schema_version": "analysis-runtime-publication-index.v1",
            "analysis_contract_id": contract_ref,
            "ordered_refs": record_refs,
        },
        "publication_events": [deepcopy(persisted_event)],
        "delivery_verifier_events": [deepcopy(delivery_event)],
        "indexed_analysis_contract": deepcopy(
            fixture.store.analysis_runtime_authority["analysis_contract"][
                contract_ref
            ]
        ),
        "indexed_contract_run_id": run_id,
        "normalized_runtime_groups": groups,
    }

    class Store:
        def _fetchall(self, statement, params):
            if "live_eval_runtime_evaluation_authority_root" in statement:
                assert "analysis_runtime_publications" in statement
                assert "p.payload AS publication_index" in statement
                assert "p.bundle_digest AS publication_digest" in statement
                assert "ac.payload AS indexed_analysis_contract" in statement
                assert "delivery_verifier_completed" in statement
                assert "answer_packages" not in statement
                assert params == {"run_id": run_id}
                return [row]
            assert "live_eval_runtime_evaluation_authority_inventory" in statement
            for table in (
                "query_contracts",
                "query_execution_authority",
                "rows_metadata_authority",
                "snapshot_authority",
                "query_completeness_reports",
                "capability_binding_authority",
                "evidence_manifests",
                "context_manifests",
                "claim_provenance_records",
                "verified_claims",
                "claim_evidence_links",
                "query_repair_attempts",
            ):
                assert f"waje_runtime.{table}" in statement
            assert "answer_packages" not in statement
            assert params["run_id"] == run_id
            assert json.loads(params["ordered_refs"]) == row[
                "publication_index"
            ]["ordered_refs"]
            wrapper_kinds = {
                "query_execution_records": "query_execution",
                "rows_records": "rows",
                "snapshot_records": "snapshot",
                "completeness_records": "completeness",
                "capability_binding_records": "capability_binding",
            }
            return [
                {
                    "record_group": group,
                    "record_ref": entry.get(
                        "record_ref",
                        _runtime_evaluation_record_ref(
                            group,
                            entry["payload"],
                        ),
                    ),
                    "owner_run_ids": deepcopy(entry["owner_run_ids"]),
                    "payload": (
                        {
                            "kind": wrapper_kinds[group],
                            "record": deepcopy(entry["payload"]),
                        }
                        if group in wrapper_kinds
                        else deepcopy(entry["payload"])
                    ),
                }
                for group, entries in row[
                    "normalized_runtime_groups"
                ].items()
                for entry in entries
            ]

    return Store(), row


def _runtime_evaluation_backend(fixture, run_id, backend):
    if backend == "memory":
        return fixture.store, None
    return _postgres_runtime_evaluation_backend(fixture, run_id)


def test_eval_runtime_authority_projection_reads_complete_run_matched_store_chain():
    from tools.phase7.run_live_conversation_system_test import (
        _runtime_authority_resolver_for_store,
    )

    fixture, run_id = _runtime_evaluation_projection_fixture()

    projection = _runtime_authority_resolver_for_store(fixture.store)(run_id)

    assert projection["projection_schema_version"] == "eval-runtime-authority.v1"
    assert projection["run_id"] == run_id
    assert projection["thread_id"] == fixture.thread_id
    assert projection["topic_id"] == fixture.topic_id
    assert projection["publication_digest"]
    assert projection["analysis_contract"]["analysis_contract_id"] == (
        f"analysis:{run_id}:1"
    )
    assert projection["query_contracts"]
    assert projection["query_executions"]
    assert projection["completeness_records"]
    assert projection["capability_bindings"]
    assert projection["evidence_manifests"]
    assert "verified_claims" in projection
    assert "claim_links" in projection
    assert projection["delivery_verifier"]["status"] == "passed"
    assert projection["reuse_decisions"] == [
        dict(fixture.current.reuse_decisions[0])
    ]
    assert all(
        item["analysis_contract_ref"]
        == projection["analysis_contract"]["analysis_contract_id"]
        for item in projection["query_contracts"]
    )


def test_runtime_publication_audit_binds_the_projection_digest():
    fixture, run_id = _runtime_evaluation_projection_fixture()
    persisted = [
        event
        for event in fixture.store.audit_events
        if event["event_type"] == "analysis_runtime_records_persisted"
        and event["run_id"] == run_id
    ]

    assert len(persisted) == 1
    assert persisted[0]["payload"]["bundle_digest"] == (
        fixture.store.analysis_runtime_records[run_id]["digest"]
    )


def test_postgres_runtime_publication_index_keeps_only_ordered_record_refs():
    from bi_agent.conversation.postgres_store import _runtime_publication_index

    fixture, run_id = _runtime_evaluation_projection_fixture()
    bundle = fixture.store.analysis_runtime_records[run_id]["payload"]

    publication_index = _runtime_publication_index(bundle)

    assert set(publication_index) == {
        "schema_version",
        "analysis_contract_id",
        "ordered_refs",
    }
    assert publication_index["schema_version"] == (
        "analysis-runtime-publication-index.v1"
    )
    assert set(publication_index["ordered_refs"]) == {
        *_RUNTIME_EVALUATION_RECORD_REF_FIELDS,
        "claim_links",
    }
    assert publication_index["ordered_refs"]["query_contracts"] == [
        item["query_contract_id"] for item in bundle["query_contracts"]
    ]
    assert publication_index["ordered_refs"]["rows_records"] == [
        item["record_ref"] for item in bundle["rows_records"]
    ]
    assert publication_index["ordered_refs"]["snapshot_records"] == [
        item["record_ref"] for item in bundle["snapshot_records"]
    ]
    assert publication_index["ordered_refs"]["repair_attempts"] == []
    assert "query_contracts" not in publication_index


def test_postgres_eval_runtime_authority_projection_reads_one_normalized_row():
    from tools.phase7.run_live_conversation_system_test import (
        _runtime_authority_resolver_for_store,
    )

    fixture, run_id = _runtime_evaluation_projection_fixture()
    store, row = _postgres_runtime_evaluation_backend(fixture, run_id)

    assert row["publication_index"]["ordered_refs"]["repair_attempts"] == []
    assert row["publication_index"]["ordered_refs"]["rows_records"]
    assert row["publication_index"]["ordered_refs"]["snapshot_records"]

    projection = _runtime_authority_resolver_for_store(store)(run_id)

    assert projection["projection_schema_version"] == "eval-runtime-authority.v1"
    assert projection["run_id"] == run_id
    assert projection["query_executions"]
    assert projection["capability_bindings"]
    assert projection["delivery_verifier"]["status"] == "passed"
    assert projection["admin_audit"]["query_results"]


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        ("missing", "runtime_evaluation_normalized_rows_missing"),
        ("duplicate", "runtime_evaluation_normalized_rows_ambiguous"),
        ("cross_run", "runtime_evaluation_authority_cross_run"),
        ("digest_drift", "runtime_evaluation_publication_digest_mismatch"),
    ],
)
def test_postgres_eval_runtime_authority_projection_fails_closed_on_normalized_row_drift(
    mutation,
    expected_error,
):
    from tools.phase7.run_live_conversation_system_test import (
        _runtime_authority_resolver_for_store,
    )

    fixture, run_id = _runtime_evaluation_projection_fixture()
    store, row = _postgres_runtime_evaluation_backend(fixture, run_id)
    records = row["normalized_runtime_groups"]["query_execution_records"]
    if mutation == "missing":
        records.pop()
    elif mutation == "duplicate":
        records.append(deepcopy(records[0]))
    elif mutation == "cross_run":
        records[0]["owner_run_ids"] = ["run-other"]
    else:
        records[0]["payload"]["execution_status"] = "failed"

    with pytest.raises(ValueError, match=f"^{expected_error}$"):
        _runtime_authority_resolver_for_store(store)(run_id)


@pytest.mark.parametrize("mutation", ["missing_group", "duplicate_ref"])
def test_postgres_eval_runtime_authority_projection_rejects_invalid_ordered_refs(
    mutation,
):
    from tools.phase7.run_live_conversation_system_test import (
        _runtime_authority_resolver_for_store,
    )

    fixture, run_id = _runtime_evaluation_projection_fixture()
    store, row = _postgres_runtime_evaluation_backend(fixture, run_id)
    ordered_refs = row["publication_index"]["ordered_refs"]
    if mutation == "missing_group":
        ordered_refs.pop("repair_attempts")
    else:
        ordered_refs["query_contracts"].append(
            ordered_refs["query_contracts"][0]
        )

    with pytest.raises(
        ValueError,
        match="^runtime_evaluation_publication_index_invalid$",
    ):
        _runtime_authority_resolver_for_store(store)(run_id)


def test_postgres_eval_runtime_authority_projection_rejects_unindexed_run_record():
    from tools.phase7.run_live_conversation_system_test import (
        _runtime_authority_resolver_for_store,
    )

    fixture, run_id = _runtime_evaluation_projection_fixture()
    store, row = _postgres_runtime_evaluation_backend(fixture, run_id)
    extra = deepcopy(
        row["normalized_runtime_groups"]["query_execution_records"][0]
    )
    extra["payload"]["record_ref"] = "query-execution:unindexed"
    row["normalized_runtime_groups"]["query_execution_records"].append(extra)

    with pytest.raises(
        ValueError,
        match="^runtime_evaluation_normalized_rows_unexpected$",
    ):
        _runtime_authority_resolver_for_store(store)(run_id)


@pytest.mark.parametrize("group", ["rows_records", "snapshot_records"])
def test_postgres_eval_runtime_authority_projection_rejects_unowned_global_record(
    group,
):
    from tools.phase7.run_live_conversation_system_test import (
        _runtime_authority_resolver_for_store,
    )

    fixture, run_id = _runtime_evaluation_projection_fixture()
    store, row = _postgres_runtime_evaluation_backend(fixture, run_id)
    row["normalized_runtime_groups"][group][0]["owner_run_ids"] = []

    with pytest.raises(
        ValueError,
        match="^runtime_evaluation_authority_cross_run$",
    ):
        _runtime_authority_resolver_for_store(store)(run_id)


def test_postgres_eval_runtime_authority_projection_rejects_db_ref_payload_drift():
    from tools.phase7.run_live_conversation_system_test import (
        _runtime_authority_resolver_for_store,
    )

    fixture, run_id = _runtime_evaluation_projection_fixture()
    store, row = _postgres_runtime_evaluation_backend(fixture, run_id)
    row["normalized_runtime_groups"]["query_contracts"][0][
        "record_ref"
    ] = "query:db-column-drift"

    with pytest.raises(
        ValueError,
        match="^runtime_evaluation_normalized_rows_invalid$",
    ):
        _runtime_authority_resolver_for_store(store)(run_id)


def test_postgres_eval_runtime_authority_projection_accepts_all_empty_record_groups():
    from bi_agent.runtime.analysis_contracts import analysis_contract_signature
    from bi_agent.runtime.evidence_authority import canonical_digest
    from bi_agent.runtime.runtime_publication_index import (
        RUNTIME_PUBLICATION_RECORD_GROUPS,
    )
    from tools.phase7.run_live_conversation_system_test import (
        _runtime_authority_resolver_for_store,
    )

    fixture, run_id = _runtime_evaluation_projection_fixture()
    store, row = _postgres_runtime_evaluation_backend(fixture, run_id)
    contract = _analysis_contract_gap_authority([], [])
    contract["analysis_contract_id"] = f"analysis:{run_id}:empty"
    contract["contract_signature"] = analysis_contract_signature(contract)
    bundle = {
        "analysis_contract": contract,
        **{group: [] for group in RUNTIME_PUBLICATION_RECORD_GROUPS},
    }
    digest = canonical_digest(bundle)
    row["indexed_analysis_contract"] = contract
    row["stored_contract_signature"] = contract["contract_signature"]
    row["publication_index"] = {
        "schema_version": "analysis-runtime-publication-index.v1",
        "analysis_contract_id": contract["analysis_contract_id"],
        "ordered_refs": {
            group: [] for group in RUNTIME_PUBLICATION_RECORD_GROUPS
        },
    }
    row["normalized_runtime_groups"] = {
        group: [] for group in RUNTIME_PUBLICATION_RECORD_GROUPS
    }
    row["publication_digest"] = digest
    row["publication_events"][0]["payload"]["bundle_digest"] = digest

    projection = _runtime_authority_resolver_for_store(store)(run_id)

    assert projection["analysis_contract"]["analysis_contract_id"] == (
        contract["analysis_contract_id"]
    )
    assert projection["query_contracts"] == []
    assert projection["query_executions"] == []


def test_postgres_eval_runtime_authority_projection_rejects_legacy_publication_index():
    from tools.phase7.run_live_conversation_system_test import (
        _runtime_authority_resolver_for_store,
    )

    fixture, run_id = _runtime_evaluation_projection_fixture()
    store, row = _postgres_runtime_evaluation_backend(fixture, run_id)
    row["publication_index"] = {
        "analysis_contract_id": row["indexed_analysis_contract"][
            "analysis_contract_id"
        ],
        "context_manifest_refs": [],
        "verified_claim_refs": [],
    }

    with pytest.raises(
        ValueError,
        match="^runtime_evaluation_publication_index_invalid$",
    ):
        _runtime_authority_resolver_for_store(store)(run_id)


@pytest.mark.parametrize("backend", ["memory", "postgres"])
def test_eval_runtime_authority_projection_rejects_incomplete_run(backend):
    from tools.phase7.run_live_conversation_system_test import (
        _runtime_authority_resolver_for_store,
    )

    fixture, run_id = _runtime_evaluation_projection_fixture()
    store, row = _runtime_evaluation_backend(fixture, run_id, backend)
    if backend == "memory":
        fixture.store.runs[run_id]["status"] = "failed"
    else:
        row["run_status"] = "failed"

    with pytest.raises(
        ValueError,
        match="^runtime_evaluation_run_incomplete$",
    ):
        _runtime_authority_resolver_for_store(store)(run_id)


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize(
    "mutation",
    ["status", "errors", "errors_missing", "errors_wrong_type"],
)
def test_eval_runtime_authority_projection_rejects_failed_delivery_verifier(
    backend,
    mutation,
):
    from tools.phase7.run_live_conversation_system_test import (
        _runtime_authority_resolver_for_store,
    )

    fixture, run_id = _runtime_evaluation_projection_fixture()
    store, row = _runtime_evaluation_backend(fixture, run_id, backend)
    if backend == "memory":
        verifier = next(
            event["payload"]
            for event in fixture.store._audit_events
            if event["event_type"] == "delivery_verifier_completed"
            and event["run_id"] == run_id
        )
    else:
        verifier = row["delivery_verifier_events"][0]["payload"]
    if mutation == "status":
        verifier["status"] = "failed"
    elif mutation == "errors":
        verifier["errors"] = ["claim_without_evidence"]
    elif mutation == "errors_missing":
        verifier.pop("errors")
    else:
        verifier["errors"] = ""

    with pytest.raises(
        ValueError,
        match="^runtime_evaluation_delivery_verifier_rejected$",
    ):
        _runtime_authority_resolver_for_store(store)(run_id)


@pytest.mark.parametrize("backend", ["memory", "postgres"])
def test_eval_runtime_authority_projection_rejects_publication_index_drift(backend):
    from bi_agent.runtime.analysis_contracts import (
        analysis_contract_from_dict,
        analysis_contract_signature,
    )
    from bi_agent.runtime.evidence_authority import canonical_digest
    from tools.phase7.run_live_conversation_system_test import (
        _runtime_authority_resolver_for_store,
    )

    fixture, run_id = _runtime_evaluation_projection_fixture()
    store, row = _runtime_evaluation_backend(fixture, run_id, backend)
    if backend == "memory":
        publication = fixture.store.analysis_runtime_records[run_id]
        publication_event = next(
            event
            for event in fixture.store._audit_events
            if event["event_type"] == "analysis_runtime_records_persisted"
            and event["run_id"] == run_id
        )
    else:
        row["publication_index"]["analysis_contract_id"] = "analysis:other:1"
        publication_event = row["publication_events"][0]
    if backend == "memory":
        contract = publication["payload"]["analysis_contract"]
        contract["question_families"] = ["revenue_health_review"]
        unsigned_contract = {
            key: value
            for key, value in contract.items()
            if key != "contract_signature"
        }
        contract["contract_signature"] = analysis_contract_signature(
            analysis_contract_from_dict(unsigned_contract)
        )
        publication["digest"] = canonical_digest(publication["payload"])
        publication_event["payload"]["bundle_digest"] = publication["digest"]

    with pytest.raises(
        ValueError,
        match="^runtime_evaluation_authority_index_mismatch$",
    ):
        _runtime_authority_resolver_for_store(store)(run_id)


@pytest.mark.parametrize("backend", ["memory", "postgres"])
def test_eval_runtime_authority_projection_rejects_cross_owner_delivery_event(
    backend,
):
    from tools.phase7.run_live_conversation_system_test import (
        _runtime_authority_resolver_for_store,
    )

    fixture, run_id = _runtime_evaluation_projection_fixture()
    store, row = _runtime_evaluation_backend(fixture, run_id, backend)
    if backend == "memory":
        delivery_event = next(
            event
            for event in fixture.store._audit_events
            if event["event_type"] == "delivery_verifier_completed"
            and event["run_id"] == run_id
        )
    else:
        delivery_event = row["delivery_verifier_events"][0]
    delivery_event["thread_id"] = "thread-other"

    with pytest.raises(
        ValueError,
        match="^runtime_evaluation_authority_cross_run$",
    ):
        _runtime_authority_resolver_for_store(store)(run_id)


def test_runtime_audit_reviews_use_persisted_projection_instead_of_artifact_producers(
    tmp_path,
    monkeypatch,
):
    from tools.phase7 import run_live_conversation_system_test as system_test

    fixture, run_id = _runtime_evaluation_projection_fixture()
    projection_resolver = system_test._runtime_authority_resolver_for_store(
        fixture.store
    )
    contract_ref = fixture.store.analysis_runtime_records[run_id]["payload"][
        "analysis_contract"
    ]["analysis_contract_id"]
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    artifact_path = artifact_root / "answer-package.json"
    artifact_path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "sections": [
                    {
                        "section_id": "summary",
                        "payload": {
                            "claims": [
                                {
                                    "claim_ref": "forged-client-claim",
                                    "producer": "clickhouse",
                                }
                            ]
                        },
                    }
                ],
                "admin_audit": {
                    "analysis_runtime_persistence": {
                        "status": "persisted",
                        "analysis_contract_ref": contract_ref,
                    },
                    "verifier": {"status": "passed"},
                    "reuse_decisions": [
                        {"decision": "reuse", "source_ref": "forged-client-ref"}
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(system_test, "ROOT", tmp_path)

    authority = system_test._runtime_audit_package(
        {
            "run_id": run_id,
            "artifact_path": str(artifact_path),
            "answer_package": {
                "admin_audit": {
                    "analysis_runtime_persistence": {
                        "status": "persisted",
                        "analysis_contract_ref": contract_ref,
                    }
                }
            },
        },
        authority_resolver=projection_resolver,
    )

    assert authority["projection_schema_version"] == "eval-runtime-authority.v1"
    assert authority["admin_audit"]["query_contracts"]
    assert authority["admin_audit"]["query_results"]
    assert authority["admin_audit"]["capability_bindings"]
    assert authority["admin_audit"]["verifier"] == authority["delivery_verifier"]
    assert authority["admin_audit"]["reuse_decisions"] == [
        dict(fixture.current.reuse_decisions[0])
    ]
    assert not any(
        claim.get("producer") == "clickhouse"
        for section in authority["sections"]
        for claim in section.get("payload", {}).get("claims", [])
    )


def test_real_clickhouse_review_accepts_the_already_resolved_runtime_projection(
    monkeypatch,
):
    from tools.phase7 import run_live_conversation_system_test as system_test

    monkeypatch.setattr(
        system_test,
        "_runtime_audit_package",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("runtime authority must not be resolved twice")
        ),
    )

    review = system_test._real_clickhouse_review(
        {},
        real_clickhouse=True,
        runtime_authority={"_authority_error": "projection_failed"},
    )

    assert review["real_clickhouse_verified"] is False
    assert "runtime_authority_error:projection_failed" in review["issues"]


def test_eval_runtime_authority_projection_does_not_treat_claim_producer_as_execution():
    from bi_agent.runtime.evidence_authority import canonical_digest
    from tools.phase7.run_live_conversation_system_test import (
        _runtime_authority_resolver_for_store,
    )

    fixture, run_id = _runtime_evaluation_projection_fixture()
    publication = fixture.store.analysis_runtime_records[run_id]
    publication["payload"]["query_execution_records"] = []
    publication["digest"] = canonical_digest(publication["payload"])
    _rebind_runtime_publication_digest(fixture, run_id)

    with pytest.raises(
        ValueError,
        match="^runtime_evaluation_query_execution_missing$",
    ):
        _runtime_authority_resolver_for_store(fixture.store)(run_id)


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        ("cross_run", "runtime_evaluation_authority_cross_run"),
        ("duplicate", "runtime_evaluation_query_contract_ambiguous"),
        ("digest_drift", "runtime_evaluation_publication_digest_mismatch"),
    ],
)
def test_eval_runtime_authority_projection_fails_closed_on_owner_duplicate_or_digest_drift(
    mutation,
    expected_error,
):
    from bi_agent.runtime.evidence_authority import canonical_digest
    from tools.phase7.run_live_conversation_system_test import (
        _runtime_authority_resolver_for_store,
    )

    fixture, run_id = _runtime_evaluation_projection_fixture()
    publication = fixture.store.analysis_runtime_records[run_id]
    if mutation == "cross_run":
        publication["payload"]["query_contracts"][0][
            "analysis_contract_ref"
        ] = "analysis:run-other:1"
        publication["digest"] = canonical_digest(publication["payload"])
    elif mutation == "duplicate":
        publication["payload"]["query_contracts"].append(
            deepcopy(publication["payload"]["query_contracts"][0])
        )
        publication["digest"] = canonical_digest(publication["payload"])
    else:
        publication["payload"]["analysis_contract"]["question_families"] = [
            "revenue_health_review"
        ]
    if mutation != "digest_drift":
        _rebind_runtime_publication_digest(fixture, run_id)

    with pytest.raises(ValueError, match=f"^{expected_error}$"):
        _runtime_authority_resolver_for_store(fixture.store)(run_id)


@pytest.mark.parametrize(
    "expected",
    [
        None,
        "market_health_compare",
        {},
        {"capability_id": "market_health_compare"},
        {"capability_id": "", "dataset_ids": ["market_dashboard"]},
        {"capability_id": "market_health_compare", "dataset_ids": []},
        {
            "capability_id": "market_health_compare",
            "dataset_ids": ["market_dashboard"],
            "extra": True,
        },
    ],
)
def test_required_reuse_review_rejects_invalid_exact_expectation_schema(expected):
    from tools.phase7.run_live_conversation_system_test import _review_required_reuse

    fixture = _authoritative_reuse_review_fixture()
    review = _review_required_reuse(
        fixture.authority,
        expected,
        registry=fixture.registry,
        evidence_resolver=fixture.resolver,
        rows_loader=fixture.rows_loader,
        release_resolver=fixture.release_resolver,
        conversation_store=fixture.store,
        case_lineage=_reuse_case_lineage(fixture),
    )

    assert review["passed"] is False
    assert review["errors"] == ["expected_reuse_schema_invalid"]


def test_required_reuse_review_accepts_actual_market_dashboard_provenance():
    from tools.phase7.run_live_conversation_system_test import _review_required_reuse

    fixture = _authoritative_market_reuse_review_fixture()
    signed_candidate = fixture.store.resolve_result_candidate_authority(
        result_ref=fixture.source_result_ref,
        topic_id=fixture.topic_id,
    )["result_ref_record"]["payload"]
    decision = fixture.authority["admin_audit"]["reuse_decisions"][0]
    current_record = fixture.resolver.resolve_query_execution(
        fixture.current_result_ref
    )
    assert {
        signed_candidate["candidate_signature"],
        decision["candidate_signature"],
        current_record.result_payload["provider_stats"]["candidate_signature"],
    } == {signed_candidate["candidate_signature"]}
    review = _review_required_reuse(
        fixture.authority,
        {
            "capability_id": "market_health_compare",
            "dataset_ids": ["market_dashboard"],
        },
        registry=fixture.registry,
        evidence_resolver=fixture.resolver,
        rows_loader=fixture.rows_loader,
        release_resolver=fixture.release_resolver,
        conversation_store=fixture.store,
        case_lineage=_reuse_case_lineage(fixture),
    )

    assert review == {
        "passed": True,
        "errors": [],
        "source_result_ref": fixture.source_result_ref,
        "current_result_ref": fixture.current_result_ref,
        "query_contract_ref": fixture.query_contract_ref,
        "capability_id": "market_health_compare",
        "dataset_ids": ["market_dashboard"],
    }


def test_required_reuse_review_rejects_decision_candidate_signature_tamper():
    from tools.phase7.run_live_conversation_system_test import _review_required_reuse

    fixture = _authoritative_reuse_review_fixture()
    fixture.authority["admin_audit"]["reuse_decisions"][0][
        "candidate_signature"
    ] = "0" * 64
    review = _review_required_reuse(
        fixture.authority,
        {"capability_id": "compare_periods", "dataset_ids": ["paid_order_success"]},
        registry=fixture.registry,
        evidence_resolver=fixture.resolver,
        rows_loader=fixture.rows_loader,
        release_resolver=fixture.release_resolver,
        conversation_store=fixture.store,
        case_lineage=_reuse_case_lineage(fixture),
    )

    assert review["passed"] is False
    assert "reuse_candidate_signature_lineage_mismatch" in review["errors"]


def test_required_reuse_review_rejects_current_result_candidate_signature_tamper():
    from tests.phase4.test_authoritative_query_chain import (
        _replace_binding_completeness,
    )
    from tools.phase7.run_live_conversation_system_test import _review_required_reuse

    fixture = _authoritative_reuse_review_fixture()
    current_record = fixture.resolver.resolve_query_execution(
        fixture.current_result_ref
    )
    changed_provider_stats = {
        **current_record.result_payload["provider_stats"],
        "candidate_signature": "0" * 64,
    }
    changed_record = _resigned_query_execution(
        current_record,
        provider_stats=changed_provider_stats,
    )
    changed_binding = _binding_with_query_execution(
        fixture.current_binding,
        current_record,
        changed_record,
    )
    completeness = fixture.resolver.resolve_completeness(
        fixture.current_binding.completeness_record_refs[0]
    )
    changed_completeness = _resigned_completeness_provider_stats(
        completeness,
        changed_provider_stats,
    )
    changed_binding = _replace_binding_completeness(
        changed_binding,
        changed_completeness,
    )
    evidence = fixture.authority["sections"][0]["payload"]["evidence"][0]
    evidence["binding_manifest_ref"] = changed_binding.record_ref
    evidence["binding_manifest_digest"] = changed_binding.binding_digest
    review = _review_required_reuse(
        fixture.authority,
        {"capability_id": "compare_periods", "dataset_ids": ["paid_order_success"]},
        registry=fixture.registry,
        evidence_resolver=_CurrentReuseRecordResolver(
            fixture.resolver,
            changed_binding,
            changed_record,
            changed_completeness,
        ),
        rows_loader=fixture.rows_loader,
        release_resolver=fixture.release_resolver,
        conversation_store=fixture.store,
        case_lineage=_reuse_case_lineage(fixture),
    )

    assert review["passed"] is False
    assert "reuse_candidate_signature_lineage_mismatch" in review["errors"]


@pytest.mark.parametrize("owner_axis", ["thread_id", "topic_id", "run_id"])
def test_required_reuse_review_rejects_current_store_run_owner_tamper(owner_axis):
    from tools.phase7.run_live_conversation_system_test import _review_required_reuse

    fixture = _authoritative_reuse_review_fixture()
    if owner_axis == "run_id":
        fixture.store.runs["run-reuse-review-foreign"] = fixture.store.runs.pop(
            fixture.current_run_id
        )
    else:
        fixture.store.runs[fixture.current_run_id][owner_axis] = (
            f"{owner_axis}:foreign"
        )
    review = _review_required_reuse(
        fixture.authority,
        {"capability_id": "compare_periods", "dataset_ids": ["paid_order_success"]},
        registry=fixture.registry,
        evidence_resolver=fixture.resolver,
        rows_loader=fixture.rows_loader,
        release_resolver=fixture.release_resolver,
        conversation_store=fixture.store,
        case_lineage=_reuse_case_lineage(fixture),
    )

    assert review["passed"] is False
    assert "reuse_current_run_owner_invalid" in review["errors"]


def test_required_reuse_review_rejects_integrity_clean_current_query_owner_tamper():
    from tools.phase7.run_live_conversation_system_test import _review_required_reuse

    fixture = _authoritative_reuse_review_fixture()
    current_record = fixture.resolver.resolve_query_execution(
        fixture.current_result_ref
    )
    changed_record = _resigned_query_execution(
        current_record,
        contract=replace(
            current_record.contract,
            analysis_contract_ref="analysis:foreign-current-owner:1",
        ),
    )
    changed_binding = _binding_with_query_execution(
        fixture.current_binding,
        current_record,
        changed_record,
    )
    evidence = fixture.authority["sections"][0]["payload"]["evidence"][0]
    evidence["binding_manifest_ref"] = changed_binding.record_ref
    evidence["binding_manifest_digest"] = changed_binding.binding_digest
    review = _review_required_reuse(
        fixture.authority,
        {"capability_id": "compare_periods", "dataset_ids": ["paid_order_success"]},
        registry=fixture.registry,
        evidence_resolver=_CurrentReuseRecordResolver(
            fixture.resolver,
            changed_binding,
            changed_record,
        ),
        rows_loader=fixture.rows_loader,
        release_resolver=fixture.release_resolver,
        conversation_store=fixture.store,
        case_lineage=_reuse_case_lineage(fixture),
    )

    assert review["passed"] is False
    assert "reuse_current_query_contract_owner_invalid" in review["errors"]


def test_required_reuse_review_resolves_exact_current_binding_and_source_result():
    from tools.phase7.run_live_conversation_system_test import _review_required_reuse

    fixture = _authoritative_reuse_review_fixture()
    assert fixture.source_binding.analysis_contract_ref != (
        fixture.current_binding.analysis_contract_ref
    )
    review = _review_required_reuse(
        fixture.authority,
        {"capability_id": "compare_periods", "dataset_ids": ["paid_order_success"]},
        registry=fixture.registry,
        evidence_resolver=fixture.resolver,
        rows_loader=fixture.rows_loader,
        release_resolver=fixture.release_resolver,
        conversation_store=fixture.store,
        case_lineage=_reuse_case_lineage(fixture),
    )

    assert review == {
        "passed": True,
        "errors": [],
        "source_result_ref": fixture.source_result_ref,
        "current_result_ref": fixture.current_result_ref,
        "query_contract_ref": fixture.query_contract_ref,
        "capability_id": "compare_periods",
        "dataset_ids": ["paid_order_success"],
    }


def test_required_reuse_review_rejects_nested_unbound_marker():
    from tools.phase7.run_live_conversation_system_test import _review_required_reuse

    fixture = _authoritative_reuse_review_fixture()
    marker = fixture.authority["admin_audit"].pop("reuse_decisions")
    fixture.authority["sections"].append({"payload": {"reuse_decisions": marker}})

    review = _review_required_reuse(
        fixture.authority,
        {"capability_id": "compare_periods", "dataset_ids": ["paid_order_success"]},
        registry=fixture.registry,
        evidence_resolver=fixture.resolver,
        rows_loader=fixture.rows_loader,
        release_resolver=fixture.release_resolver,
        conversation_store=fixture.store,
        case_lineage=_reuse_case_lineage(fixture),
    )

    assert review["passed"] is False
    assert review["errors"] == ["admin_reuse_decision_missing"]


def test_required_reuse_review_deduplicates_same_authoritative_binding_ref():
    from tools.phase7.run_live_conversation_system_test import _review_required_reuse

    fixture = _authoritative_reuse_review_fixture()
    duplicate = json.loads(json.dumps(fixture.authority["sections"][0]))
    duplicate["payload"]["evidence"][0]["evidence_ref"] = (
        "evidence:reuse-review-current-alias"
    )
    fixture.authority["sections"].append(duplicate)

    review = _review_required_reuse(
        fixture.authority,
        {"capability_id": "compare_periods", "dataset_ids": ["paid_order_success"]},
        registry=fixture.registry,
        evidence_resolver=fixture.resolver,
        rows_loader=fixture.rows_loader,
        release_resolver=fixture.release_resolver,
        conversation_store=fixture.store,
        case_lineage=_reuse_case_lineage(fixture),
    )

    assert review["passed"] is True


def test_required_reuse_review_accepts_expected_binding_with_unrelated_sibling_binding():
    from tools.phase7.run_live_conversation_system_test import _review_required_reuse

    fixture = _authoritative_reuse_review_fixture()
    sibling = fixture.source_binding
    assert fixture.current_result_ref not in {
        *sibling.result_refs,
        *sibling.validation_result_refs,
    }
    fixture.authority["sections"][0]["payload"]["evidence"].append({
        "evidence_ref": "evidence:reuse-review-source-sibling",
        "binding_manifest_ref": sibling.record_ref,
        "binding_manifest_digest": sibling.binding_digest,
        "result_refs": [
            *sibling.result_refs,
            *sibling.validation_result_refs,
        ],
    })

    review = _review_required_reuse(
        fixture.authority,
        {"capability_id": "compare_periods", "dataset_ids": ["paid_order_success"]},
        registry=fixture.registry,
        evidence_resolver=fixture.resolver,
        rows_loader=fixture.rows_loader,
        release_resolver=fixture.release_resolver,
        conversation_store=fixture.store,
        case_lineage=_reuse_case_lineage(fixture),
    )

    assert review["passed"] is True
    assert review["current_result_ref"] == fixture.current_result_ref


def test_required_reuse_review_rejects_two_expected_bindings_as_ambiguous():
    from bi_agent.runtime.evidence_authority import (
        runtime_evidence_record_integrity_errors,
    )
    from tests.phase4.test_authoritative_query_chain import _resign_binding
    from tools.phase7.run_live_conversation_system_test import _review_required_reuse

    fixture = _authoritative_reuse_review_fixture()
    sibling_plan = {
        **fixture.current_binding.plan_payload,
        "review_binding_alias": "parallel-expected-binding",
    }
    sibling = _resign_binding(
        fixture.current_binding,
        plan_payload=sibling_plan,
    )
    assert sibling.record_ref != fixture.current_binding.record_ref
    assert runtime_evidence_record_integrity_errors(sibling) == ()
    fixture.authority["sections"][0]["payload"]["evidence"].append({
        "evidence_ref": "evidence:reuse-review-current-sibling",
        "binding_manifest_ref": sibling.record_ref,
        "binding_manifest_digest": sibling.binding_digest,
        "result_refs": [
            *sibling.result_refs,
            *sibling.validation_result_refs,
        ],
    })

    review = _review_required_reuse(
        fixture.authority,
        {"capability_id": "compare_periods", "dataset_ids": ["paid_order_success"]},
        registry=fixture.registry,
        evidence_resolver=_AdditionalBindingResolver(fixture.resolver, sibling),
        rows_loader=fixture.rows_loader,
        release_resolver=fixture.release_resolver,
        conversation_store=fixture.store,
        case_lineage=_reuse_case_lineage(fixture),
    )

    assert review["passed"] is False
    assert review["errors"] == ["reuse_current_binding_ambiguous"]


def test_required_reuse_review_ignores_unrelated_full_package_decision():
    from tools.phase7.run_live_conversation_system_test import _review_required_reuse

    fixture = _authoritative_reuse_review_fixture()
    fixture.authority["admin_audit"]["reuse_decisions"].append({
        "source_ref": "result:unrelated-source",
        "result_ref": "result:unrelated-current",
        "decision": "reuse",
        "reason": "validated_authoritative_query_chain",
        "can_support_claim": True,
        "requires_rerun": False,
        "query_contract_ref": "query:unrelated",
    })

    review = _review_required_reuse(
        fixture.authority,
        {"capability_id": "compare_periods", "dataset_ids": ["paid_order_success"]},
        registry=fixture.registry,
        evidence_resolver=fixture.resolver,
        rows_loader=fixture.rows_loader,
        release_resolver=fixture.release_resolver,
        conversation_store=fixture.store,
        case_lineage=_reuse_case_lineage(fixture),
    )

    assert review["passed"] is True


def test_required_reuse_review_rejects_two_valid_expected_path_decisions():
    from tools.phase7.run_live_conversation_system_test import _review_required_reuse

    fixture = _authoritative_reuse_review_fixture()
    decision = fixture.authority["admin_audit"]["reuse_decisions"][0]
    fixture.authority["admin_audit"]["reuse_decisions"].append(dict(decision))

    review = _review_required_reuse(
        fixture.authority,
        {"capability_id": "compare_periods", "dataset_ids": ["paid_order_success"]},
        registry=fixture.registry,
        evidence_resolver=fixture.resolver,
        rows_loader=fixture.rows_loader,
        release_resolver=fixture.release_resolver,
        conversation_store=fixture.store,
        case_lineage=_reuse_case_lineage(fixture),
    )

    assert review["passed"] is False
    assert review["errors"] == ["expected_reuse_tuple_ambiguous"]


def test_required_reuse_review_rejects_clean_wrong_source_lookup():
    from tools.phase7.run_live_conversation_system_test import _review_required_reuse

    fixture = _authoritative_reuse_review_fixture()

    class WrongSourceResolver(_ClaimAuthorityResolver):
        def resolve_query_execution(self, result_ref):
            if result_ref == fixture.source_result_ref:
                return fixture.resolver.resolve_query_execution(
                    fixture.current_result_ref
                )
            return super().resolve_query_execution(result_ref)

    review = _review_required_reuse(
        fixture.authority,
        {"capability_id": "compare_periods", "dataset_ids": ["paid_order_success"]},
        registry=fixture.registry,
        evidence_resolver=WrongSourceResolver(fixture.resolver),
        rows_loader=fixture.rows_loader,
        release_resolver=fixture.release_resolver,
        conversation_store=fixture.store,
        case_lineage=_reuse_case_lineage(fixture),
    )

    assert review["passed"] is False
    assert "reuse_source_result_authority_invalid" in review["errors"]


def test_required_reuse_review_rejects_clean_source_row_mismatch():
    from tools.phase7.run_live_conversation_system_test import _review_required_reuse
    from tests.phase7.test_analysis_runtime_reuse import (
        _candidate,
        _publish_source,
        _source_request,
    )

    fixture = _authoritative_reuse_review_fixture()
    original_aggregate = fixture.provider.aggregate

    def changed_rows(*args, **kwargs):
        result = original_aggregate(*args, **kwargs)
        rows = tuple(
            {
                **row,
                "paid_amount": float(row["paid_amount"]) + 11.0,
            }
            for row in result.rows
        )
        return replace(result, rows=rows)

    fixture.provider.aggregate = changed_rows
    alternative = fixture.runtime.execute(
        _source_request("run-reuse-review-row-tamper", fixture.topic_id)
    )
    alternative_candidate = _candidate(
        fixture.runtime,
        alternative,
        fixture.signed_snapshots,
    )
    _publish_source(
        fixture.runtime,
        fixture.store,
        fixture.topic_id,
        alternative,
        alternative_candidate,
    )
    decision = fixture.authority["admin_audit"]["reuse_decisions"][0]
    decision["source_ref"] = alternative.query_results[0].result_ref

    review = _review_required_reuse(
        fixture.authority,
        {"capability_id": "compare_periods", "dataset_ids": ["paid_order_success"]},
        registry=fixture.registry,
        evidence_resolver=fixture.resolver,
        rows_loader=fixture.rows_loader,
        release_resolver=fixture.release_resolver,
        conversation_store=fixture.store,
        case_lineage=_reuse_case_lineage(
            fixture,
            prior_runs=({
                "run_id": "run-reuse-review-row-tamper",
                "thread_id": fixture.thread_id,
                "topic_id": fixture.topic_id,
                "status": "completed",
            },),
        ),
    )

    assert review["passed"] is False
    assert "reuse_source_rows_mismatch" in review["errors"]


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        ("same_result", "reuse_source_current_result_alias"),
        ("reason", "reuse_decision_reason_invalid"),
    ],
)
def test_required_reuse_review_requires_distinct_results_and_exact_reason(
    mutation, expected_error
):
    from tools.phase7.run_live_conversation_system_test import _review_required_reuse

    fixture = _authoritative_reuse_review_fixture()
    decision = fixture.authority["admin_audit"]["reuse_decisions"][0]
    if mutation == "same_result":
        decision["source_ref"] = decision["result_ref"]
    else:
        decision["reason"] = "same_query_probably_reusable"

    review = _review_required_reuse(
        fixture.authority,
        {"capability_id": "compare_periods", "dataset_ids": ["paid_order_success"]},
        registry=fixture.registry,
        evidence_resolver=fixture.resolver,
        rows_loader=fixture.rows_loader,
        release_resolver=fixture.release_resolver,
        conversation_store=fixture.store,
        case_lineage=_reuse_case_lineage(fixture),
    )

    assert review["passed"] is False
    assert expected_error in review["errors"]


@pytest.mark.parametrize(
    ("field", "value", "expected_error"),
    [
        ("source_ref", "result:forged-source", "reuse_source_result_mismatch"),
        ("result_ref", "result:forged-current", "reuse_current_result_mismatch"),
        ("query_contract_ref", "query:forged", "reuse_query_contract_mismatch"),
    ],
)
def test_required_reuse_review_rejects_tampered_exact_refs(
    field, value, expected_error
):
    from tools.phase7.run_live_conversation_system_test import _review_required_reuse

    fixture = _authoritative_reuse_review_fixture()
    fixture.authority["admin_audit"]["reuse_decisions"][0][field] = value

    review = _review_required_reuse(
        fixture.authority,
        {"capability_id": "compare_periods", "dataset_ids": ["paid_order_success"]},
        registry=fixture.registry,
        evidence_resolver=fixture.resolver,
        rows_loader=fixture.rows_loader,
        release_resolver=fixture.release_resolver,
        conversation_store=fixture.store,
        case_lineage=_reuse_case_lineage(fixture),
    )

    assert review["passed"] is False
    assert expected_error in review["errors"]


def test_required_reuse_review_rejects_source_candidate_from_same_run():
    from tools.phase7.run_live_conversation_system_test import _review_required_reuse

    fixture = _authoritative_reuse_review_fixture()
    review = _review_required_reuse(
        fixture.authority,
        {"capability_id": "compare_periods", "dataset_ids": ["paid_order_success"]},
        registry=fixture.registry,
        evidence_resolver=fixture.resolver,
        rows_loader=fixture.rows_loader,
        release_resolver=fixture.release_resolver,
        conversation_store=fixture.store,
        case_lineage=_reuse_case_lineage(
            fixture,
            current_run_id=fixture.source_run_id,
        ),
    )

    assert review["passed"] is False
    assert "reuse_source_run_not_prior" in review["errors"]


def test_required_reuse_review_rejects_source_candidate_from_other_topic():
    from tools.phase7.run_live_conversation_system_test import _review_required_reuse

    fixture = _authoritative_reuse_review_fixture()
    review = _review_required_reuse(
        fixture.authority,
        {"capability_id": "compare_periods", "dataset_ids": ["paid_order_success"]},
        registry=fixture.registry,
        evidence_resolver=fixture.resolver,
        rows_loader=fixture.rows_loader,
        release_resolver=fixture.release_resolver,
        conversation_store=fixture.store,
        case_lineage=_reuse_case_lineage(
            fixture,
            current_topic_id="topic:other-eval-topic",
        ),
    )

    assert review["passed"] is False
    assert "reuse_source_candidate_authority_invalid" in review["errors"]


def test_required_reuse_review_rejects_unpublished_source_result():
    from tools.phase7.run_live_conversation_system_test import _review_required_reuse

    fixture = _authoritative_reuse_review_fixture()
    fixture.store.result_refs[fixture.topic_id].clear()
    review = _review_required_reuse(
        fixture.authority,
        {"capability_id": "compare_periods", "dataset_ids": ["paid_order_success"]},
        registry=fixture.registry,
        evidence_resolver=fixture.resolver,
        rows_loader=fixture.rows_loader,
        release_resolver=fixture.release_resolver,
        conversation_store=fixture.store,
        case_lineage=_reuse_case_lineage(fixture),
    )

    assert review["passed"] is False
    assert "reuse_source_candidate_authority_invalid" in review["errors"]


def test_required_reuse_review_rejects_source_run_outside_prior_case_lineage():
    from tools.phase7.run_live_conversation_system_test import _review_required_reuse

    fixture = _authoritative_reuse_review_fixture()
    review = _review_required_reuse(
        fixture.authority,
        {"capability_id": "compare_periods", "dataset_ids": ["paid_order_success"]},
        registry=fixture.registry,
        evidence_resolver=fixture.resolver,
        rows_loader=fixture.rows_loader,
        release_resolver=fixture.release_resolver,
        conversation_store=fixture.store,
        case_lineage=_reuse_case_lineage(fixture, prior_runs=()),
    )

    assert review["passed"] is False
    assert "reuse_source_run_not_prior" in review["errors"]


def test_required_reuse_review_rejects_tampered_candidate_signature():
    from tools.phase7.run_live_conversation_system_test import _review_required_reuse

    fixture = _authoritative_reuse_review_fixture()
    record = fixture.store.result_refs[fixture.topic_id][0]
    fixture.store.result_refs[fixture.topic_id][0] = replace(
        record,
        payload={**record.payload, "candidate_signature": "0" * 64},
    )
    review = _review_required_reuse(
        fixture.authority,
        {"capability_id": "compare_periods", "dataset_ids": ["paid_order_success"]},
        registry=fixture.registry,
        evidence_resolver=fixture.resolver,
        rows_loader=fixture.rows_loader,
        release_resolver=fixture.release_resolver,
        conversation_store=fixture.store,
        case_lineage=_reuse_case_lineage(fixture),
    )

    assert review["passed"] is False
    assert "reuse_source_candidate_authority_invalid" in review["errors"]


def test_required_reuse_review_rejects_degraded_current_binding():
    from tools.phase7.run_live_conversation_system_test import _review_required_reuse

    fixture = _authoritative_reuse_review_fixture()
    degraded = _resigned_claim_binding(fixture.current_binding, status="degraded")
    evidence = fixture.authority["sections"][0]["payload"]["evidence"][0]
    evidence["binding_manifest_ref"] = degraded.record_ref
    evidence["binding_manifest_digest"] = degraded.binding_digest
    review = _review_required_reuse(
        fixture.authority,
        {"capability_id": "compare_periods", "dataset_ids": ["paid_order_success"]},
        registry=fixture.registry,
        evidence_resolver=_AdditionalBindingResolver(fixture.resolver, degraded),
        rows_loader=fixture.rows_loader,
        release_resolver=fixture.release_resolver,
        conversation_store=fixture.store,
        case_lineage=_reuse_case_lineage(fixture),
    )

    assert review["passed"] is False
    assert "reuse_current_chain_not_claim_ready" in review["errors"]


def test_required_reuse_review_rejects_noncomplete_current_input_report():
    from tools.phase7.run_live_conversation_system_test import _review_required_reuse

    fixture = _authoritative_reuse_review_fixture()
    noncomplete = _resigned_claim_binding(
        fixture.current_binding,
        input_completeness_statuses=("partial",),
    )
    evidence = fixture.authority["sections"][0]["payload"]["evidence"][0]
    evidence["binding_manifest_ref"] = noncomplete.record_ref
    evidence["binding_manifest_digest"] = noncomplete.binding_digest
    review = _review_required_reuse(
        fixture.authority,
        {"capability_id": "compare_periods", "dataset_ids": ["paid_order_success"]},
        registry=fixture.registry,
        evidence_resolver=_AdditionalBindingResolver(fixture.resolver, noncomplete),
        rows_loader=fixture.rows_loader,
        release_resolver=fixture.release_resolver,
        conversation_store=fixture.store,
        case_lineage=_reuse_case_lineage(fixture),
    )

    assert review["passed"] is False
    assert "reuse_current_chain_not_claim_ready" in review["errors"]


def test_required_reuse_review_rejects_nonready_source_candidate_chain():
    from bi_agent.conversation.models import sign_result_reuse_candidate
    from tools.phase7.run_live_conversation_system_test import _review_required_reuse

    fixture = _authoritative_reuse_review_fixture()
    degraded = _resigned_claim_binding(fixture.source_binding, status="degraded")
    record = fixture.store.result_refs[fixture.topic_id][0]
    payload = sign_result_reuse_candidate({
        **record.payload,
        "binding_record_refs": [degraded.record_ref],
        "binding_record_digests": [degraded.binding_digest],
    })
    fixture.store.result_refs[fixture.topic_id][0] = replace(record, payload=payload)
    review = _review_required_reuse(
        fixture.authority,
        {"capability_id": "compare_periods", "dataset_ids": ["paid_order_success"]},
        registry=fixture.registry,
        evidence_resolver=_AdditionalBindingResolver(fixture.resolver, degraded),
        rows_loader=fixture.rows_loader,
        release_resolver=fixture.release_resolver,
        conversation_store=fixture.store,
        case_lineage=_reuse_case_lineage(fixture),
    )

    assert review["passed"] is False
    assert "reuse_source_chain_not_claim_ready" in review["errors"]


def test_all_suite_claim_ceilings_use_runtime_maximum_strength_taxonomy():
    from tools.phase7.run_live_conversation_system_test import load_suite_cases

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    for suite in ("fixed-eight", "platform-current-data"):
        for case in load_suite_cases(suite):
            for turn in case["turns"]:
                ceiling = str(
                    (turn.get("scenario") or {}).get("allowed_claim_ceiling") or ""
                )
                assert registry.maximum_claim_strength_rank(ceiling) >= 0


def test_obligation_review_resolves_contract_and_reports_typed_gaps():
    from tools.phase7.run_live_conversation_system_test import (
        review_case_obligations,
    )

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    turn = {
        "accepted_graph": [
            "data_quality_profile",
            "driver_decomposition",
            "compare_periods",
            "answer_verify",
            "metric_timeseries",
            "metric_coverage_profile",
        ],
        "scenario": {
            "question_family": "paid_amount_change_explanation",
            "target_metrics": ["paid_amount"],
            "baselines": ["previous_day"],
            "required_capabilities": [],
            "expected_dataset_states": {
                "paid_order_success": "executable",
                "payment_attempt": "source_unbound",
            },
            "excluded_inputs": {
                "payment_attempt": "missing_contract",
            },
            "allowed_claim_ceiling": "directional",
            "terminal_boundary": "contract_allowed_partial",
        },
        "status": "completed",
        "runtime_authority": {
            **_run_matched_contract_authority(
                _analysis_contract_gap_authority(
                    [{
                        "dataset_id": "payment_attempt",
                        "gap_type": "source_unbound",
                        "gap_id": "dataset:payment_attempt:source_unbound",
                        "owner": "data_owner",
                    }],
                    [
                        "data_quality_profile",
                        "driver_decomposition",
                        "compare_periods",
                        "answer_verify",
                        "metric_timeseries",
                        "metric_coverage_profile",
                    ],
                )
            ),
            "query_executions": [{"dataset_id": "paid_order_success", "execution_status": "succeeded", "completeness_status": "complete"}],
        },
    }

    review = review_case_obligations(turn, registry)

    assert review["missing_required_capabilities"] == []
    assert review["expected_typed_gaps"] == {
        "payment_attempt": "missing_contract"
    }
    assert review["missing_current_data_obligations"] == []
    assert "final_answer_contains" not in review


def test_obligation_review_fails_missing_current_data_obligation():
    from tools.phase7.run_live_conversation_system_test import (
        review_case_obligations,
    )

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    review = review_case_obligations(
        {
            "accepted_graph": ["data_quality_profile", "answer_verify"],
            "scenario": {
                "question_family": "data_quality_or_evidence_review",
                "target_metrics": ["paid_amount"],
                "expected_dataset_states": {"paid_order_success": "executable"},
                "allowed_claim_ceiling": "directional",
                "terminal_boundary": "verified_answer",
            },
            "status": "completed",
            "runtime_authority": {},
        },
        registry,
    )

    assert review["missing_current_data_obligations"] == [
        "paid_order_success:executable"
    ]
    assert review["hard_acceptance_passed"] is False


@pytest.mark.parametrize(
    ("observed_state", "missing"),
    [
        ("executable", []),
        ("degraded", []),
        ("source_unbound", []),
        ("contract_partial", []),
        ("snapshot_unavailable_as_of", []),
        ("permission_blocked", ["paid_order_success:degraded"]),
        ("unobserved", ["paid_order_success:degraded"]),
    ],
)
def test_legacy_degraded_dataset_gate_uses_typed_state_relation(
    observed_state, missing
):
    from tools.phase7.run_live_conversation_system_test import (
        review_case_obligations,
    )

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    required_capabilities = [
        "metric_coverage_profile",
        "data_quality_profile",
        "answer_verify",
    ]
    gap_type = (
        "dataset_snapshot_unavailable_as_of"
        if observed_state == "snapshot_unavailable_as_of"
        else observed_state
    )
    gaps = (
        []
        if observed_state in {"executable", "degraded", "unobserved"}
        else [{
            "dataset_id": "paid_order_success",
            "gap_type": gap_type,
            "gap_id": f"dataset:paid_order_success:{gap_type}",
            "owner": "data_owner",
        }]
    )
    authority = _run_matched_contract_authority(
        _analysis_contract_gap_authority(gaps, required_capabilities),
        run_id=f"run-legacy-degraded-{observed_state}",
    )
    if observed_state in {"executable", "degraded"}:
        authority["query_executions"] = [{
            "dataset_id": "paid_order_success",
            "execution_status": (
                "succeeded" if observed_state == "executable" else "degraded"
            ),
            "completeness_status": (
                "complete" if observed_state == "executable" else "partial"
            ),
        }]

    review = review_case_obligations(
        {
            "status": "completed",
            "accepted_graph": required_capabilities,
            "scenario": {
                "question_family": "data_quality_or_evidence_review",
                "target_metrics": ["paid_amount"],
                "expected_dataset_states": {
                    "paid_order_success": "degraded"
                },
                "allowed_claim_ceiling": "trust_boundary",
                "terminal_boundary": "contract_allowed_partial",
            },
            "runtime_authority": authority,
        },
        registry,
    )

    assert review["observed_dataset_states"].get(
        "paid_order_success", "unobserved"
    ) == observed_state
    assert review["missing_current_data_obligations"] == missing


@pytest.mark.parametrize(
    ("boundary", "gaps", "claim_strength", "status", "passed"),
    [
        ("verified_answer", [], "observed", "completed", False),
        ("verified_answer", [], "strong", "completed", False),
        ("permission_blocked", [{"dataset_id": "market_dashboard_channel", "gap_type": "permission_blocked"}], "insufficient", "completed", True),
        ("permission_blocked", [], "insufficient", "completed", False),
        ("contract_allowed_partial", [{"dataset_id": "gameplay_channel", "gap_type": "contract_partial"}], "context_only", "completed", True),
        ("contract_allowed_partial", [], "context_only", "completed", False),
    ],
)
def test_obligation_review_enforces_claim_ceiling_and_terminal_boundary(
    boundary, gaps, claim_strength, status, passed
):
    from tools.phase7.run_live_conversation_system_test import review_case_obligations

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    dataset = "market_dashboard_channel" if boundary == "permission_blocked" else "gameplay_channel"
    expected_state = "permission_blocked" if boundary == "permission_blocked" else "contract_partial"
    if boundary == "verified_answer":
        dataset, expected_state = "paid_order_success", "executable"
    required_capabilities = [
        "data_quality_profile",
        "answer_verify",
        "metric_coverage_profile",
    ]
    authority_gaps = [
        {
            **gap,
            "gap_id": (
                f"dataset:{gap['dataset_id']}:{gap['gap_type']}"
                + (":required_fields" if gap["gap_type"] == "contract_partial" else "")
            ),
            "affected_capabilities": required_capabilities,
            "owner": "contract_owner",
        }
        for gap in gaps
    ]
    turn = {
        "status": status,
        "accepted_graph": required_capabilities,
        "scenario": {
            "question_family": "data_quality_or_evidence_review",
            "target_metrics": ["paid_amount"],
            "allowed_claim_ceiling": "directional",
            "terminal_boundary": boundary,
            "expected_dataset_states": {dataset: expected_state},
            "excluded_inputs": ({dataset: gaps[0]["gap_type"]} if gaps else {}),
        },
        "runtime_authority": {
            **_run_matched_contract_authority(
                _analysis_contract_gap_authority(
                    authority_gaps, required_capabilities
                ),
                run_id="run-terminal-boundary",
            ),
            "query_executions": ([{"dataset_id": dataset, "result_ref": "result:test", "execution_status": "succeeded", "completeness_status": "complete", "analysis_readiness": "ready"}] if boundary == "verified_answer" else []),
            "capability_bindings": [
                {
                    "binding_manifest_ref": (
                        "binding:test"
                        if capability_id == "data_quality_profile"
                        else f"binding:{capability_id}"
                    ),
                    "capability_id": capability_id,
                    "maximum_claim_strength": "directional",
                    "result_refs": ["result:test"],
                    "status": "ready",
                }
                for capability_id in required_capabilities
            ],
            "evidence_manifests": [{"evidence_ref": "evidence:test", "binding_manifest_ref": "binding:test", "result_refs": ["result:test"]}],
            "verified_claims": [{"claim_ref": "claim:test", "claim_strength": claim_strength, "evidence_refs": ["evidence:test"], "result_refs": ["result:test"]}],
        },
    }
    review = review_case_obligations(turn, registry)
    assert review["claim_ceiling_passed"] is (claim_strength != "strong")
    if claim_strength != "strong":
        expected_boundary = True if boundary == "verified_answer" else passed
        assert review["terminal_boundary_passed"] is expected_boundary
    assert review["hard_acceptance_passed"] is passed


def test_claim_ceiling_uses_only_claim_producing_binding_provenance():
    from tools.phase7.run_live_conversation_system_test import review_case_obligations

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)

    def review(strength="observed", evidence_ref="evidence:analysis"):
        return review_case_obligations(
            {
                "status": "completed",
                "answer_package": {"final_answer": "有证据的结论"},
                "accepted_graph": ["data_quality_profile", "metric_coverage_profile", "answer_verify"],
                "scenario": {
                    "question_family": "data_quality_or_evidence_review",
                    "target_metrics": ["paid_amount"],
                    "expected_dataset_states": {"paid_order_success": "executable"},
                    "allowed_claim_ceiling": "directional",
                    "terminal_boundary": "verified_answer",
                },
                "runtime_authority": {
                    "query_executions": [{"dataset_id": "paid_order_success", "execution_status": "succeeded", "completeness_status": "complete"}],
                    "capability_bindings": [
                        {"binding_manifest_ref": "binding:analysis", "capability_id": "data_quality_profile", "maximum_claim_strength": "directional", "result_refs": ["result:analysis"], "status": "ready"},
                        {"binding_manifest_ref": "binding:verify", "capability_id": "answer_verify", "maximum_claim_strength": "verifier_only", "result_refs": [], "status": "ready"},
                        {"binding_manifest_ref": "binding:reduce", "capability_id": "evidence_reduce", "maximum_claim_strength": "reducer_only", "result_refs": [], "status": "ready"},
                    ],
                    "evidence_manifests": [{"evidence_ref": "evidence:analysis", "binding_manifest_ref": "binding:analysis", "result_refs": ["result:analysis"]}],
                    "verified_claims": [{"claim_ref": "claim:1", "claim_strength": strength, "evidence_refs": [evidence_ref], "result_refs": (["result:analysis"] if evidence_ref == "evidence:analysis" else ["result:missing"])}],
                },
            },
            registry,
        )

    legal = review()
    assert legal["claim_ceiling_passed"] is True
    assert legal["actual_authority_ceiling"] == "directional"
    assert legal["missing_claim_capability_provenance"] == []
    assert review("strong")["claim_ceiling_passed"] is False
    missing = review(evidence_ref="evidence:missing")
    assert missing["missing_claim_capability_provenance"] == ["claim:1"]
    assert missing["hard_acceptance_passed"] is False


def _claim_binding_authority(
    registry,
):
    from tests.phase4.analysis_asset_fixtures import verified_dimension_scan_asset

    _, context = verified_dimension_scan_asset(
        rows=(
            {
                "window_id": "target_day",
                "window_role": "target",
                "observation_key": "2026-06-02",
                "paid_amount": 100.0,
                "amount": 100.0,
                "channel": "A",
            },
        ),
        required_fields=("window_id", "amount", "channel"),
        resolved_windows={
            "target_day": {
                "start_inclusive": "2026-06-02",
                "end_exclusive": "2026-06-03",
                "timezone": "Africa/Lagos",
            }
        },
    )
    resolver = context["evidence_resolver"]
    record = resolver.resolve_capability_binding(context["binding_manifest_ref"])
    assert record is not None
    assert record.status == "ready"
    assert record.validation_result_refs
    return SimpleNamespace(
        resolver=resolver,
        rows_loader=context["rows_loader"],
        release_resolver=context["release_resolver"],
        record=record,
        primary_result_ref=record.result_refs[0],
        validation_result_ref=record.validation_result_refs[0],
    )


class _ClaimAuthorityResolver:
    def __init__(
        self,
        delegate,
        *,
        binding=...,
        missing_query_record_ref="",
    ):
        self.delegate = delegate
        self.binding = binding
        self.missing_query_record_ref = missing_query_record_ref

    def resolve_query_execution(self, result_ref):
        return self.delegate.resolve_query_execution(result_ref)

    def resolve_query_execution_record(self, record_ref):
        if record_ref == self.missing_query_record_ref:
            return None
        return self.delegate.resolve_query_execution_record(record_ref)

    def resolve_rows(self, rows_ref):
        return self.delegate.resolve_rows(rows_ref)

    def resolve_rows_record(self, record_ref):
        return self.delegate.resolve_rows_record(record_ref)

    def resolve_snapshot(self, snapshot_ref):
        return self.delegate.resolve_snapshot(snapshot_ref)

    def resolve_completeness(self, record_ref):
        return self.delegate.resolve_completeness(record_ref)

    def resolve_capability_binding(self, binding_ref):
        if self.binding is ...:
            return self.delegate.resolve_capability_binding(binding_ref)
        return self.binding


class _AdditionalBindingResolver(_ClaimAuthorityResolver):
    def __init__(self, delegate, binding):
        super().__init__(delegate)
        self.additional_binding = binding

    def resolve_capability_binding(self, binding_ref):
        if binding_ref == self.additional_binding.record_ref:
            return self.additional_binding
        return super().resolve_capability_binding(binding_ref)


class _CurrentReuseRecordResolver(_AdditionalBindingResolver):
    def __init__(self, delegate, binding, query_record, completeness_record=None):
        super().__init__(delegate, binding)
        self.query_record = query_record
        self.completeness_record = completeness_record

    def resolve_query_execution(self, result_ref):
        if result_ref == self.query_record.result_ref:
            return self.query_record
        return super().resolve_query_execution(result_ref)

    def resolve_query_execution_record(self, record_ref):
        if record_ref == self.query_record.record_ref:
            return self.query_record
        return super().resolve_query_execution_record(record_ref)

    def resolve_completeness(self, record_ref):
        if (
            self.completeness_record is not None
            and record_ref == self.completeness_record.record_ref
        ):
            return self.completeness_record
        return super().resolve_completeness(record_ref)


def _resigned_query_execution(record, *, contract=None, provider_stats=None):
    from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value

    changed_contract = contract or record.contract
    query_contract_payload = canonical_value({
        **record.query_contract,
        "analysis_contract_ref": changed_contract.analysis_contract_ref,
    })
    result_payload = dict(record.result_payload)
    if provider_stats is not None:
        result_payload["provider_stats"] = dict(provider_stats)
    record_payload = dict(record.record_payload)
    record_payload["query_contract"] = query_contract_payload
    record_payload["result"] = canonical_value(result_payload)
    record_payload = canonical_value(record_payload)
    digest = canonical_digest(record_payload)
    return replace(
        record,
        record_ref=f"query-execution:{record.result_ref}:{digest}",
        record_digest=digest,
        record_payload=record_payload,
        query_contract=query_contract_payload,
        contract=changed_contract,
        result_payload=canonical_value(result_payload),
    )


def _binding_with_query_execution(binding, previous, changed):
    from tests.phase4.test_authoritative_query_chain import _resign_binding

    prefix = "" if previous.result_ref in binding.result_refs else "validation_"
    refs_field = f"{prefix}query_execution_record_refs"
    digests_field = f"{prefix}query_execution_record_digests"
    refs = tuple(
        changed.record_ref if ref == previous.record_ref else ref
        for ref in getattr(binding, refs_field)
    )
    digests = tuple(
        changed.record_digest if ref == previous.record_ref else digest
        for ref, digest in zip(
            getattr(binding, refs_field),
            getattr(binding, digests_field),
        )
    )
    binding_payload = dict(binding.binding_payload)
    binding_payload[refs_field] = refs
    binding_payload[digests_field] = digests
    return _resign_binding(
        binding,
        **{
            refs_field: refs,
            digests_field: digests,
            "binding_payload": binding_payload,
        },
    )


def _resigned_completeness_provider_stats(record, provider_stats):
    from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value

    payload = dict(record.report_payload)
    assertions = []
    for assertion in payload.get("assertion_results") or ():
        item = dict(assertion)
        if item.get("assertion") == "provider_not_truncated":
            item["details"] = dict(provider_stats)
        assertions.append(item)
    payload["assertion_results"] = assertions
    payload = canonical_value(payload)
    digest = canonical_digest(payload)
    return replace(
        record,
        record_ref=f"completeness-record:{record.report_ref}:{digest}",
        report_digest=digest,
        report_payload=payload,
    )


def _resigned_claim_binding(record, **changes):
    from tests.phase4.test_authoritative_query_chain import _resign_binding

    binding_payload = dict(record.binding_payload)
    for key in (
        "status",
        "input_completeness_statuses",
        "supported_claim_types",
        "supported_evidence_types",
        "maximum_claim_strength",
        "maximum_claim_strength_rank",
    ):
        if key in changes:
            binding_payload[key] = changes[key]
    return _resign_binding(record, binding_payload=binding_payload, **changes)


def _resolver_claim_authority(
    record,
    *,
    claim=None,
    evidence=None,
    claim_result_refs=None,
    evidence_result_refs=None,
    claim_type=None,
    evidence_type=None,
):
    producing_refs = list(
        record.result_refs if claim_result_refs is None else claim_result_refs
    )
    manifest_refs = list(
        record.result_refs if evidence_result_refs is None else evidence_result_refs
    )
    claim_payload = claim or {
        "claim_ref": "claim:resolver-authority",
        "claim_strength": "observed",
        "claim_type": claim_type or record.supported_claim_types[0],
        "provenance_record_ref": "claim-provenance:resolver-authority",
        "evidence_refs": ["evidence:resolver-authority"],
        "result_refs": producing_refs,
    }
    evidence_payload = evidence or {
        "evidence_ref": "evidence:resolver-authority",
        "evidence_type": evidence_type or record.supported_evidence_types[0],
        "binding_manifest_ref": record.record_ref,
        "binding_manifest_digest": record.binding_digest,
        "result_refs": manifest_refs,
    }
    return {
        "sections": [{"payload": {"evidence": [evidence_payload]}}],
        "admin_audit": {
            "verified_claims": [claim_payload],
            "trusted_claim_provenance_records": [{
                "record_ref": "claim-provenance:resolver-authority",
                "evidence_refs": ["evidence:resolver-authority"],
                "result_refs": producing_refs,
            }],
        },
        "available_evidence_brief": {"verified_claims": [claim_payload]},
    }


def test_claim_ceiling_resolves_real_binding_authority_and_deduplicates_claims():
    from tools.phase7.run_live_conversation_system_test import (
        review_case_obligations,
    )

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    fixture = _claim_binding_authority(registry)
    record = fixture.record
    authority = _resolver_claim_authority(record)
    claim = authority["admin_audit"]["verified_claims"][0]
    claim.pop("evidence_refs")
    claim.pop("result_refs")
    authority["sections"][0]["payload"]["evidence"][0][
        "capability_id"
    ] = "forged_evidence_capability"
    authority["admin_audit"]["capability_execution_plans"] = [{
        "capability_id": "forged_plan_capability",
        "maximum_claim_strength": "candidate_mechanism",
    }]
    authority["sections"].append({
        "payload": {
            "verified_claims": list(
                authority["admin_audit"]["verified_claims"]
            )
        }
    })

    review = review_case_obligations(
        {
            "status": "completed",
            "accepted_graph": [],
            "scenario": {
                "question_family": "custom_baseline_comparison",
                "allowed_claim_ceiling": "directional",
                "terminal_boundary": "verified_answer",
            },
            "runtime_authority": authority,
        },
        registry,
        evidence_resolver=fixture.resolver,
        rows_loader=fixture.rows_loader,
        release_resolver=fixture.release_resolver,
    )

    assert review["claim_ceiling_passed"] is True
    assert review["actual_authority_ceiling"] == "quantified_contribution"
    assert review["missing_claim_capability_provenance"] == []
    assert review["claim_authority_reviews"] == [{
        "claim_ref": "claim:resolver-authority",
        "claim_strength": "observed",
        "producing_capabilities": ["segment_contribution"],
        "authority_ceiling": "quantified_contribution",
        "passed": True,
        "error_code": "",
        "authority_errors": [],
    }]


def test_claim_ceiling_allows_claim_relevant_primary_refs_with_validation_binding():
    from tools.phase7.run_live_conversation_system_test import _review_claim_ceiling

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    fixture = _claim_binding_authority(registry)
    record = fixture.record

    review = _review_claim_ceiling(
        _resolver_claim_authority(record),
        {"allowed_claim_ceiling": "quantified_contribution"},
        registry,
        evidence_resolver=fixture.resolver,
        rows_loader=fixture.rows_loader,
        release_resolver=fixture.release_resolver,
    )

    assert review["passed"] is True
    assert review["claim_authority_reviews"][0]["producing_capabilities"] == [
        "segment_contribution"
    ]


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        ("non_ready", "capability_binding_not_ready"),
        (
            "input_incomplete",
            "capability_binding_input_completeness_not_complete",
        ),
    ],
)
def test_claim_ceiling_fails_closed_on_non_ready_or_incomplete_binding(
    mutation, expected_error
):
    from tools.phase7.run_live_conversation_system_test import _review_claim_ceiling

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    fixture = _claim_binding_authority(registry)
    if mutation == "non_ready":
        record = _resigned_claim_binding(fixture.record, status="degraded")
    else:
        record = _resigned_claim_binding(
            fixture.record,
            input_completeness_statuses=("partial", "complete"),
        )
    resolver = _ClaimAuthorityResolver(fixture.resolver, binding=record)

    review = _review_claim_ceiling(
        _resolver_claim_authority(record),
        {"allowed_claim_ceiling": "quantified_contribution"},
        registry,
        evidence_resolver=resolver,
        rows_loader=fixture.rows_loader,
        release_resolver=fixture.release_resolver,
    )

    assert review["passed"] is False
    assert expected_error in review["claim_authority_reviews"][0][
        "authority_errors"
    ]


def test_claim_ceiling_fails_closed_on_dangling_authoritative_query_chain():
    from tools.phase7.run_live_conversation_system_test import _review_claim_ceiling

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    fixture = _claim_binding_authority(registry)
    resolver = _ClaimAuthorityResolver(
        fixture.resolver,
        missing_query_record_ref=fixture.record.query_execution_record_refs[0],
    )

    review = _review_claim_ceiling(
        _resolver_claim_authority(fixture.record),
        {"allowed_claim_ceiling": "quantified_contribution"},
        registry,
        evidence_resolver=resolver,
        rows_loader=fixture.rows_loader,
        release_resolver=fixture.release_resolver,
    )

    assert review["passed"] is False
    assert "authoritative_query_chain_invalid:query_execution_record_missing" in (
        review["claim_authority_reviews"][0]["authority_errors"]
    )


def test_claim_ceiling_rejects_validation_only_claim_producing_result_refs():
    from tools.phase7.run_live_conversation_system_test import _review_claim_ceiling

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    fixture = _claim_binding_authority(registry)

    review = _review_claim_ceiling(
        _resolver_claim_authority(
            fixture.record,
            claim_result_refs=(fixture.validation_result_ref,),
            evidence_result_refs=(fixture.validation_result_ref,),
        ),
        {"allowed_claim_ceiling": "quantified_contribution"},
        registry,
        evidence_resolver=fixture.resolver,
        rows_loader=fixture.rows_loader,
        release_resolver=fixture.release_resolver,
    )

    assert review["passed"] is False
    assert "claim_result_refs_not_primary" in review[
        "claim_authority_reviews"
    ][0]["authority_errors"]


def test_claim_ceiling_requires_nonempty_claim_producing_result_refs():
    from tools.phase7.run_live_conversation_system_test import _review_claim_ceiling

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    fixture = _claim_binding_authority(registry)

    review = _review_claim_ceiling(
        _resolver_claim_authority(fixture.record, claim_result_refs=()),
        {"allowed_claim_ceiling": "quantified_contribution"},
        registry,
        evidence_resolver=fixture.resolver,
        rows_loader=fixture.rows_loader,
        release_resolver=fixture.release_resolver,
    )

    assert review["passed"] is False
    assert "claim_result_refs_missing" in review["claim_authority_reviews"][0][
        "authority_errors"
    ]


@pytest.mark.parametrize(
    ("field", "value", "expected_error"),
    [
        ("claim_type", "causal_effect", "claim_type_not_supported"),
        ("evidence_type", "external_event", "evidence_type_not_supported"),
    ],
)
def test_claim_ceiling_enforces_binding_and_registry_claim_evidence_types(
    field, value, expected_error
):
    from tools.phase7.run_live_conversation_system_test import _review_claim_ceiling

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    fixture = _claim_binding_authority(registry)

    review = _review_claim_ceiling(
        _resolver_claim_authority(fixture.record, **{field: value}),
        {"allowed_claim_ceiling": "quantified_contribution"},
        registry,
        evidence_resolver=fixture.resolver,
        rows_loader=fixture.rows_loader,
        release_resolver=fixture.release_resolver,
    )

    assert review["passed"] is False
    assert expected_error in review["claim_authority_reviews"][0][
        "authority_errors"
    ]


def test_claim_ceiling_fails_closed_with_typed_error_for_malformed_record():
    from tools.phase7.run_live_conversation_system_test import _review_claim_ceiling

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    fixture = _claim_binding_authority(registry)
    resolver = _ClaimAuthorityResolver(
        fixture.resolver,
        binding={"record_ref": fixture.record.record_ref},
    )

    review = _review_claim_ceiling(
        _resolver_claim_authority(fixture.record),
        {"allowed_claim_ceiling": "quantified_contribution"},
        registry,
        evidence_resolver=resolver,
        rows_loader=fixture.rows_loader,
        release_resolver=fixture.release_resolver,
    )

    assert review["passed"] is False
    assert review["claim_authority_reviews"][0]["authority_errors"] == [
        "capability_binding_record_type_invalid"
    ]


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        ("unresolved", "capability_binding_record_missing"),
        ("binding_ref", "capability_binding_record_ref_mismatch"),
        ("binding_digest", "binding_manifest_digest_mismatch"),
        ("result_refs", "capability_binding_result_refs_mismatch"),
        ("record_integrity", "capability_binding_record_integrity"),
        ("contract_signature", "capability_contract_signature_mismatch"),
        ("contract_version", "capability_contract_version_mismatch"),
        ("claim_ceiling", "capability_claim_ceiling_mismatch"),
    ],
)
def test_claim_ceiling_fails_closed_on_resolved_binding_authority_drift(
    mutation, expected_error
):
    from tools.phase7.run_live_conversation_system_test import _review_claim_ceiling
    from tests.phase4.test_authoritative_query_chain import _resign_binding

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    fixture = _claim_binding_authority(registry)
    record = fixture.record
    resolved_record = record
    if mutation == "contract_signature":
        plan_payload = dict(record.plan_payload)
        plan_payload["capability_contract_signature"] = "0" * 64
        resolved_record = _resign_binding(
            record,
            capability_contract_signature="0" * 64,
            plan_payload=plan_payload,
        )
    elif mutation == "contract_version":
        plan_payload = dict(record.plan_payload)
        plan_payload["capability_contract_version"] = "drifted-runtime-contract"
        resolved_record = _resign_binding(
            record,
            capability_contract_version="drifted-runtime-contract",
            plan_payload=plan_payload,
        )
    elif mutation == "claim_ceiling":
        plan_payload = dict(record.plan_payload)
        binding_payload = dict(record.binding_payload)
        plan_payload["maximum_claim_strength"] = "candidate_driver"
        binding_payload["maximum_claim_strength"] = "candidate_driver"
        resolved_record = _resign_binding(
            record,
            maximum_claim_strength="candidate_driver",
            plan_payload=plan_payload,
            binding_payload=binding_payload,
        )
    evidence = None
    if mutation == "unresolved":
        resolved_record = None
    elif mutation == "binding_ref":
        evidence = {
            "evidence_ref": "evidence:resolver-authority",
            "evidence_type": record.supported_evidence_types[0],
            "binding_manifest_ref": "capability-binding:forged-ref",
            "binding_manifest_digest": record.binding_digest,
            "result_refs": list(record.result_refs),
        }
    elif mutation == "binding_digest":
        evidence = {
            "evidence_ref": "evidence:resolver-authority",
            "evidence_type": record.supported_evidence_types[0],
            "binding_manifest_ref": record.record_ref,
            "binding_manifest_digest": "f" * 64,
            "result_refs": list(record.result_refs),
        }
    elif mutation == "result_refs":
        evidence = {
            "evidence_ref": "evidence:resolver-authority",
            "evidence_type": record.supported_evidence_types[0],
            "binding_manifest_ref": record.record_ref,
            "binding_manifest_digest": record.binding_digest,
            "result_refs": ["result:forged"],
        }
    elif mutation == "record_integrity":
        resolved_record = replace(record, binding_digest="f" * 64)

    resolver = _ClaimAuthorityResolver(
        fixture.resolver,
        binding=resolved_record,
    )

    review = _review_claim_ceiling(
        _resolver_claim_authority(resolved_record or record, evidence=evidence),
        {"allowed_claim_ceiling": "quantified_contribution"},
        registry,
        evidence_resolver=resolver,
        rows_loader=fixture.rows_loader,
        release_resolver=fixture.release_resolver,
    )

    assert review["passed"] is False
    assert review["claim_authority_reviews"][0]["error_code"] == (
        "claim_capability_authority_invalid"
    )
    assert expected_error in review["claim_authority_reviews"][0][
        "authority_errors"
    ]


def test_claim_ceiling_fails_closed_on_conflicting_same_ref_claim_payloads():
    from tools.phase7.run_live_conversation_system_test import _review_claim_ceiling

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    fixture = _claim_binding_authority(registry)
    record = fixture.record
    authority = _resolver_claim_authority(record)
    conflicting = {
        **authority["admin_audit"]["verified_claims"][0],
        "claim_strength": "strong",
    }
    authority["sections"].append({"payload": {"verified_claims": [conflicting]}})

    review = _review_claim_ceiling(
        authority,
        {"allowed_claim_ceiling": "quantified_contribution"},
        registry,
        evidence_resolver=fixture.resolver,
        rows_loader=fixture.rows_loader,
        release_resolver=fixture.release_resolver,
    )

    assert review["passed"] is False
    assert len(review["claim_authority_reviews"]) == 1
    conflicting_review = review["claim_authority_reviews"][0]
    assert conflicting_review["claim_ref"] == "claim:resolver-authority"
    assert conflicting_review["producing_capabilities"] == []
    assert conflicting_review["authority_ceiling"] == ""
    assert conflicting_review["passed"] is False
    assert conflicting_review["error_code"] == "conflicting_claim_ref_payload"
    assert conflicting_review["authority_errors"] == [
        "conflicting_claim_ref_payload"
    ]


def test_run_case_passes_core_authority_chain_resolvers_to_obligation_review(
    tmp_path, monkeypatch
):
    from tools.phase7 import run_live_conversation_system_test as system_test

    resolver = object()
    rows_loader = object()
    release_resolver = object()
    captured = {}

    class Core:
        def run_message(self, **kwargs):
            return {
                "status": "completed",
                "run_id": "run:resolver-forwarding",
                "topic_id": "topic:resolver-forwarding",
                "answer_package": {},
                "context_manifest": {},
                "accepted_graph": [],
                "llm_calls": [],
            }

    monkeypatch.setattr(system_test, "_runtime_quality_review", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        system_test,
        "_review_expectations",
        lambda *args, **kwargs: {"passed": True},
    )
    monkeypatch.setattr(system_test, "_runtime_audit_package", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        system_test,
        "_real_clickhouse_review",
        lambda *args, **kwargs: {
            "runtime_correctness": {
                "all_required_queries_complete": True,
                "all_capabilities_bound": True,
                "all_claims_traceable": True,
            },
            "issues": [],
        },
    )
    monkeypatch.setattr(system_test, "_write_case_artifact", lambda *args, **kwargs: None)

    def review(*args, **kwargs):
        captured["evidence_resolver"] = kwargs.get("evidence_resolver")
        captured["rows_loader"] = kwargs.get("rows_loader")
        captured["release_resolver"] = kwargs.get("release_resolver")
        captured["conversation_store"] = kwargs.get("conversation_store")
        captured["case_lineage"] = kwargs.get("case_lineage")
        return {
            "hard_acceptance_passed": True,
            "reuse_passed": True,
            "clarification_resume_passed": True,
        }

    monkeypatch.setattr(system_test, "review_case_obligations", review)

    core = Core()
    core.evidence_resolver = resolver
    core.rows_loader = rows_loader
    core.release_resolver = release_resolver
    core.store = object()
    system_test.run_case(
        core,
        {
            "id": "resolver-forwarding",
            "turns": [{
                "user": "检查证据链",
                "scenario": {"question_family": "custom_baseline_comparison"},
            }],
        },
        tmp_path,
    )

    assert captured["evidence_resolver"] is resolver
    assert captured["rows_loader"] is rows_loader
    assert captured["release_resolver"] is release_resolver
    assert captured["conversation_store"] is core.store
    assert captured["case_lineage"]["thread_id"].startswith(
        "live-resolver-forwarding-"
    )
    assert captured["case_lineage"]["current_run_id"] == "run:resolver-forwarding"
    assert captured["case_lineage"]["current_topic_id"] == "topic:resolver-forwarding"
    assert captured["case_lineage"]["prior_runs"] == []


def test_effective_result_preserves_direct_run_failure_reason():
    from tools.phase7.run_live_conversation_system_test import _effective_result

    assert _effective_result({
        "status": "failed",
        "run_id": "run:direct-failure",
        "failure_reason": "llm_binding_failed:provider_contract_invalid",
    })["failure_reason"] == "llm_binding_failed:provider_contract_invalid"


def test_run_case_direct_failure_short_circuits_downstream_reviews(
    tmp_path, monkeypatch
):
    from tools.phase7 import run_live_conversation_system_test as system_test

    calls = {
        key: 0
        for key in (
            "coverage_authority",
            "evidence_resolver",
            "expectation",
            "runtime_authority",
            "clickhouse",
            "obligation",
            "strict_quality",
        )
    }
    audits = [
        {
            "task": "business_intent",
            "attempt": attempt,
            "response_id": f"response-{attempt}",
            "validation_code": "provider_contract_invalid",
        }
        for attempt in range(1, 4)
    ]

    class Store:
        def list_dataset_snapshots(self):
            return []

        def runtime_evidence_resolver(self):
            calls["evidence_resolver"] += 1
            return object()

    class Core:
        store = Store()
        evidence_resolver = None
        rows_loader = object()
        release_resolver = object()

        def run_message(self, **kwargs):
            return {
                "status": "failed",
                "run_id": "run:direct-failure",
                "topic_id": "topic:direct-failure",
                "failure_reason": "llm_binding_failed:provider_contract_invalid",
                "answer_package": None,
                "context_manifest": None,
                "accepted_graph": [],
                "artifact_path": "",
                "llm_calls": audits,
            }

    def called(name, value):
        def review(*args, **kwargs):
            calls[name] += 1
            return value

        return review

    monkeypatch.setattr(
        system_test,
        "audit_existing_data_coverage",
        called("coverage_authority", {}),
    )
    monkeypatch.setattr(system_test, "_runtime_quality_review", lambda *a, **k: {})
    monkeypatch.setattr(
        system_test,
        "_review_expectations",
        called("expectation", {"passed": False}),
    )
    monkeypatch.setattr(
        system_test,
        "_runtime_audit_package",
        called("runtime_authority", {"_authority_error": "artifact_path_missing"}),
    )
    monkeypatch.setattr(
        system_test,
        "_real_clickhouse_review",
        called("clickhouse", {
            "required": True,
            "real_clickhouse_verified": False,
            "clickhouse_result_refs": [],
            "observed_datasets": [],
            "runtime_correctness": {
                "all_required_queries_complete": False,
                "all_capabilities_bound": False,
                "all_claims_traceable": False,
            },
            "issues": ["missing_required_dataset:paid_order_success"],
        }),
    )
    monkeypatch.setattr(
        system_test,
        "review_case_obligations",
        called("obligation", {"hard_acceptance_passed": False}),
    )
    monkeypatch.setattr(
        system_test,
        "_strict_quality_failed",
        called("strict_quality", True),
    )
    monkeypatch.setattr(system_test, "_write_case_artifact", lambda *a, **k: None)

    output = system_test.run_case(
        Core(),
        {
            "id": "direct-run-failure",
            "required_datasets": ["paid_order_success"],
            "analysis_context": {"as_of": "2026-06-03T12:00:00+01:00"},
            "turns": [{
                "user": "检查付费金额",
                "expect": {},
                "scenario": {
                    "question_family": "data_quality_or_evidence_review",
                    "expected_dataset_states": {
                        "paid_order_success": "executable"
                    },
                },
            }],
        },
        tmp_path,
        strict_quality=True,
        real_clickhouse=True,
    )

    marker = {
        "status": "not_evaluated_due_to_run_failure",
        "evaluated": False,
        "primary_failure": {
            "stage": "run",
            "run_id": "run:direct-failure",
            "reason": "llm_binding_failed:provider_contract_invalid",
        },
    }
    turn = output["turns"][0]
    assert calls == {key: 0 for key in calls}
    assert turn["evaluation"] == marker
    assert turn["expectation_review"] == {**marker, "passed": None}
    assert turn["obligation_review"] == {
        **marker,
        "hard_acceptance_passed": None,
    }
    assert turn["real_clickhouse_review"]["real_clickhouse_verified"] is None
    assert turn["real_clickhouse_review"]["runtime_correctness"] == {
        "all_required_queries_complete": None,
        "all_capabilities_bound": None,
        "all_claims_traceable": None,
    }
    assert turn["real_clickhouse_review"]["issues"] == []
    assert turn["strict_quality_failed"] is None
    assert output["status"] == "failed"
    assert output["primary_failure"] == marker["primary_failure"]
    assert output["llm_calls"] == audits
    assert output["real_clickhouse_verified"] is None
    assert output["real_clickhouse_review"]["issues"] == []
    assert output["coverage_summary"]["final_answer_audit_coverage"] == {
        "reviewed": 0,
        "total": 0,
    }
    assert output["coverage_summary"]["runtime_correctness"] == {
        "all_required_queries_complete": None,
        "all_capabilities_bound": None,
        "all_claims_traceable": None,
    }


def test_run_case_resume_failure_uses_resume_as_primary_failure(
    tmp_path, monkeypatch
):
    from tools.phase7 import run_live_conversation_system_test as system_test

    results = iter((
        {
            "status": "waiting_for_clarification",
            "run_id": "run:clarification",
            "topic_id": "topic:clarification",
            "failure_reason": "",
            "answer_package": None,
            "context_manifest": None,
            "accepted_graph": [],
            "artifact_path": "",
            "llm_calls": [],
            "clarification": {},
        },
        {
            "status": "failed",
            "run_id": "run:clarification-resume",
            "topic_id": "topic:clarification",
            "failure_reason": "clarification_resume_authority_failed",
            "answer_package": None,
            "context_manifest": None,
            "accepted_graph": [],
            "artifact_path": "",
            "llm_calls": [{"task": "clarification", "attempt": 1}],
        },
    ))

    class Core:
        store = object()
        evidence_resolver = None
        rows_loader = None
        release_resolver = None

        def run_message(self, **kwargs):
            return next(results)

    forbidden = lambda *a, **k: pytest.fail("downstream review executed")
    monkeypatch.setattr(system_test, "_runtime_quality_review", lambda *a, **k: {})
    monkeypatch.setattr(system_test, "_review_expectations", forbidden)
    monkeypatch.setattr(system_test, "_runtime_audit_package", forbidden)
    monkeypatch.setattr(system_test, "_real_clickhouse_review", forbidden)
    monkeypatch.setattr(system_test, "review_case_obligations", forbidden)
    monkeypatch.setattr(system_test, "_strict_quality_failed", forbidden)
    monkeypatch.setattr(system_test, "_write_case_artifact", lambda *a, **k: None)

    output = system_test.run_case(
        Core(),
        {
            "id": "resume-run-failure",
            "turns": [{
                "user": "继续分析",
                "clarification_response": "按推荐继续",
                "expect": {},
                "scenario": {"question_family": "pattern_explanation"},
            }],
        },
        tmp_path,
        strict_quality=True,
    )

    turn = output["turns"][0]
    assert turn["evaluation"]["primary_failure"] == {
        "stage": "clarification_resume",
        "run_id": "run:clarification-resume",
        "reason": "clarification_resume_authority_failed",
    }
    assert turn["resumed_failure_reason"] == "clarification_resume_authority_failed"
    assert output["failure_reason"] == "clarification_resume_authority_failed"
    assert output["llm_calls"] == [{"task": "clarification", "attempt": 1}]


def test_failed_turn_is_excluded_from_mixed_turn_coverage_denominators(
    tmp_path, monkeypatch
):
    from tools.phase7 import run_live_conversation_system_test as system_test

    results = iter((
        {
            "status": "failed",
            "run_id": "run:mixed-failed",
            "topic_id": "topic:mixed",
            "failure_reason": "provider_attempts_exhausted",
            "answer_package": None,
            "context_manifest": None,
            "accepted_graph": [],
            "artifact_path": "",
            "llm_calls": [],
        },
        {
            "status": "completed",
            "run_id": "run:mixed-completed",
            "topic_id": "topic:mixed",
            "failure_reason": "",
            "answer_package": {},
            "context_manifest": {},
            "accepted_graph": ["compare_periods"],
            "artifact_path": "artifact:completed",
            "llm_calls": [],
        },
    ))

    class Core:
        store = object()
        evidence_resolver = None
        rows_loader = None
        release_resolver = None

        def run_message(self, **kwargs):
            return next(results)

    monkeypatch.setattr(system_test, "_runtime_quality_review", lambda *a, **k: {})
    monkeypatch.setattr(system_test, "_review_expectations", lambda *a, **k: {"passed": True})
    monkeypatch.setattr(system_test, "_runtime_audit_package", lambda *a, **k: {})
    monkeypatch.setattr(
        system_test,
        "_real_clickhouse_review",
        lambda *a, **k: {
            "required": False,
            "real_clickhouse_verified": True,
            "clickhouse_result_refs": [],
            "observed_datasets": [],
            "runtime_correctness": {
                "all_required_queries_complete": True,
                "all_capabilities_bound": True,
                "all_claims_traceable": True,
            },
            "issues": [],
        },
    )
    monkeypatch.setattr(
        system_test,
        "review_case_obligations",
        lambda *a, **k: {
            "required_capabilities": ["compare_periods"],
            "capability_outcomes": {"compare_periods": "executed"},
            "hard_acceptance_passed": True,
        },
    )
    monkeypatch.setattr(system_test, "_strict_quality_failed", lambda *a, **k: False)
    monkeypatch.setattr(system_test, "_has_completed_final_answer_audit", lambda *a, **k: True)
    monkeypatch.setattr(system_test, "_write_case_artifact", lambda *a, **k: None)

    output = system_test.run_case(
        Core(),
        {
            "id": "mixed-run-failure",
            "turns": [
                {
                    "user": "第一次分析",
                    "expect": {},
                    "scenario": {"question_family": "period_comparison"},
                },
                {
                    "user": "第二次分析",
                    "expect": {},
                    "scenario": {"question_family": "period_comparison"},
                },
            ],
        },
        tmp_path,
    )

    assert output["status"] == "failed"
    assert output["primary_failure"] == {
        "stage": "run",
        "run_id": "run:mixed-failed",
        "reason": "provider_attempts_exhausted",
    }
    assert output["final_turn_status"] == "completed"
    assert output["coverage_summary"]["obligation_coverage"]["required"] == 1
    assert output["coverage_summary"]["final_answer_audit_coverage"] == {
        "reviewed": 1,
        "total": 1,
    }


def test_failed_run_without_reason_reports_primary_failure_contract_issue(
    tmp_path, monkeypatch
):
    from tools.phase7 import run_live_conversation_system_test as system_test

    class Core:
        store = object()
        evidence_resolver = None

        def run_message(self, **kwargs):
            return {
                "status": "failed",
                "run_id": "run:missing-reason",
                "topic_id": "topic:missing-reason",
                "failure_reason": "",
                "answer_package": None,
                "context_manifest": None,
                "accepted_graph": [],
                "artifact_path": "",
                "llm_calls": [],
            }

    monkeypatch.setattr(system_test, "_runtime_quality_review", lambda *a, **k: {})
    monkeypatch.setattr(system_test, "_write_case_artifact", lambda *a, **k: None)
    output = system_test.run_case(
        Core(),
        {"id": "missing-primary-reason", "turns": [{"user": "分析", "expect": {}}]},
        tmp_path,
    )

    assert output["status"] == "failed"
    assert output["primary_failure"] == {
        "stage": "run",
        "run_id": "run:missing-reason",
        "reason": "primary_failure_reason_missing",
    }


def test_completed_turn_without_scenario_still_requires_runtime_authority(
    tmp_path, monkeypatch
):
    from tools.phase7 import run_live_conversation_system_test as system_test

    class Core:
        store = object()
        evidence_resolver = None

        def run_message(self, **kwargs):
            return {
                "status": "completed",
                "run_id": "run:empty-scenario-missing-artifact",
                "topic_id": "topic:empty-scenario-missing-artifact",
                "failure_reason": "",
                "answer_package": {},
                "context_manifest": {},
                "accepted_graph": [],
                "artifact_path": "",
                "llm_calls": [],
            }

    monkeypatch.setattr(system_test, "_runtime_quality_review", lambda *a, **k: {})
    monkeypatch.setattr(
        system_test,
        "_review_expectations",
        lambda *a, **k: {"passed": True},
    )
    monkeypatch.setattr(system_test, "_write_case_artifact", lambda *a, **k: None)

    output = system_test.run_case(
        Core(),
        {
            "id": "empty-scenario-missing-artifact",
            "turns": [{"user": "继续", "expect": {}, "scenario": {}}],
        },
        tmp_path,
    )

    assert output["turns"][0]["runtime_authority"] == {
        "_authority_error": "artifact_path_missing"
    }
    assert output["runtime_authority_failed"] is True
    assert output["status"] == "failed"


@pytest.mark.parametrize(
    "expectation_review",
    (
        {"passed": None},
        {},
        {"passed": "true"},
    ),
    ids=("none", "missing", "non_boolean"),
)
def test_completed_turn_expectation_verdict_fails_closed(expectation_review):
    from tools.phase7.run_live_conversation_system_test import _case_output

    output = _case_output(
        case={"id": "completed-expectation-verdict"},
        thread_id="thread:completed-expectation-verdict",
        run_mode="dry_run",
        strict_quality=False,
        real_clickhouse=False,
        turns=[{
            "status": "completed",
            "run_id": "run:completed-expectation-verdict",
            "topic_id": "topic:completed-expectation-verdict",
            "expectation_review": expectation_review,
            "runtime_authority": {},
            "obligation_review": {"hard_acceptance_passed": True},
            "real_clickhouse_review": {
                "required": False,
                "real_clickhouse_verified": True,
                "clickhouse_result_refs": [],
                "observed_datasets": [],
                "runtime_correctness": {
                    "all_required_queries_complete": True,
                    "all_capabilities_bound": True,
                    "all_claims_traceable": True,
                },
                "issues": [],
            },
            "strict_quality_failed": False,
        }],
    )

    assert output["status"] == "failed"


@pytest.mark.parametrize(
    "expectation_review",
    (
        {"passed": None},
        {},
        {"passed": "true"},
    ),
    ids=("none", "missing", "non_boolean"),
)
def test_waiting_turn_expectation_verdict_fails_closed(expectation_review):
    from tools.phase7.run_live_conversation_system_test import _case_output

    output = _case_output(
        case={"id": "waiting-expectation-verdict"},
        thread_id="thread:waiting-expectation-verdict",
        run_mode="dry_run",
        strict_quality=False,
        real_clickhouse=False,
        turns=[{
            "status": "waiting_for_clarification",
            "run_id": "run:waiting-expectation-verdict",
            "topic_id": "topic:waiting-expectation-verdict",
            "expectation_review": expectation_review,
            "obligation_review": {"hard_acceptance_passed": True},
            "real_clickhouse_review": {
                "required": False,
                "real_clickhouse_verified": True,
                "clickhouse_result_refs": [],
                "observed_datasets": [],
                "runtime_correctness": {
                    "all_required_queries_complete": True,
                    "all_capabilities_bound": True,
                    "all_claims_traceable": True,
                },
                "issues": [],
            },
            "strict_quality_failed": False,
        }],
    )

    assert output["status"] == "failed"


def test_waiting_turn_with_valid_expectation_verdict_keeps_clarification_behavior():
    from tools.phase7.run_live_conversation_system_test import _case_output

    output = _case_output(
        case={"id": "waiting-valid-expectation-verdict"},
        thread_id="thread:waiting-valid-expectation-verdict",
        run_mode="dry_run",
        strict_quality=False,
        real_clickhouse=False,
        turns=[{
            "status": "waiting_for_clarification",
            "run_id": "run:waiting-valid-expectation-verdict",
            "topic_id": "topic:waiting-valid-expectation-verdict",
            "expectation_review": {"passed": True},
            "obligation_review": {"hard_acceptance_passed": True},
            "real_clickhouse_review": {
                "required": False,
                "real_clickhouse_verified": True,
                "clickhouse_result_refs": [],
                "observed_datasets": [],
                "runtime_correctness": {
                    "all_required_queries_complete": True,
                    "all_capabilities_bound": True,
                    "all_claims_traceable": True,
                },
                "issues": [],
            },
            "strict_quality_failed": False,
        }],
    )

    assert output["status"] == "passed"


def test_runtime_review_serializes_same_hard_acceptance_summary(tmp_path):
    from tools.phase7.run_live_conversation_system_test import _write_case_artifact

    output = {
        "coverage_summary": {
            "hard_acceptance": {"runtime_passed": True, "obligation_passed": False, "passed": False},
            "runtime_correctness": {"all_required_queries_complete": True},
        },
        "turns": [{"index": 1, "real_clickhouse_review": {}, "obligation_review": {}}],
    }
    _write_case_artifact(tmp_path, "serialized-hard", output)
    runtime = json.loads((tmp_path / "serialized-hard.runtime-review.json").read_text())
    coverage = json.loads((tmp_path / "serialized-hard.coverage-summary.json").read_text())
    assert runtime["hard_acceptance"] == coverage["hard_acceptance"]


def test_runtime_observation_does_not_copy_expected_and_requires_excluded_gap():
    from tools.phase7.run_live_conversation_system_test import review_case_obligations

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    base = {
        "status": "completed",
        "accepted_graph": ["data_quality_profile"],
        "scenario": {
            "question_family": "business_object_impact_review",
            "target_metrics": ["paid_amount"],
            "expected_dataset_states": {"gameplay_channel": "contract_partial"},
            "excluded_inputs": {"gameplay_channel": "contract_partial"},
            "allowed_claim_ceiling": "candidate_mechanism",
            "terminal_boundary": "contract_allowed_partial",
        },
        "runtime_authority": {"contract_gaps": []},
    }
    review = review_case_obligations(base, registry)
    assert review["observed_dataset_states"] == {}
    assert review["missing_current_data_obligations"] == ["gameplay_channel:contract_partial"]
    assert review["missing_expected_typed_gaps"] == ["gameplay_channel:contract_partial"]
    assert review["hard_acceptance_passed"] is False


def test_obligation_review_resolves_current_state_from_release_authority():
    from tools.phase7.run_live_conversation_system_test import review_case_obligations

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    turn = {
        "status": "completed",
        "accepted_graph": [
            "metric_coverage_profile",
            "data_quality_profile",
            "answer_verify",
        ],
        "scenario": {
            "question_family": "data_quality_or_evidence_review",
            "target_metrics": ["paid_amount"],
            "expected_dataset_states": {"paid_order_success": "executable"},
            "allowed_claim_ceiling": "trust_boundary",
            "terminal_boundary": "contract_allowed_partial",
        },
        "runtime_authority": {
            **_run_matched_contract_authority(
                _analysis_contract_gap_authority([{
                    "dataset_id": "paid_order_success",
                    "gap_type": "dataset_snapshot_unavailable_as_of",
                    "gap_id": "dataset:paid_order_success:dataset_snapshot_unavailable_as_of",
                    "owner": "data_owner",
                }], [
                    "metric_coverage_profile",
                    "data_quality_profile",
                    "answer_verify",
                ]),
                run_id="run-release-authority",
            ),
        },
    }
    coverage_authority = {
        "as_of": "2026-06-03T12:00:00+01:00",
        "permission_scope": "analyst",
        "cells": {
            "data_quality_profile:paid_order_success": {
                "capability": "data_quality_profile",
                "datasets": ["paid_order_success"],
                "question_families": ["data_quality_or_evidence_review"],
                "state": "snapshot_unavailable_as_of",
            },
        },
    }

    review = review_case_obligations(
        turn,
        registry,
        coverage_authority=coverage_authority,
    )

    assert review["authored_expected_dataset_states"] == {
        "paid_order_success": "executable"
    }
    assert review["expected_dataset_states"] == {
        "paid_order_success": "snapshot_unavailable_as_of"
    }
    assert review["authored_authority_mismatches"] == [
        "paid_order_success:executable->snapshot_unavailable_as_of"
    ]
    assert review["expected_typed_gaps"] == {
        "paid_order_success": "snapshot_unavailable_as_of"
    }
    assert review["missing_current_data_obligations"] == []
    assert review["hard_acceptance_passed"] is True


def test_obligation_review_fails_closed_when_declared_role_has_no_authority_cell():
    from tools.phase7.run_live_conversation_system_test import review_case_obligations

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    review = review_case_obligations(
        {
            "status": "completed",
            "accepted_graph": ["data_quality_profile", "answer_verify"],
            "scenario": {
                "question_family": "data_quality_or_evidence_review",
                "target_metrics": ["paid_amount"],
                "expected_dataset_states": {"paid_order_success": "executable"},
                "allowed_claim_ceiling": "trust_boundary",
                "terminal_boundary": "verified_answer",
            },
            "runtime_authority": {},
        },
        registry,
        coverage_authority={"cells": {}},
    )

    assert review["unresolved_authority_dataset_roles"] == ["paid_order_success"]
    assert review["hard_acceptance_passed"] is False


def test_authority_resolution_uses_dataset_role_cell_when_route_is_independent():
    from tools.phase7.run_live_conversation_system_test import review_case_obligations

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    review = review_case_obligations(
        {
            "status": "completed",
            "accepted_graph": ["pattern_scan", "segment_contribution", "joint_attribution"],
            "scenario": {
                "question_family": "pattern_explanation",
                "target_metrics": ["paid_amount"],
                "requested_dimensions": ["channel"],
                "expected_dataset_states": {"market_dashboard_channel": "executable"},
                "allowed_claim_ceiling": "directional",
                "terminal_boundary": "contract_allowed_partial",
            },
            "runtime_authority": {
                "contract_gaps": [{
                    "dataset_id": "market_dashboard_channel",
                    "gap_type": "contract_partial",
                }]
            },
        },
        registry,
        coverage_authority={
            "cells": {
                "market_pattern_compare:market_dashboard_channel": {
                    "capability": "market_pattern_compare",
                    "datasets": ["market_dashboard_channel"],
                    "question_families": [],
                    "state": "contract_partial",
                }
            }
        },
    )

    assert review["unresolved_authority_dataset_roles"] == []
    assert review["ambiguous_authority_dataset_roles"] == [
        "market_dashboard_channel"
    ]
    assert review["expected_dataset_states"] == {
        "market_dashboard_channel": "contract_partial"
    }


def test_ambiguous_dataset_role_resolution_uses_conservative_authority_state():
    from tools.phase7.run_live_conversation_system_test import review_case_obligations

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    review = review_case_obligations(
        {
            "status": "completed",
            "accepted_graph": ["compare_periods", "driver_decomposition"],
            "scenario": {
                "question_family": "paid_amount_change_explanation",
                "target_metrics": ["paid_amount"],
                "expected_dataset_states": {"market_dashboard": "executable"},
                "allowed_claim_ceiling": "directional",
                "terminal_boundary": "contract_allowed_partial",
            },
            "runtime_authority": _run_matched_contract_authority(
                _analysis_contract_gap_authority([{
                    "dataset_id": "market_dashboard",
                    "gap_type": "contract_partial",
                    "gap_id": "dataset:market_dashboard:contract_partial:required_fields",
                    "owner": "contract_owner",
                }], ["compare_periods", "driver_decomposition"]),
                run_id="run-ambiguous-authority",
            ),
        },
        registry,
        coverage_authority={
            "cells": {
                "market_health_compare:market_dashboard": {
                    "capability": "market_health_compare",
                    "datasets": ["market_dashboard"],
                    "question_families": [],
                    "state": "executable",
                },
                "source_reconciliation:market_dashboard": {
                    "capability": "source_reconciliation",
                    "datasets": ["market_dashboard"],
                    "question_families": [],
                    "state": "contract_partial",
                },
            }
        },
    )

    assert review["expected_dataset_states"] == {
        "market_dashboard": "contract_partial"
    }
    assert review["ambiguous_authority_dataset_roles"] == ["market_dashboard"]
    assert review["missing_current_data_obligations"] == []


def test_coverage_summary_counts_declared_clarification_and_exact_reuse_only():
    from tools.phase7.run_live_conversation_system_test import _coverage_summary

    turns = [
        {
            "topic_id": "topic-1",
            "resumed_topic_id": "topic-1",
            "resumed_status": "completed",
            "scenario": {"clarification_resume": "required"},
            "obligation_review": {"hard_acceptance_passed": True, "clarification_resume_passed": True},
            "quality_review": {"display_status": "", "direct_answer": False},
            "real_clickhouse_review": {"runtime_correctness": {"all_required_queries_complete": True, "all_capabilities_bound": True, "all_claims_traceable": True}},
        },
        {
            "topic_id": "topic-1",
            "prior_topic_id": "topic-1",
            "scenario": {"reuse": "required"},
            "runtime_authority": {
                "sections": [{"payload": {"reuse_decisions": [{"decision": "reuse"}]}}]
            },
            "obligation_review": {
                "hard_acceptance_passed": True,
                "reuse_passed": True,
                "reuse_review": {"passed": True, "errors": []},
            },
            "quality_review": {"display_status": "ready", "direct_answer": True},
            "real_clickhouse_review": {"runtime_correctness": {"all_required_queries_complete": True, "all_capabilities_bound": True, "all_claims_traceable": True}},
        },
        {"topic_id": "topic-2", "resumed_topic_id": "topic-2", "resumed_status": "completed", "scenario": {}},
    ]
    summary = _coverage_summary(turns)
    assert summary["final_answer_audit_coverage"] == {"reviewed": 0, "total": 3}
    assert summary["clarification_resume"] == {"required": 1, "passed": 1}
    assert summary["reuse_coverage"] == {"required": 1, "passed": 1}
    turns[0]["resumed_status"] = "failed"
    turns[0]["obligation_review"]["hard_acceptance_passed"] = False
    turns[1]["obligation_review"]["reuse_review"]["passed"] = False
    turns[1]["obligation_review"]["reuse_passed"] = False
    turns[1]["obligation_review"]["hard_acceptance_passed"] = False
    failed = _coverage_summary(turns)
    assert failed["clarification_resume"] == {"required": 0, "passed": 0}
    assert failed["final_answer_audit_coverage"] == {"reviewed": 0, "total": 2}
    assert failed["reuse_coverage"] == {"required": 1, "passed": 0}


def test_coverage_summary_reads_run_matched_internal_audit_for_zero_claim_terminal(
    tmp_path, monkeypatch
):
    from tools.phase7 import run_live_conversation_system_test as system_test

    artifact_root = tmp_path / "artifacts"
    internal_path = artifact_root / "phase-7" / "run-boundary-only" / "answer_package.json"
    internal_path.parent.mkdir(parents=True)
    monkeypatch.setattr(system_test, "ROOT", tmp_path)
    internal_path.write_text(
        json.dumps(
            {
                "run_id": "run-boundary-only",
                "final_answer": "当前数据边界不足，已给出负责方与下一步。",
                "quality_gate": {
                    "display_status": "ready",
                    "has_verified_claims": False,
                    "blocks_display": False,
                },
                "llm_calls": [
                    {
                        "task": "final_answer_audit",
                        "structured_output": {
                            "display_status": "ready",
                            "hard_blockers": [],
                            "repairable_warnings": [],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    turns = [
        {
            "resumed_status": "completed",
            "resumed_run_id": "run-boundary-only",
            "resumed_artifact_path": str(internal_path),
            "resumed_answer_package": {
                "run_id": "run-boundary-only",
                "final_answer": "当前数据边界不足，已给出负责方与下一步。",
                "quality_gate": {
                    "display_status": "",
                    "has_verified_claims": False,
                },
                "llm_calls": [],
            },
        }
    ]

    summary = system_test._coverage_summary(turns)

    assert summary["final_answer_audit_coverage"] == {"reviewed": 1, "total": 1}

    turns[0]["resumed_quality_review"] = {"display_status": "ready"}
    turns[0]["resumed_run_id"] = "run-different"
    mismatched = system_test._coverage_summary(turns)
    assert mismatched["final_answer_audit_coverage"] == {"reviewed": 0, "total": 1}


def test_coverage_summary_rejects_internal_audit_path_outside_artifact_root(
    tmp_path, monkeypatch
):
    from tools.phase7 import run_live_conversation_system_test as system_test

    (tmp_path / "artifacts").mkdir()
    outside_path = tmp_path / "outside-answer-package.json"
    outside_path.write_text(
        json.dumps(
            {
                "run_id": "run-outside",
                "llm_calls": [
                    {
                        "task": "final_answer_audit",
                        "structured_output": {"display_status": "ready"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(system_test, "ROOT", tmp_path)
    turns = [
        {
            "status": "completed",
            "run_id": "run-outside",
            "artifact_path": "artifacts/../outside-answer-package.json",
            "quality_review": {"display_status": "ready"},
        }
    ]

    summary = system_test._coverage_summary(turns)

    assert summary["final_answer_audit_coverage"] == {"reviewed": 0, "total": 1}


def test_coverage_summary_rejects_internal_audit_from_sibling_suite(
    tmp_path, monkeypatch
):
    from tools.phase7 import run_live_conversation_system_test as system_test

    sibling_path = (
        tmp_path
        / "artifacts"
        / "other-suite"
        / "run-sibling-audit"
        / "answer_package.json"
    )
    sibling_path.parent.mkdir(parents=True)
    sibling_path.write_text(
        json.dumps(
            {
                "run_id": "run-sibling-audit",
                "llm_calls": [
                    {
                        "task": "final_answer_audit",
                        "structured_output": {"display_status": "ready"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(system_test, "ROOT", tmp_path)

    summary = system_test._coverage_summary(
        [
            {
                "status": "completed",
                "run_id": "run-sibling-audit",
                "artifact_path": str(sibling_path),
                "quality_review": {"display_status": "ready"},
            }
        ]
    )

    assert summary["final_answer_audit_coverage"] == {"reviewed": 0, "total": 1}


def test_coverage_summary_separates_expected_from_observed_dataset_states():
    from tools.phase7.run_live_conversation_system_test import _coverage_summary

    summary = _coverage_summary([
        {
            "obligation_review": {
                "required_capabilities": ["compare_periods"],
                "missing_required_capabilities": [],
                "expected_dataset_states": {
                    "paid_order_success": "executable",
                    "payment_attempt": "source_unbound",
                },
                "observed_dataset_states": {
                    "paid_order_success": "executable",
                },
                "missing_current_data_obligations": [
                    "payment_attempt:source_unbound"
                ],
                "hard_acceptance_passed": False,
            },
            "real_clickhouse_review": {"runtime_correctness": {}},
        }
    ])

    assert summary["expected_dataset_coverage"] == {
        "paid_order_success": {"executable": 1},
        "payment_attempt": {"source_unbound": 1},
    }
    assert summary["observed_dataset_coverage"] == {
        "paid_order_success": {"executable": 1},
        "payment_attempt": {"unobserved": 1},
    }
    assert summary["dataset_coverage_deprecated"] == {
        "meaning": "expected_dataset_coverage",
        "coverage": summary["expected_dataset_coverage"],
    }


def test_coverage_summary_reports_authored_and_persisted_question_families():
    from tools.phase7.run_live_conversation_system_test import _coverage_summary

    summary = _coverage_summary([
        {
            "obligation_review": {
                "authored_question_family": "business_object_impact_review",
                "question_family": "segment_or_factor_attribution",
                "question_families": [
                    "segment_or_factor_attribution",
                    "data_quality_or_evidence_review",
                ],
                "question_family_authority_status": "mismatch",
                "required_capabilities": [],
                "hard_acceptance_passed": False,
            },
            "real_clickhouse_review": {"runtime_correctness": {}},
        }
    ])

    assert summary["question_family_coverage"] == {
        "authored": {"business_object_impact_review": 1},
        "persisted": {
            "segment_or_factor_attribution": 1,
            "data_quality_or_evidence_review": 1,
        },
        "persisted_sets": {
            "segment_or_factor_attribution|data_quality_or_evidence_review": 1
        },
        "authority_status": {"mismatch": 1},
        "mismatches": 1,
    }


def test_eval_review_summary_preserves_both_question_family_views(tmp_path):
    from tools.phase7.review_analysis_contract_eval import review_artifact

    family_coverage = {
        "authored": {"business_object_impact_review": 1},
        "persisted": {"segment_or_factor_attribution": 1},
        "authority_status": {"mismatch": 1},
        "mismatches": 1,
    }
    path = tmp_path / "case.json"
    path.write_text(
        json.dumps({
            "case_id": "family-authority-review",
            "coverage_summary": {"question_family_coverage": family_coverage},
            "turns": [],
        }),
        encoding="utf-8",
    )

    assert review_artifact(path)["question_family_coverage"] == family_coverage


def test_obligation_coverage_outcomes_are_mutually_exclusive_and_authoritative():
    from tools.phase7.run_live_conversation_system_test import _coverage_summary

    summary = _coverage_summary([
        {
            "obligation_review": {
                "required_capabilities": [
                    "ready",
                    "partial",
                    "stopped",
                    "unseen",
                    "unrouted",
                ],
                "capability_outcomes": {
                    "ready": "executed",
                    "partial": "degraded",
                    "stopped": "blocked",
                    "unseen": "unobserved",
                    "unrouted": "missing_route",
                },
                "hard_acceptance_passed": False,
            },
            "real_clickhouse_review": {"runtime_correctness": {}},
        }
    ])

    assert summary["obligation_coverage"] == {
        "required": 5,
        "routed": 4,
        "terminal": 3,
        "executed": 1,
        "degraded": 1,
        "blocked": 1,
        "unobserved": 1,
        "missing_route": 1,
    }


def test_obligation_review_rejects_binding_result_without_persisted_plan_chain():
    from tools.phase7.run_live_conversation_system_test import review_case_obligations

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    turn = {
        "status": "completed",
        "accepted_graph": [
            "metric_coverage_profile",
            "data_quality_profile",
            "answer_verify",
        ],
        "scenario": {
            "question_family": "data_quality_or_evidence_review",
            "target_metrics": ["paid_amount"],
            "expected_dataset_states": {},
            "allowed_claim_ceiling": "trust_boundary",
            "terminal_boundary": "verified_answer",
        },
        "runtime_authority": {
            "query_executions": [
                {
                    "result_ref": "result:complete",
                    "execution_status": "succeeded",
                    "completeness_status": "complete",
                    "analysis_readiness": "ready",
                },
                {
                    "result_ref": "result:partial",
                    "execution_status": "succeeded",
                    "completeness_status": "partial",
                    "analysis_readiness": "degraded",
                },
            ],
            "capability_bindings": [
                {
                    "capability_id": "metric_coverage_profile",
                    "status": "ready",
                    "result_refs": ["result:complete"],
                },
                {
                    "capability_id": "data_quality_profile",
                    "status": "degraded",
                    "result_refs": ["result:partial"],
                },
                {
                    "capability_id": "answer_verify",
                    "status": "ready",
                    "result_refs": ["result:missing"],
                },
            ],
            "analysis_contract": _analysis_contract_gap_authority(
                [],
                [
                    "metric_coverage_profile",
                    "data_quality_profile",
                    "answer_verify",
                ],
            ),
        },
    }

    review = review_case_obligations(turn, registry)

    assert review["capability_outcomes"] == {
        "metric_coverage_profile": "unobserved",
        "data_quality_profile": "unobserved",
        "answer_verify": "unobserved",
    }


@pytest.mark.parametrize(
    ("accepted_graph", "runtime_authority", "expected_outcome"),
    [
        (["answer_verify"], {}, "unobserved"),
        ([], {}, "missing_route"),
        (
            [],
            {
                "contract_gaps": [{
                    "gap_type": "contract_partial",
                    "gap_id": "dataset:paid_order_success:contract_partial",
                    "dataset_id": "paid_order_success",
                    "affected_capabilities": ["different_capability"],
                    "owner": "contract_owner",
                }]
            },
            "missing_route",
        ),
    ],
)
def test_capability_outcome_derivation_rejects_every_nonterminal_outcome(
    accepted_graph, runtime_authority, expected_outcome
):
    from tools.phase7.run_live_conversation_system_test import (
        _derive_capability_outcomes,
    )

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    outcomes = _derive_capability_outcomes(
        ("answer_verify",),
        accepted_capabilities=set(accepted_graph),
        authority=runtime_authority,
        registry=registry,
    )

    assert outcomes == {"answer_verify": expected_outcome}


@pytest.mark.parametrize("terminal_outcome", ["executed", "degraded", "blocked"])
def test_capability_outcome_derivation_accepts_only_authority_backed_terminal_outcomes(
    terminal_outcome
):
    from tools.phase7.run_live_conversation_system_test import (
        _derive_capability_outcomes,
    )

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    accepted_graph = [] if terminal_outcome == "blocked" else ["answer_verify"]
    if terminal_outcome == "blocked":
        blocked_capabilities = [
            "data_quality_profile",
            "metric_coverage_profile",
            "answer_verify",
        ]
        runtime_authority = {
            "analysis_contract": _analysis_contract_gap_authority(
                [
                    {
                        "gap_type": "contract_partial",
                        "gap_id": (
                            f"capability:{capability_id}:required_query:slot:unbound"
                        ),
                        "dataset_id": "paid_order_success",
                        "owner": "contract_owner",
                    }
                    for capability_id in blocked_capabilities
                ],
                blocked_capabilities,
            )
        }
    else:
        partial = terminal_outcome == "degraded"
        runtime_authority = {
            "query_executions": [{
                "result_ref": "result:answer-verify",
                "execution_status": "succeeded",
                "completeness_status": "partial" if partial else "complete",
                "analysis_readiness": "degraded" if partial else "ready",
            }],
            "capability_bindings": [{
                "capability_id": "answer_verify",
                "status": "degraded" if partial else "ready",
                "result_refs": ["result:answer-verify"],
            }],
        }
    outcomes = _derive_capability_outcomes(
        ("answer_verify",),
        accepted_capabilities=set(accepted_graph),
        authority=runtime_authority,
        registry=registry,
    )

    expected = "blocked" if terminal_outcome == "blocked" else "unobserved"
    assert outcomes == {"answer_verify": expected}


def test_cli_case_selection_rejects_conflicts_cross_suite_and_unknown():
    from tools.phase7.run_live_conversation_system_test import resolve_cli_cases

    with pytest.raises(ValueError, match="eval_cli_source_conflict"):
        resolve_cli_cases("fixed-eight", "custom.yaml", None)
    with pytest.raises(ValueError, match="eval_case_not_in_suite"):
        resolve_cli_cases("fixed-eight", None, "platform_paid_amount_change")
    with pytest.raises(ValueError, match="eval_case_unknown"):
        resolve_cli_cases(None, "evals/phase7/conversation_scenarios.yaml", "absent")


def test_cli_selection_error_is_typed_and_nonzero(capsys):
    from tools.phase7.run_live_conversation_system_test import main

    with pytest.raises(SystemExit) as exc:
        main(["--suite", "fixed-eight", "--case", "platform_paid_amount_change"])
    assert exc.value.code == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload == {
        "ok": False,
        "error_code": "eval_case_not_in_suite",
        "owner": "eval_operator",
        "impact": "no evaluation cases were executed",
    }


class Releases:
    def __init__(self, records):
        self.records = {record.release_ref: record for record in records}

    def resolve_dataset_release(self, release_ref):
        return self.records[release_ref]


def authority_inputs(registry):
    snapshots = {}
    for case in current_data_coverage_cases(registry):
        for snapshot in (case.snapshots or {}).values():
            snapshots.setdefault(snapshot.dataset_id, snapshot)
    selected = []
    records = []
    for dataset_id in ("paid_order_success", "market_dashboard", "market_dashboard_channel", "gameplay", "gameplay_channel", "external_event"):
        snapshot = snapshots[dataset_id]
        membership = tuple(registry.dataset(dataset_id)["release_membership"]["dataset_ids"])
        members = [snapshots[item] for item in membership]
        record = build_dataset_release_authority_record(
            tuple({**item.to_dict(), "requires_release": True} for item in members)
        )
        records.append(record)
        selected.append(replace(snapshot, authority_record_ref=record.authority_record_ref))
    return tuple(selected), Releases(records)


def releases_for(registry, snapshots):
    by_dataset = {item.dataset_id: item for item in snapshots}
    records = []
    normalized = {}
    seen = set()
    for snapshot in snapshots:
        membership = tuple(registry.dataset(snapshot.dataset_id)["release_membership"]["dataset_ids"])
        key = tuple(membership)
        if key in seen:
            continue
        seen.add(key)
        members = [by_dataset[item] for item in membership]
        release_ref = dataset_snapshot_release_ref(
            members[0].logical_snapshot_id, members[0].load_revision,
            tuple(item.snapshot_ref for item in members),
        )
        members = [replace(item, release_ref=release_ref, authority_record_ref="") for item in members]
        record = build_dataset_release_authority_record(tuple(
            {**item.to_dict(), "requires_release": True} for item in members
        ))
        records.append(record)
        normalized.update({item.dataset_id: replace(item, authority_record_ref=record.authority_record_ref) for item in members})
    return tuple(normalized[item.dataset_id] for item in snapshots), Releases(records)


def test_coverage_audit_reports_current_and_excluded_cells():
    from bi_agent.runtime.coverage_audit import audit_existing_data_coverage

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    snapshots, releases = authority_inputs(registry)
    audit = audit_existing_data_coverage(
        registry,
        snapshot_records=snapshots,
        release_resolver=releases,
        as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        permission_scope="analyst",
    )
    assert audit["states"] == [
        "executable", "degraded", "source_unbound", "contract_partial",
        "permission_blocked", "snapshot_unavailable_as_of",
    ]
    assert audit["cells"]["market_health_compare:market_dashboard"]["state"] == "executable"
    assert audit["cells"]["source_reconciliation:market_dashboard"]["state"] == "contract_partial"
    assert audit["cells"]["event_evidence:external_event"]["state"] == "executable"
    assert audit["cells"]["driver_decomposition:payment_attempt"]["state"] == "source_unbound"
    excluded = audit["cells"]["event_evidence:internal_operation_event"]
    assert excluded["owner"] == "data_operations_owner"
    required = {"question_families", "capability", "datasets", "metrics", "dimensions", "windows", "evidence_types", "claim_ceiling", "current_release_refs", "state", "owner", "impact", "next_action"}
    assert required <= set(excluded)
    assert list(audit["cells"]) == sorted(audit["cells"])
    assert audit["cells"]["market_health_compare:market_dashboard"]["current_releases"][0]["load_revision"]


def test_context_capability_dataset_roles_persist_terminal_contract_authority():
    from bi_agent.runtime.analysis_contract_compiler import (
        _merge_contract_gaps,
        compile_analysis_contract,
    )
    from bi_agent.runtime.dataset_catalog import DatasetCatalog
    from tools.phase7.run_live_conversation_system_test import (
        _derive_runtime_dataset_states,
    )

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    snapshots, releases = authority_inputs(registry)
    snapshots, releases = releases_for(
        registry,
        tuple(
            replace(snapshot, evidence_state="context_only")
            if snapshot.dataset_id == "market_dashboard_channel"
            else replace(snapshot, loaded_at="2026-06-09T00:00:00+00:00")
            if snapshot.dataset_id == "external_event"
            else snapshot
            for snapshot in snapshots
        ),
    )
    catalog = DatasetCatalog(snapshots)
    as_of = datetime.fromisoformat("2026-06-03T12:00:00+01:00")

    channel = compile_analysis_contract(
        run_id="run-context-channel-authority",
        proposal={"target_metrics": ["paid_amount"]},
        accepted_capabilities=("market_channel_context",),
        catalog=catalog,
        registry=registry,
        as_of=as_of,
        permission_scope="analyst",
        release_resolver=releases,
    )
    assert "market_dashboard_channel" in channel.analysis_contract.dataset_requirements
    assert any(
        gap.gap_id
        == (
            "dataset:market_dashboard_channel:evidence_state:context_only:"
            "capability:market_channel_context"
        )
        and gap.dataset_id == "market_dashboard_channel"
        and gap.affected_capabilities == ("market_channel_context",)
        for gap in channel.analysis_contract.contract_gaps
    )
    context_gap = next(
        gap
        for gap in channel.analysis_contract.contract_gaps
        if "evidence_state:context_only" in gap.gap_id
    )
    merged_gap = _merge_contract_gaps(
        tuple(
            replace(context_gap, repair_options=(repair_option,))
            for repair_option in reversed(context_gap.repair_options)
        )
    )[0]
    serialized_contract = replace(
        channel.analysis_contract,
        contract_gaps=(merged_gap,),
    ).to_dict()
    states, _ = _derive_runtime_dataset_states(
        _run_matched_contract_authority(
            serialized_contract,
            run_id="run-context-channel-authority",
        ),
        registry=registry,
    )
    assert states["market_dashboard_channel"] == "degraded"

    event = compile_analysis_contract(
        run_id="run-context-event-authority",
        proposal={
            "target_metrics": ["paid_amount"],
            "requested_context_sources": ["external_event"],
        },
        accepted_capabilities=("event_evidence",),
        catalog=catalog,
        registry=registry,
        as_of=as_of,
        permission_scope="analyst",
        release_resolver=releases,
    )
    assert "external_event" in event.analysis_contract.dataset_requirements
    assert any(
        gap.gap_type == "dataset_snapshot_unavailable_as_of"
        and gap.dataset_id == "external_event"
        and "event_evidence" in gap.affected_capabilities
        for gap in event.analysis_contract.contract_gaps
    )


def test_metric_sources_are_resolved_per_capability_before_global_reconciliation():
    from bi_agent.runtime.analysis_contract_compiler import compile_analysis_contract
    from bi_agent.runtime.dataset_catalog import DatasetCatalog

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    as_of = datetime.fromisoformat("2026-06-03T12:00:00+01:00")
    outcome = compile_analysis_contract(
        run_id="run-per-capability-source-selection",
        proposal={"target_metrics": ["paid_amount"]},
        accepted_capabilities=(
            "data_quality_profile",
            "market_channel_context",
            "gameplay_activity_context",
        ),
        catalog=DatasetCatalog(()),
        registry=registry,
        as_of=as_of,
        permission_scope="analyst",
    )

    assert {
        "paid_order_success",
        "market_dashboard_channel",
        "gameplay",
        "gameplay_channel",
    } <= set(outcome.analysis_contract.dataset_requirements)
    gameplay_ambiguity = next(
        gap
        for gap in outcome.analysis_contract.contract_gaps
        if gap.gap_id.startswith("metric:player_bet_amount:source_ambiguous:")
    )
    assert gameplay_ambiguity.affected_capabilities == (
        "gameplay_activity_context",
    )
    assert gameplay_ambiguity.requires_clarification is True


def _accepted_context_gap_authority(
    *, run_id="run-context-authority", plan_mutation=None
):
    gap = ContractGap(
        gap_type="contract_partial",
        gap_id=(
            "dataset:market_dashboard_channel:evidence_state:context_only:"
            "capability:market_channel_context"
        ),
        dataset_id="market_dashboard_channel",
        affected_capabilities=("market_channel_context",),
        affected_claim_types=("contract_coverage_and_trust_boundary",),
        owner="data_owner",
        repair_options=(
            "use_context_only_query",
            "publish_claim_ready_release",
            "resolve_reconciliation",
        ),
        requires_clarification=False,
        diagnostic_context={},
    )
    contract = AnalysisContract(
        analysis_contract_id=f"analysis:{run_id}:1",
        contract_version="1",
        question_families=("business_object_impact_review",),
        target_metric_refs=(),
        claim_intents=("contract_coverage_and_trust_boundary",),
        scope={},
        business_timezone="Europe/London",
        as_of="2026-06-03T12:00:00+01:00",
        resolved_windows=(),
        metric_bindings=(),
        dimension_bindings=(),
        dataset_requirements=("market_dashboard_channel",),
        capability_requirements=("market_channel_context",),
        permission_scope="analyst",
        contract_gaps=(gap,),
    ).to_dict()
    plan_contract = json.loads(json.dumps(contract))
    if plan_mutation is not None:
        plan_mutation(plan_contract)
    return {
        "run_id": run_id,
        "admin_audit": {
            "analysis_contract": contract,
            "compiler_runtime_plan": {"analysis_contract": plan_contract},
        },
    }


def test_runtime_dataset_state_ignores_forged_sibling_gap():
    from tools.phase7.run_live_conversation_system_test import (
        _derive_runtime_dataset_states,
    )

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    authority = _accepted_context_gap_authority()
    forged = authority["admin_audit"]["analysis_contract"]["contract_gaps"][0]
    authority["admin_audit"]["analysis_contract"]["contract_gaps"] = []
    authority["admin_audit"]["compiler_runtime_plan"]["analysis_contract"][
        "contract_gaps"
    ] = []
    authority["typed_gaps"] = [forged]

    states, gaps = _derive_runtime_dataset_states(authority, registry=registry)

    assert states == {}
    assert gaps == {}


def test_runtime_dataset_state_requires_run_matched_contract_and_fails_review():
    from tools.phase7.run_live_conversation_system_test import (
        _derive_runtime_dataset_states,
        review_case_obligations,
    )

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    authority = _accepted_context_gap_authority(run_id="different-run")
    authority["run_id"] = "expected-run"

    states, _ = _derive_runtime_dataset_states(authority, registry=registry)
    review = review_case_obligations(
        {
            "status": "completed",
            "answer_package": {"summary": "bounded result"},
            "accepted_graph": ["market_channel_context"],
            "scenario": {
                "required_capabilities": ["market_channel_context"],
                "expected_dataset_states": {
                    "market_dashboard_channel": "degraded"
                },
                "allowed_claim_ceiling": "candidate_mechanism",
                "terminal_boundary": "contract_allowed_partial",
            },
            "runtime_authority": authority,
        },
        registry,
    )

    assert states == {}
    assert review["missing_current_data_obligations"] == [
        "market_dashboard_channel:degraded"
    ]
    assert review["hard_acceptance_passed"] is False


def test_runtime_dataset_state_accepts_matching_context_only_contract():
    from tools.phase7.run_live_conversation_system_test import (
        _derive_runtime_dataset_states,
    )

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    states, gaps = _derive_runtime_dataset_states(
        _accepted_context_gap_authority(), registry=registry
    )

    assert states == {"market_dashboard_channel": "degraded"}
    assert gaps == {"market_dashboard_channel": ("contract_partial",)}


@pytest.mark.parametrize(
    "plan_mutation",
    [
        lambda contract: contract.update(permission_scope="admin"),
        lambda contract: contract["contract_gaps"][0].update(
            gap_id="dataset:forged:contract_partial"
        ),
    ],
)
def test_runtime_dataset_state_rejects_mismatched_compiler_plan(plan_mutation):
    from tools.phase7.run_live_conversation_system_test import (
        _derive_runtime_dataset_states,
    )

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    states, gaps = _derive_runtime_dataset_states(
        _accepted_context_gap_authority(plan_mutation=plan_mutation),
        registry=registry,
    )

    assert states == {}
    assert gaps == {}


def test_queryless_checkpoint_uses_final_run_bound_matching_event():
    from tools.phase7.run_live_conversation_system_test import (
        _queryless_completion_authority_passed,
    )

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    authority = _accepted_context_gap_authority(run_id="run-checkpoint")
    for contract in (
        authority["admin_audit"]["analysis_contract"],
        authority["admin_audit"]["compiler_runtime_plan"]["analysis_contract"],
    ):
        contract["capability_requirements"] = (
            *contract["capability_requirements"],
            "evidence_reduce",
        )
    authority["checkpoint_events"] = [
        {"node": "reduce_evidence", "status": "failed", "attempt": 1},
        {"node": "reduce_evidence", "status": "completed", "attempt": 2},
    ]

    assert _queryless_completion_authority_passed(
        "evidence_reduce",
        authority=authority,
        admin=authority["admin_audit"],
        registry=registry,
    ) is True
    authority["checkpoint_events"].append(
        {"node": "reduce_evidence", "status": "failed", "attempt": 3}
    )
    assert _queryless_completion_authority_passed(
        "evidence_reduce",
        authority=authority,
        admin=authority["admin_audit"],
        registry=registry,
    ) is False
    authority["run_id"] = "different-run"
    assert _queryless_completion_authority_passed(
        "evidence_reduce",
        authority=authority,
        admin=authority["admin_audit"],
        registry=registry,
    ) is False


def test_coverage_audit_distinguishes_permission_future_and_partial_contract():
    from bi_agent.runtime.coverage_audit import audit_existing_data_coverage

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    snapshots, releases = authority_inputs(registry)
    changed = []
    for snapshot in snapshots:
        if snapshot.dataset_id == "market_dashboard":
            snapshot = replace(snapshot, permission_scopes=("admin",))
        elif snapshot.dataset_id == "external_event":
            snapshot = replace(snapshot, loaded_at="2026-06-04T00:00:00+00:00")
        changed.append(snapshot)
    changed, changed_releases = releases_for(registry, changed)
    audit = audit_existing_data_coverage(
        registry, changed, changed_releases,
        datetime.fromisoformat("2026-06-03T12:00:00+01:00"), "analyst",
    )
    assert audit["cells"]["market_health_compare:market_dashboard"]["state"] == "permission_blocked"
    assert audit["cells"]["event_evidence:external_event"]["state"] == "snapshot_unavailable_as_of"
    future = audit["cells"]["event_evidence:external_event"]
    assert "advance the audit as_of" in future["next_action"]
    assert "publish" not in future["next_action"]


def test_coverage_audit_fails_closed_on_release_integrity():
    from bi_agent.runtime.coverage_audit import audit_existing_data_coverage

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    snapshots, releases = authority_inputs(registry)
    releases.records[next(iter(releases.records))] = replace(
        next(iter(releases.records.values())), integrity_errors=("digest",)
    )
    with pytest.raises(ValueError, match="coverage_release_integrity"):
        audit_existing_data_coverage(registry, snapshots, releases, datetime.fromisoformat("2026-06-03T12:00:00+01:00"), "analyst")


class EmptyStore:
    def runtime_evidence_resolver(self):
        return object()

    def list_dataset_snapshots(self):
        return ()


def test_cli_writes_structurally_valid_source_unbound_artifact(tmp_path):
    from tools.phase7.audit_existing_data_coverage import run_audit

    output = tmp_path / "coverage.json"
    result = run_audit(SimpleNamespace(
        as_of="2026-06-03T12:00:00+01:00",
        permission_scope="analyst",
        out=str(output),
    ), store=EmptyStore())
    artifact = json.loads(output.read_text())
    assert result == {"ok": True, "artifact": str(output), "summary": artifact["summary"]}
    assert artifact["cells"]["driver_decomposition:payment_attempt"]["state"] == "source_unbound"


def test_cli_maps_credential_bearing_resolver_failure_without_disclosure(tmp_path, capsys):
    from tools.phase7.audit_existing_data_coverage import main

    secret = "postgresql://alice:password@secret-db.internal/waje"

    class BrokenStore:
        def runtime_evidence_resolver(self):
            return object()

        def list_dataset_snapshots(self):
            raise RuntimeError(f"connection failed {secret} SELECT * FROM private")

    code = main(
        ["--as-of", "2026-06-03T12:00:00+01:00", "--permission-scope", "analyst", "--out", str(tmp_path / "coverage.json")],
        store_factory=lambda: BrokenStore(),
    )
    captured = capsys.readouterr()
    assert code == 1
    payload = json.loads(captured.err)
    assert payload == {
        "error_code": "coverage_database_unavailable",
        "impact": "current coverage authority could not be read",
        "ok": False,
        "owner": "runtime_operations_owner",
    }
    assert secret not in captured.err
    assert "secret-db" not in captured.err
    assert "SELECT" not in captured.err


def test_cli_maps_hard_release_integrity_failure_nonzero(tmp_path, capsys):
    from tools.phase7.audit_existing_data_coverage import main

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    snapshots, _ = authority_inputs(registry)

    class BrokenAuthorityStore(EmptyStore):
        def list_dataset_snapshots(self):
            return snapshots

        def resolve_dataset_release(self, release_ref):
            raise RuntimeError("postgresql://user:password@host/db")

    code = main(
        ["--as-of", "2026-06-03T12:00:00+01:00", "--permission-scope", "analyst", "--out", str(tmp_path / "coverage.json")],
        store_factory=lambda: BrokenAuthorityStore(),
    )
    payload = json.loads(capsys.readouterr().err)
    assert code == 1
    assert payload["error_code"] == "coverage_release_authority_invalid"
    assert "password" not in json.dumps(payload)


def test_cli_maps_contract_integrity_failure_nonzero(tmp_path, capsys, monkeypatch):
    from tools.phase7 import audit_existing_data_coverage as cli

    def fail_contract(*args, **kwargs):
        raise ValueError("contract query SELECT secret_password")

    monkeypatch.setattr(cli.RuntimeContractRegistry, "from_path", fail_contract)
    code = cli.main(
        ["--as-of", "2026-06-03T12:00:00+01:00", "--permission-scope", "analyst", "--out", str(tmp_path / "coverage.json")],
        store_factory=lambda: EmptyStore(),
    )
    payload = json.loads(capsys.readouterr().err)
    assert code == 1
    assert payload["error_code"] == "coverage_runtime_contract_invalid"
    assert "SELECT" not in json.dumps(payload)
    assert "password" not in json.dumps(payload)


def test_cli_maps_artifact_path_failure_without_echoing_path(tmp_path, capsys):
    from tools.phase7.audit_existing_data_coverage import main

    secret_path = tmp_path / "secret-password-output"
    secret_path.mkdir()
    code = main(
        ["--as-of", "2026-06-03T12:00:00+01:00", "--permission-scope", "analyst", "--out", str(secret_path)],
        store_factory=lambda: EmptyStore(),
    )
    payload = json.loads(capsys.readouterr().err)
    assert code == 1
    assert payload["error_code"] == "coverage_artifact_write_failed"
    assert str(secret_path) not in json.dumps(payload)


def test_cli_maps_credential_bearing_close_failure_nonzero(tmp_path, capsys):
    from tools.phase7.audit_existing_data_coverage import main

    secret = "postgresql://closer:password@close-host.internal/waje"

    class Connection:
        def close(self):
            raise RuntimeError(f"close failed {secret} SELECT pg_terminate_backend")

    store = EmptyStore()
    store.connection = Connection()
    code = main(
        ["--as-of", "2026-06-03T12:00:00+01:00", "--permission-scope", "analyst", "--out", str(tmp_path / "coverage.json")],
        store_factory=lambda: store,
    )
    captured = capsys.readouterr()
    assert code == 1
    assert json.loads(captured.err)["error_code"] == "coverage_database_close_failed"
    assert captured.out == ""
    assert secret not in captured.err
    assert "close-host" not in captured.err
    assert "SELECT" not in captured.err


def test_cli_sanitizes_unknown_arguments_with_credentials(capsys):
    from tools.phase7.audit_existing_data_coverage import main

    secret = "postgresql://alice:password@host.internal/waje"
    code = main(["--bogus", secret], store_factory=lambda: EmptyStore())
    captured = capsys.readouterr()
    assert code == 1
    assert json.loads(captured.err) == {
        "error_code": "coverage_cli_arguments_invalid",
        "impact": "the coverage audit command arguments are invalid",
        "ok": False,
        "owner": "audit_operator",
    }
    assert captured.out == ""
    assert secret not in captured.err
    assert "host.internal" not in captured.err
    assert "usage:" not in captured.err


def test_cli_preserves_primary_error_when_close_also_fails(tmp_path, capsys):
    from tools.phase7.audit_existing_data_coverage import main

    class Connection:
        def close(self):
            raise RuntimeError("postgresql://close:password@close-host/db")

    class BrokenStore(EmptyStore):
        connection = Connection()

        def list_dataset_snapshots(self):
            raise RuntimeError("postgresql://read:password@read-host/db SELECT secret")

    code = main(
        ["--as-of", "2026-06-03T12:00:00+01:00", "--permission-scope", "analyst", "--out", str(tmp_path / "coverage.json")],
        store_factory=lambda: BrokenStore(),
    )
    captured = capsys.readouterr()
    assert code == 1
    assert json.loads(captured.err)["error_code"] == "coverage_database_unavailable"
    assert "password" not in captured.err
    assert "host" not in captured.err
