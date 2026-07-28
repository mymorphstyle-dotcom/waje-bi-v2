from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

from psycopg.errors import CheckViolation

from bi_agent.conversation.postgres_store import PostgresConversationStore
from bi_agent.runtime.analysis_contracts import (
    AnalysisContract,
    CapabilityExecutionPlan,
    CapabilityInputSlot,
    QueryContract,
    QueryResultEnvelope,
    ResultShape,
    analysis_contract_signature,
    query_contract_signature,
)
from bi_agent.runtime.authoritative_execution_result import (
    AuthoritativeExecutionResult,
)
from bi_agent.runtime.capability_authority import (
    CapabilityAdapterOutput,
    CapabilityEvidence,
    CapabilityFailure,
    CapabilityOutcome,
    EvidenceLedgerEntry,
    ExecutionSnapshot,
    ExplorationStopRecord,
    FailureRecord,
)
from bi_agent.runtime.capability_scheduler import (
    _capability_call_spec,
    _journaled_adapter_output,
    capability_execution_transition_payloads,
    topological_ready_waves,
)
from bi_agent.runtime.capability_execution import bind_capability_inputs
from bi_agent.runtime.evidence_authority import (
    EvidenceIntegrityError,
    canonical_digest,
    canonical_result_rows_hash,
)
from bi_agent.runtime.evidence_taxonomy import publication_evidence_kind
from bi_agent.runtime.durable_call_journal import DurableCallSpec
from bi_agent.runtime.runtime_persistence import CapabilitySettlementAuthority
from bi_agent.runtime.query_audit import query_audit_refs
from bi_agent.runtime.query_completeness import ASSERTIONS, validate_query_result
from bi_agent.runtime.single_authority import DurableTransition
from tests.phase7.test_single_authority_phase02_postgres import (
    QUESTION,
    _accepted_provider_attempt_ref,
    _authority_context,
    _intent_revision,
    _plan_revision,
    _plan_transition,
    _planner_proposal,
    _proposal_admission,
    _transition,
)
from tests.phase4.authoritative_query_vectors import verified_dimension_scan_context
from tools.runtime.cutover_single_authority_schema import (
    SINGLE_AUTHORITY_MIGRATION_DIGEST,
    SINGLE_AUTHORITY_MIGRATION_ID,
)


ROOT = Path(__file__).resolve().parents[2]


def _schema() -> str:
    return (ROOT / "tools/runtime/conversation-runtime.sql").read_text(encoding="utf-8")


def test_phase03_declares_task_scoped_authority_without_copying_task_definitions():
    schema = _schema()
    for table in (
        "capability_task_attempts",
        "capability_failure_records",
        "capability_outcomes",
        "capability_evidence_ledger_entries",
        "exploration_stop_records",
        "capability_execution_snapshots",
    ):
        assert f"CREATE TABLE IF NOT EXISTS waje_runtime.{table}" in schema
        assert f"'{table}'" in schema
    assert "capability_task_definitions" not in schema


def test_phase03_authority_uses_restrict_and_append_only_records():
    schema = _schema()
    phase03 = schema[schema.index("-- vNext Phase 3 task-scoped execution authority") :]
    assert "ON DELETE CASCADE" not in phase03
    assert "reject_append_only_authority_mutation()" in phase03
    assert "capability_task_dispatches" in phase03
    assert "Mutable worker coordination" in phase03


def test_phase03_outcome_statuses_are_typed_and_one_outcome_is_accepted_per_task():
    schema = _schema()
    phase03 = schema[
        schema.index("CREATE TABLE IF NOT EXISTS waje_runtime.capability_outcomes") :
    ]
    for status in (
        "succeeded",
        "unavailable",
        "integrity_failed",
        "technical_failed",
        "skipped",
        "superseded",
    ):
        assert f"'{status}'" in phase03
    assert "UNIQUE(plan_revision_id, task_id)" in phase03


def test_phase03_evidence_ledger_is_independent_from_publication_manifest():
    schema = _schema()
    phase03 = schema[schema.index("-- vNext Phase 3 task-scoped execution authority") :]
    ledger = phase03[
        phase03.index(
            "CREATE TABLE IF NOT EXISTS waje_runtime.capability_evidence_ledger_entries"
        ) : phase03.index(
            "CREATE TABLE IF NOT EXISTS waje_runtime.exploration_stop_records"
        )
    ]
    assert "authority_context_ref" in ledger
    assert "binding_record_ref" in ledger
    assert "result_membership_digest" in ledger
    assert "completeness_membership_digest" in ledger
    assert "context_manifest_ref" not in ledger
    assert "evidence_manifests" not in ledger


def test_phase03_evidence_kind_constraint_matches_current_authority_contract():
    schema = _schema()
    ledger = schema[
        schema.index(
            "CREATE TABLE IF NOT EXISTS waje_runtime.capability_evidence_ledger_entries"
        ) : schema.index(
            "CREATE TABLE IF NOT EXISTS waje_runtime.exploration_stop_records"
        )
    ]
    allowed_clause = (
        "evidence_kind IN ("
        "'boundary', 'observed', 'derived', 'scenario', "
        "'statistical_association')"
    )

    assert ledger.count(allowed_clause) == 2
    assert "CONSTRAINT capability_evidence_ledger_entries_evidence_kind_check" in ledger


def test_parallel_task_records_do_not_depend_on_global_transition_head():
    schema = _schema()
    phase03 = schema[
        schema.index("-- vNext Phase 3 task-scoped execution authority") :
        schema.index("-- vNext Phase 4-6 sealed authority")
    ]
    assert "parent_transition_id" not in phase03
    assert "workflow_transition_attempts" not in phase03


def _accepted_plan(
    store: PostgresConversationStore,
) -> tuple[object, DurableTransition]:
    suffix = uuid4().hex
    thread_id = f"phase03-settlement-thread-{suffix}"
    run_id = f"phase03-settlement-run-{suffix}"
    store.create_thread(thread_id, owner_id="phase03-settlement-user")
    store.upsert_run(run_id, thread_id=thread_id, status="running")

    intent = _intent_revision(run_id)
    intent_input = {"question": QUESTION, "contract_test": True}
    intent_output = {"intent_revision": intent.to_dict()}
    intent_transition = _transition(
        node_name="bind_intent",
        run_id=run_id,
        intent_revision_id=intent.intent_revision_id,
        input_payload=intent_input,
        output_payload=intent_output,
        next_transition="compile_authoritative_plan",
    )
    store.save_intent_revision_transition(
        intent_revision=intent,
        transition=intent_transition,
        input_payload=intent_input,
        output_payload=intent_output,
        accepted_attempt_refs=(
            _accepted_provider_attempt_ref(
                store,
                run_id=run_id,
                intent_revision_id=None,
                call_kind="intent_provider",
                stage_name="bind_intent",
            ),
        ),
    )

    context = _authority_context(run_id)
    proposal = _planner_proposal(run_id, intent.intent_revision_id, context)
    admission = _proposal_admission(
        proposal=proposal,
        intent_revision_id=intent.intent_revision_id,
        authority_context=context,
    )
    plan = _plan_revision(
        run_id=run_id,
        intent_revision_id=intent.intent_revision_id,
        authority_context=context,
        proposal=proposal,
        proposal_admission=admission,
    )
    plan_transition, plan_input, plan_output = _plan_transition(
        intent_revision=intent,
        authority_context=context,
        proposal=proposal,
        proposal_admission=admission,
        plan_revision=plan,
        parent_transition_id=intent_transition.transition_id,
    )
    store.save_plan_revision_transition(
        authority_context=context,
        planner_proposal=proposal,
        proposal_admission=admission,
        plan_revision=plan,
        transition=plan_transition,
        input_payload=plan_input,
        output_payload=plan_output,
        accepted_attempt_refs=(
            _accepted_provider_attempt_ref(
                store,
                run_id=run_id,
                intent_revision_id=intent.intent_revision_id,
                call_kind="planner_provider",
                stage_name="compile_authoritative_plan",
            ),
        ),
    )
    return plan, plan_transition


def _persist_execution_closure(
    store: PostgresConversationStore,
    plan,
    *,
    evidence_kind: str = "observed",
):
    outcomes = []
    entries = []
    for wave in topological_ready_waves(plan):
        for task in wave:
            evidence = CapabilityEvidence.create(
                evidence_ref=f"evidence:{task.task_id}",
                binding_record_ref=None,
                execution_state="available",
                evidence_kind=evidence_kind,
                data_contract_state="complete",
                supported_claim_kinds=("comparative_change",),
                evidence_strength="high",
                maximum_claim_strength="descriptive",
                observation_facts=(
                    {
                        "fact_ref": f"fact:{task.task_id}",
                        "value": 1,
                    },
                ),
                scope="full_sample",
                window_refs=plan.resolved_window_refs,
                dimension_path=(),
                limitation_refs=(),
                result_refs=(),
                completeness_report_refs=(),
                hierarchy_qualified=False,
            )
            adapter_output = CapabilityAdapterOutput.create(
                status="succeeded",
                output_payload={"task_id": task.task_id, "value": 1},
                evidence=(evidence,),
                affected_obligation_ids=task.supports_obligation_ids,
                limitation_refs=(),
                retryability="never",
            )
            attempt, adapter_output = _journaled_adapter_output(
                plan,
                task,
                attempt_journal=store.attempt_journal,
                output_factory=lambda _attempt, output=adapter_output: output,
            )
            outcome = CapabilityOutcome.create(
                attempt,
                task,
                adapter_output,
                failure_ref=None,
                budget_units=1,
            )
            entry = EvidenceLedgerEntry.create(plan, task, outcome, evidence)
            accepted = store.accept_capability_outcome(
                attempt,
                outcome,
                (entry,),
                (),
                _empty_settlement_authority(plan),
            )
            assert accepted == (attempt, outcome, (entry,), ())
            outcomes.append(outcome)
            entries.append(entry)

    stop = ExplorationStopRecord.create(
        plan,
        outcomes,
        reason="plan_exhausted",
        hard_budget_limit=None,
    )
    snapshot = ExecutionSnapshot.create(plan, stop, outcomes, entries, ())
    return snapshot, stop


def _empty_settlement_authority(plan):
    analysis = AnalysisContract(
        analysis_contract_id=f"analysis:{plan.plan_revision_id}:settlement",
        contract_version="test.v1",
        question_families=(),
        target_metric_refs=(),
        claim_intents=(),
        scope={"type": "test"},
        business_timezone="Asia/Shanghai",
        as_of="2026-07-18T00:00:00+08:00",
        resolved_windows=(),
        metric_bindings=(),
        dimension_bindings=(),
        dataset_requirements=(),
        capability_requirements=tuple(
            dict.fromkeys(task.capability_id for task in plan.capability_tasks)
        ),
        contract_gaps=(),
    )
    return CapabilitySettlementAuthority.create(
        run_id=plan.run_attempt_id,
        analysis_contract={
            **analysis.to_dict(),
            "contract_signature": analysis_contract_signature(analysis),
        },
        query_contracts=(),
        query_execution_records=(),
        rows_records=(),
        snapshot_records=(),
        completeness_records=(),
        capability_binding_records=(),
    )


def _compare_period_settlement_authority(plan, task):
    if task.capability_id != "compare_periods":
        raise AssertionError(task.capability_id)
    run_id = plan.run_attempt_id
    analysis_ref = f"analysis:{run_id}:capability-settlement"
    query_ref = f"query:{run_id}:channel-scan"
    snapshot_ref = f"snapshot:paid:{run_id}"
    source_rows = (
        {
            "window_id": "target_day",
            "period": "2026-06-19",
            "channel": "app",
            "amount": 12,
        },
        {
            "window_id": "previous_day",
            "period": "2026-06-18",
            "channel": "app",
            "amount": 10,
        },
    )
    content = verified_dimension_scan_context(
        rows=source_rows,
        required_fields=(
            "window_id",
            "window_role",
            "observation_key",
            "paid_amount",
            "channel",
        ),
        resolved_windows={
            "target_day": {
                "start_inclusive": "2026-06-19",
                "end_exclusive": "2026-06-20",
                "timezone": "Africa/Lagos",
            },
            "previous_day": {
                "start_inclusive": "2026-06-18",
                "end_exclusive": "2026-06-19",
                "timezone": "Africa/Lagos",
            },
        },
        query_ref=query_ref,
        snapshot_ref=snapshot_ref,
        analysis_contract_ref=analysis_ref,
    )
    resolver = content["evidence_resolver"]
    source_binding = resolver.resolve_capability_binding(
        content["binding_manifest_ref"]
    )
    source_query_record = resolver.resolve_query_execution(
        source_binding.validation_result_refs[0]
    )
    source_contract = source_query_record.contract
    snapshot_ref = source_contract.dataset_snapshot_refs[0]
    snapshot_record = resolver.resolve_snapshot(snapshot_ref)
    snapshot = snapshot_record.snapshot
    registry = content["runtime_registry"]
    capability = registry.capability_inputs("compare_periods")
    query_shape = registry.query_shape("daily_metric_baselines")
    metric_binding = source_contract.metric_bindings[0]
    required_fields = tuple(
        dict.fromkeys(
            (
                *query_shape["required_fields"],
                metric_binding.metric_id,
            )
        )
    )
    contract = QueryContract(
        query_contract_id=f"query:{run_id}:daily-metric-baselines",
        analysis_contract_ref=analysis_ref,
        query_intent="daily_metric_baselines",
        dataset_snapshot_refs=(snapshot_ref,),
        metric_bindings=(metric_binding,),
        dimension_bindings=(),
        window_refs=source_contract.window_refs,
        resolved_windows=source_contract.resolved_windows,
        filters=(),
        result_shape=ResultShape(
            required_fields=required_fields,
            unique_key=tuple(query_shape["unique_key"]),
            grain=tuple(query_shape["grain"]),
            required_window_ids=source_contract.window_refs,
            result_semantics=str(
                query_shape.get("result_semantics") or "complete_aggregate"
            ),
            dimension_presence_policy=str(
                query_shape["dimension_presence_policy"]
            ),
        ),
        completeness_assertions=ASSERTIONS,
        workload_class="interactive_aggregate",
        contract_signature="",
        query_role_ref=f"query-role:{run_id}:daily-metric-baselines",
    )
    contract = replace(
        contract,
        contract_signature=query_contract_signature(contract),
    )
    windows_by_id = {
        window.window_id: window for window in contract.resolved_windows
    }
    rows = tuple(
        {
            "window_id": source["window_id"],
            "window_role": windows_by_id[source["window_id"]].role,
            "observation_key": source["period"],
            metric_binding.metric_id: source["amount"],
        }
        for source in source_rows
    )
    execution_attempt_ref = f"attempt:{run_id}:daily-metric-baselines"
    query_hash = f"query-hash:{run_id}:daily-metric-baselines"
    audit_refs = query_audit_refs(
        query_hash,
        contract.contract_signature,
        contract.dataset_snapshot_refs,
        query_contract_ref=contract.query_contract_id,
        execution_attempt_ref=execution_attempt_ref,
        rows_content_hash=canonical_result_rows_hash(
            rows,
            contract.result_shape.unique_key,
        ),
    )
    result = QueryResultEnvelope(
        query_contract_ref=contract.query_contract_id,
        query_id=f"clickhouse:{run_id}:daily-metric-baselines",
        query_hash=query_hash,
        result_ref=audit_refs.result_ref,
        execution_status="succeeded",
        rows_ref=audit_refs.rows_ref,
        row_count=len(rows),
        completeness_report_ref=audit_refs.completeness_report_ref,
        rows=rows,
        observed_schema={field: "String" for field in required_fields},
        observed_windows=contract.window_refs,
        observed_grain=contract.result_shape.grain,
        source_snapshot_refs=(snapshot_ref,),
        execution_attempt_ref=execution_attempt_ref,
    )
    report = validate_query_result(
        contract,
        result,
        snapshot,
        release_resolver=content["release_resolver"],
    )
    writer = resolver._runtime_writer()
    writer.record_query_execution(
        contract,
        result,
        {snapshot_ref: snapshot},
    )
    writer.record_completeness(report)
    maximum_strength = str(capability["maximum_claim_strength"])
    capability_plan = CapabilityExecutionPlan(
        capability_id="compare_periods",
        capability_contract_ref=registry.capability_contract_ref("compare_periods"),
        required_input_slots=(
            CapabilityInputSlot(
                slot_id=contract.query_intent,
                query_contract_refs=(contract.query_contract_id,),
                required=True,
                accepted_completeness=tuple(
                    capability["minimum_readiness"]["accepted_completeness"]
                ),
                required_fields=contract.result_shape.required_fields,
                required_window_ids=tuple(capability["required_windows"]),
                validation_query_contract_refs=(),
            ),
        ),
        optional_input_slots=(),
        merge_strategy=str(capability.get("merge_strategy") or "by_query_family"),
        minimum_readiness=capability["minimum_readiness"],
        degradation_policy=capability["degradation_policy"],
        supported_evidence_types=tuple(capability["supported_evidence_types"]),
        maximum_claim_strength=maximum_strength,
        analysis_contract_ref=analysis_ref,
        supported_claim_types=tuple(capability["supported_claim_types"]),
        capability_contract_version=registry.contract_version,
        capability_contract_signature=registry.capability_contract_signature(
            "compare_periods"
        ),
        claim_strength_taxonomy_version=(registry.claim_strength_taxonomy_version),
        maximum_claim_strength_rank=registry.maximum_claim_strength_rank(
            maximum_strength
        ),
    )
    bound = bind_capability_inputs(
        capability_plan,
        results={contract.query_contract_id: result},
        reports={contract.query_contract_id: report},
        evidence_resolver=resolver,
        rows_loader=resolver.rows_loader,
        evidence_writer=resolver._runtime_writer(),
        runtime_registry=registry,
        release_resolver=content["release_resolver"],
    )
    binding = resolver.resolve_capability_binding(bound.binding_manifest_ref)
    snapshots = tuple(
        resolver.resolve_snapshot(ref) for ref in contract.dataset_snapshot_refs
    )
    analysis = AnalysisContract(
        analysis_contract_id=analysis_ref,
        contract_version=registry.contract_version,
        question_families=("custom_baseline_comparison",),
        target_metric_refs=tuple(
            item.contract_ref for item in contract.metric_bindings
        ),
        claim_intents=tuple(binding.supported_claim_types),
        scope={"type": "full_sample"},
        business_timezone=registry.business_timezone,
        as_of="2026-07-18T00:00:00+08:00",
        resolved_windows=contract.resolved_windows,
        metric_bindings=contract.metric_bindings,
        dimension_bindings=contract.dimension_bindings,
        dataset_requirements=tuple(
            dict.fromkeys(item.snapshot.dataset_id for item in snapshots)
        ),
        capability_requirements=("compare_periods",),
        contract_gaps=(),
    )
    authority = CapabilitySettlementAuthority.from_resolver(
        run_id=run_id,
        analysis_contract=analysis,
        query_contracts=(contract,),
        binding_refs=(binding.record_ref,),
        evidence_resolver=resolver,
    )
    return authority, binding


def _success_bundle(store, plan, task, evidence):
    adapter_output = CapabilityAdapterOutput.create(
        status="succeeded",
        output_payload={"task_id": task.task_id, "value": 1},
        evidence=(evidence,),
        affected_obligation_ids=task.supports_obligation_ids,
        limitation_refs=(),
        retryability="never",
    )
    attempt, adapter_output = _journaled_adapter_output(
        plan,
        task,
        attempt_journal=store.attempt_journal,
        output_factory=lambda _attempt: adapter_output,
    )
    outcome = CapabilityOutcome.create(
        attempt,
        task,
        adapter_output,
        failure_ref=None,
        budget_units=1,
    )
    entry = EvidenceLedgerEntry.create(plan, task, outcome, evidence)
    return attempt, outcome, (entry,), ()


def _dense_capability_adapter_output(plan, task, *, marker: str):
    evidence = CapabilityEvidence.create(
        evidence_ref=f"evidence:{task.task_id}:dense-projection",
        binding_record_ref=None,
        execution_state="available",
        evidence_kind="observed",
        data_contract_state="complete",
        supported_claim_kinds=("comparative_change",),
        evidence_strength="quantified_contribution",
        maximum_claim_strength="descriptive",
        observation_facts=(
            {
                "projection_kind": "claim_material_summary",
                "member_count": 200,
                "record_limit_per_direction": 5,
            },
        ),
        scope="full_sample",
        window_refs=plan.resolved_window_refs,
        dimension_path=("device_model",),
        limitation_refs=(),
        result_refs=(),
        completeness_report_refs=(),
        hierarchy_qualified=True,
    )
    members = tuple(
        {
            "member": f"member-{index:04d}",
            "baseline_value": 1000.0,
            "target_value": float(1000 + index + 1),
            "delta": float(index + 1),
        }
        for index in range(200)
    )
    return CapabilityAdapterOutput.create(
        status="succeeded",
        output_payload={
            "projection_marker": marker,
            "typed_payload": {
                "dimension_breakdowns": (
                    {
                        "dimension_id": "device_model",
                        "members": members,
                    },
                ),
            },
        },
        evidence=(evidence,),
        affected_obligation_ids=task.supports_obligation_ids,
        limitation_refs=(),
        retryability="never",
    )


def _capability_evidence_with_observation(
    evidence: CapabilityEvidence,
    *,
    observation_facts,
) -> CapabilityEvidence:
    return CapabilityEvidence.create(
        evidence_ref=evidence.evidence_ref,
        binding_record_ref=evidence.binding_record_ref,
        execution_state=evidence.execution_state,
        evidence_kind=evidence.evidence_kind,
        data_contract_state=evidence.data_contract_state,
        supported_claim_kinds=evidence.supported_claim_kinds,
        evidence_strength=evidence.evidence_strength,
        maximum_claim_strength=evidence.maximum_claim_strength,
        observation_facts=observation_facts,
        scope=evidence.scope,
        window_refs=evidence.window_refs,
        dimension_path=evidence.dimension_path,
        limitation_refs=evidence.limitation_refs,
        result_refs=evidence.result_refs,
        completeness_report_refs=evidence.completeness_report_refs,
        hierarchy_qualified=evidence.hierarchy_qualified,
    )


def _technical_failure_adapter_output(task, *, detail: str):
    return CapabilityAdapterOutput.create(
        status="technical_failed",
        output_payload={"task_id": task.task_id, "stage": "query"},
        evidence=(),
        affected_obligation_ids=task.supports_obligation_ids,
        limitation_refs=(f"limitation:{task.task_id}",),
        retryability="same_input",
        failure=CapabilityFailure.create(
            layer="query",
            kind="query_transport_failed",
            scope="task",
            affected_refs=(task.task_id, *task.supports_obligation_ids),
            integrity_level="task",
            retryability="same_input",
            user_actionable=False,
            business_boundary="query_result_unavailable",
            technical_detail_ref=f"technical-detail:{task.task_id}:{detail}",
        ),
    )


def _dependency_bundle(store, plan, task):
    evidence = CapabilityEvidence.create(
        evidence_ref=f"evidence:{task.task_id}:dependency",
        binding_record_ref=None,
        execution_state="available",
        evidence_kind="boundary",
        data_contract_state="complete",
        supported_claim_kinds=(),
        evidence_strength="verified",
        maximum_claim_strength="trust_boundary",
        observation_facts=({"fact_ref": "dependency_ready", "value": True},),
        scope="full_sample",
        window_refs=plan.resolved_window_refs,
        dimension_path=(),
        limitation_refs=(),
        result_refs=(),
        completeness_report_refs=(),
        hierarchy_qualified=False,
    )
    return _success_bundle(store, plan, task, evidence)


def _binding_bundle(store, plan, task, binding):
    evidence = CapabilityEvidence.create(
        evidence_ref=f"evidence:{task.task_id}:binding",
        binding_record_ref=binding.record_ref,
        execution_state="available",
        evidence_kind=publication_evidence_kind(binding.supported_evidence_types[0]),
        data_contract_state="complete",
        supported_claim_kinds=binding.supported_claim_types,
        evidence_strength="high",
        maximum_claim_strength=binding.maximum_claim_strength,
        observation_facts=({"fact_ref": "target_value", "value": 12},),
        scope="full_sample",
        window_refs=plan.resolved_window_refs,
        dimension_path=(),
        limitation_refs=(),
        result_refs=(*binding.result_refs, *binding.validation_result_refs),
        completeness_report_refs=(
            *binding.completeness_report_refs,
            *binding.validation_completeness_report_refs,
        ),
        hierarchy_qualified=False,
    )
    return _success_bundle(store, plan, task, evidence)


def _settlement_transition(
    plan,
    *,
    parent_transition_id: str,
    input_payload: dict,
    output_payload: dict,
) -> DurableTransition:
    return DurableTransition.create(
        node_name="execute_capability_dag",
        parent_transition_id=parent_transition_id,
        run_attempt_id=plan.run_attempt_id,
        intent_revision_id=plan.intent_revision_id,
        decision_ledger_position=0,
        input_digest=canonical_digest(input_payload),
        output_digest=canonical_digest(output_payload),
        execution_attempt=1,
        provider_ref="waje-capability-runtime",
        model_ref="deterministic-capability-dag.v1",
        status="succeeded",
        acceptance_state="accepted",
        next_transition="phase03_evidence_bound",
    )


def _accept_durable_call(
    store: PostgresConversationStore,
    spec: DurableCallSpec,
) -> str:
    claim = store.attempt_journal.claim(spec)
    completion = store.attempt_journal.succeed(
        claim.attempt,
        {"contract_test": True},
    )
    assert completion.acceptance is not None
    return completion.acceptance.accepted_attempt_ref


def _settlement_bundle(store: PostgresConversationStore):
    plan, plan_transition = _accepted_plan(store)
    query_spec = DurableCallSpec.create(
        run_attempt_id=plan.run_attempt_id,
        intent_revision_id=plan.intent_revision_id,
        plan_revision_id=plan.plan_revision_id,
        task_id=plan.capability_tasks[0].task_id,
        stage_name="execute_capability_dag",
        call_kind="query",
        operation_name="phase03-settlement-query",
        input_ref=f"query-contract:{plan.plan_revision_id}",
        input_payload={"plan_revision_id": plan.plan_revision_id},
    )
    query_attempt_ref = _accept_durable_call(store, query_spec)
    snapshot, stop = _persist_execution_closure(store, plan)
    input_payload, output_payload = capability_execution_transition_payloads(
        plan,
        snapshot,
        stop,
    )
    transition = _settlement_transition(
        plan,
        parent_transition_id=plan_transition.transition_id,
        input_payload=input_payload,
        output_payload=output_payload,
    )
    accepted_attempt_refs = tuple(
        sorted(
            {
                query_attempt_ref,
                *(
                    str(row[0])
                    for row in store._fetchall(
                        """
                        SELECT acceptance.accepted_attempt_ref
                        FROM waje_runtime.durable_call_acceptances acceptance
                        JOIN waje_runtime.durable_call_attempts attempt
                          ON attempt.run_attempt_id = acceptance.run_attempt_id
                         AND attempt.attempt_ref = acceptance.accepted_attempt_ref
                        WHERE acceptance.run_attempt_id = %(run_attempt_id)s
                          AND attempt.call_kind = 'capability'
                        """,
                        {"run_attempt_id": plan.run_attempt_id},
                    )
                ),
            }
        )
    )
    return (
        plan,
        snapshot,
        stop,
        transition,
        input_payload,
        output_payload,
        accepted_attempt_refs,
    )


def _settlement_counts(
    store: PostgresConversationStore,
    *,
    run_id: str,
) -> tuple[int, int, int]:
    row = store._fetchone(
        """
        SELECT
          (SELECT count(*)
           FROM waje_runtime.exploration_stop_records
           WHERE run_attempt_id = %(run_id)s),
          (SELECT count(*)
           FROM waje_runtime.capability_execution_snapshots
           WHERE run_attempt_id = %(run_id)s),
          (SELECT count(*)
           FROM waje_runtime.workflow_transition_attempts
           WHERE run_attempt_id = %(run_id)s
             AND node_name = 'execute_capability_dag'
             AND acceptance_state = 'accepted')
        """,
        {"run_id": run_id},
    )
    return tuple(row)


def _lifecycle_count(
    store: PostgresConversationStore,
    *,
    run_id: str,
) -> int:
    row = store._fetchone(
        """
        SELECT count(*)
        FROM waje_runtime.run_lifecycle_state_revisions
        WHERE run_attempt_id = %(run_id)s
        """,
        {"run_id": run_id},
    )
    return int(row[0])


class Phase03PostgresSettlementIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (os.getenv("WAJE_RUNTIME_DATABASE_URL") or os.getenv("DATABASE_URL")):
            raise unittest.SkipTest("runtime PostgreSQL is not configured")
        cls.store = PostgresConversationStore.from_env()
        cls.store.apply_schema()

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "store"):
            cls.store.connection.close()

    def setUp(self):
        self.store.connection.rollback()

    def tearDown(self):
        self.store.connection.rollback()

    def test_session_lock_crash_close_allows_monotonic_incomplete_retry(self):
        import psycopg

        dsn = os.getenv("WAJE_RUNTIME_DATABASE_URL") or os.getenv("DATABASE_URL")
        first_store = PostgresConversationStore(psycopg.connect(dsn))
        first_store.apply_schema()
        plan, _ = _accepted_plan(first_store)
        task = plan.capability_tasks[0]
        spec = _capability_call_spec(plan, task)
        abandoned = first_store.attempt_journal.claim(spec)
        self.assertEqual(abandoned.attempt.attempt_number, 1)
        first_store.connection.close()

        second_store = PostgresConversationStore(psycopg.connect(dsn))
        try:
            retry = second_store.attempt_journal.claim(spec)
            self.assertEqual(retry.attempt.attempt_number, 2)
            self.assertEqual(
                retry.attempt.retry_reason,
                "previous_attempt_incomplete",
            )
            second_store.attempt_journal.fail(
                retry.attempt,
                failure_code="test_cleanup",
            )
        finally:
            second_store.connection.close()

    def test_in_flight_success_after_run_cancellation_is_persisted_orphan(self):
        import psycopg

        dsn = os.getenv("WAJE_RUNTIME_DATABASE_URL") or os.getenv("DATABASE_URL")
        provider_store = PostgresConversationStore(psycopg.connect(dsn))
        cancellation_store = PostgresConversationStore(psycopg.connect(dsn))
        try:
            plan, _ = _accepted_plan(provider_store)
            spec = _capability_call_spec(plan, plan.capability_tasks[0])
            claim = provider_store.attempt_journal.claim(spec)

            cancellation_store.cancel_run_attempt(
                run_attempt_id=plan.run_attempt_id,
                reason_ref="test:provider-in-flight",
            )
            completion = provider_store.attempt_journal.succeed(
                claim.attempt,
                {"output": {"message": "provider completed"}},
            )

            self.assertEqual(completion.disposition, "orphaned")
            self.assertIsNone(completion.acceptance)
            self.assertIsNone(completion.accepted_attempt)
            persisted = provider_store._fetchone(
                """
                SELECT event.success_disposition,
                       event.output_payload,
                       acceptance.accepted_attempt_ref
                FROM waje_runtime.durable_call_attempt_events event
                LEFT JOIN waje_runtime.durable_call_acceptances acceptance
                  ON acceptance.run_attempt_id = event.run_attempt_id
                 AND acceptance.accepted_attempt_ref = event.attempt_ref
                WHERE event.attempt_ref = %(attempt_ref)s
                  AND event.event_sequence = 3
                """,
                {"attempt_ref": claim.attempt.attempt_ref},
            )
            self.assertEqual(persisted[0], "orphaned")
            self.assertEqual(
                persisted[1], {"output": {"message": "provider completed"}}
            )
            self.assertIsNone(persisted[2])
        finally:
            provider_store.connection.close()
            cancellation_store.connection.close()

    def test_dense_capability_output_remains_recoverable_after_store_restart(self):
        import psycopg

        dsn = os.getenv("WAJE_RUNTIME_DATABASE_URL") or os.getenv("DATABASE_URL")
        producer = PostgresConversationStore(psycopg.connect(dsn))
        try:
            plan, _ = _accepted_plan(producer)
            task = next(
                item for item in plan.capability_tasks if not item.dependency_task_ids
            )
            expected_output = _dense_capability_adapter_output(
                plan,
                task,
                marker="accepted-before-restart",
            )
            attempt, accepted_output = _journaled_adapter_output(
                plan,
                task,
                attempt_journal=producer.attempt_journal,
                output_factory=lambda _attempt: expected_output,
            )
            outcome = CapabilityOutcome.create(
                attempt,
                task,
                accepted_output,
                failure_ref=None,
                budget_units=task.declared_budget_units,
            )
            entry = EvidenceLedgerEntry.create(
                plan,
                task,
                outcome,
                accepted_output.evidence[0],
            )
            producer.accept_capability_outcome(
                attempt,
                outcome,
                (entry,),
                (),
                _empty_settlement_authority(plan),
            )
        finally:
            producer.connection.close()

        restarted = PostgresConversationStore(psycopg.connect(dsn))
        try:
            loaded = restarted.load_capability_outcome(
                plan.plan_revision_id,
                task.task_id,
            )
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded, (attempt, outcome, (entry,), ()))

            def unexpected_execution(_attempt):
                raise AssertionError("capability replay unexpectedly executed")

            replayed_attempt, replayed_output = _journaled_adapter_output(
                plan,
                task,
                attempt_journal=restarted.attempt_journal,
                output_factory=unexpected_execution,
            )
            members = replayed_output.output_payload["typed_payload"][
                "dimension_breakdowns"
            ][0]["members"]
            self.assertEqual(replayed_attempt, attempt)
            self.assertEqual(replayed_output, accepted_output)
            self.assertEqual(len(members), 200)
            self.assertEqual(members[-1]["member"], "member-0199")
        finally:
            restarted.connection.close()

    def test_evidence_local_limitation_does_not_become_outcome_limitation(self):
        plan, _ = _accepted_plan(self.store)
        task = next(
            item for item in plan.capability_tasks if not item.dependency_task_ids
        )
        base_output = _dense_capability_adapter_output(
            plan,
            task,
            marker="evidence-local-limitation",
        )
        base_evidence = base_output.evidence[0]
        evidence = CapabilityEvidence.create(
            evidence_ref=base_evidence.evidence_ref,
            binding_record_ref=base_evidence.binding_record_ref,
            execution_state=base_evidence.execution_state,
            evidence_kind=base_evidence.evidence_kind,
            data_contract_state="partial",
            supported_claim_kinds=base_evidence.supported_claim_kinds,
            evidence_strength=base_evidence.evidence_strength,
            maximum_claim_strength=base_evidence.maximum_claim_strength,
            observation_facts=base_evidence.observation_facts,
            scope=base_evidence.scope,
            window_refs=base_evidence.window_refs,
            dimension_path=("region",),
            limitation_refs=("limitation:region:sparse",),
            result_refs=base_evidence.result_refs,
            completeness_report_refs=base_evidence.completeness_report_refs,
            hierarchy_qualified=base_evidence.hierarchy_qualified,
        )
        adapter_output = CapabilityAdapterOutput.create(
            status=base_output.status,
            output_payload=base_output.output_payload,
            evidence=(evidence,),
            affected_obligation_ids=base_output.affected_obligation_ids,
            limitation_refs=(),
            retryability=base_output.retryability,
        )
        attempt, accepted_output = _journaled_adapter_output(
            plan,
            task,
            attempt_journal=self.store.attempt_journal,
            output_factory=lambda _attempt: adapter_output,
        )
        outcome = CapabilityOutcome.create(
            attempt,
            task,
            accepted_output,
            failure_ref=None,
            budget_units=task.declared_budget_units,
        )
        entry = EvidenceLedgerEntry.create(plan, task, outcome, evidence)

        self.assertEqual(outcome.limitation_refs, ())
        self.assertEqual(entry.limitation_refs, ("limitation:region:sparse",))
        self.store.accept_capability_outcome(
            attempt,
            outcome,
            (entry,),
            (),
            _empty_settlement_authority(plan),
        )

        self.assertEqual(
            self.store.load_capability_outcome(
                plan.plan_revision_id,
                task.task_id,
            ),
            (attempt, outcome, (entry,), ()),
        )

    def test_capability_outcome_rejects_mismatched_durable_output(self):
        plan, _ = _accepted_plan(self.store)
        task = next(
            item for item in plan.capability_tasks if not item.dependency_task_ids
        )
        expected_output = _dense_capability_adapter_output(
            plan,
            task,
            marker="durably-accepted",
        )
        attempt, accepted_output = _journaled_adapter_output(
            plan,
            task,
            attempt_journal=self.store.attempt_journal,
            output_factory=lambda _attempt: expected_output,
        )
        mismatched_output = _dense_capability_adapter_output(
            plan,
            task,
            marker="tampered-outcome",
        )
        mismatched_outcome = CapabilityOutcome.create(
            attempt,
            task,
            mismatched_output,
            failure_ref=None,
            budget_units=task.declared_budget_units,
        )
        mismatched_entry = EvidenceLedgerEntry.create(
            plan,
            task,
            mismatched_outcome,
            mismatched_output.evidence[0],
        )

        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "capability_call_acceptance_invalid",
        ):
            self.store.accept_capability_outcome(
                attempt,
                mismatched_outcome,
                (mismatched_entry,),
                (),
                _empty_settlement_authority(plan),
            )

    def test_capability_outcome_rejects_changed_observation_with_same_evidence_ref(
        self,
    ):
        plan, _ = _accepted_plan(self.store)
        task = next(
            item for item in plan.capability_tasks if not item.dependency_task_ids
        )
        expected_output = _dense_capability_adapter_output(
            plan,
            task,
            marker="same-output-payload",
        )
        attempt, accepted_output = _journaled_adapter_output(
            plan,
            task,
            attempt_journal=self.store.attempt_journal,
            output_factory=lambda _attempt: expected_output,
        )
        changed_evidence = _capability_evidence_with_observation(
            accepted_output.evidence[0],
            observation_facts=(
                {
                    "projection_kind": "claim_material_summary",
                    "member_count": 199,
                    "record_limit_per_direction": 5,
                },
            ),
        )
        mismatched_output = CapabilityAdapterOutput.create(
            status=accepted_output.status,
            output_payload=accepted_output.output_payload,
            evidence=(changed_evidence,),
            affected_obligation_ids=accepted_output.affected_obligation_ids,
            limitation_refs=accepted_output.limitation_refs,
            retryability=accepted_output.retryability,
        )
        mismatched_outcome = CapabilityOutcome.create(
            attempt,
            task,
            mismatched_output,
            failure_ref=None,
            budget_units=task.declared_budget_units,
        )
        mismatched_entry = EvidenceLedgerEntry.create(
            plan,
            task,
            mismatched_outcome,
            changed_evidence,
        )

        self.assertEqual(
            changed_evidence.evidence_ref,
            accepted_output.evidence[0].evidence_ref,
        )
        self.assertEqual(
            mismatched_outcome,
            CapabilityOutcome.create(
                attempt,
                task,
                accepted_output,
                failure_ref=None,
                budget_units=task.declared_budget_units,
            ),
        )
        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "capability_call_acceptance_invalid",
        ):
            self.store.accept_capability_outcome(
                attempt,
                mismatched_outcome,
                (mismatched_entry,),
                (),
                _empty_settlement_authority(plan),
            )

    def test_capability_outcome_rejects_changed_failure_detail(self):
        plan, _ = _accepted_plan(self.store)
        task = next(
            item for item in plan.capability_tasks if not item.dependency_task_ids
        )
        expected_output = _technical_failure_adapter_output(
            task,
            detail="durably-accepted",
        )
        attempt, accepted_output = _journaled_adapter_output(
            plan,
            task,
            attempt_journal=self.store.attempt_journal,
            output_factory=lambda _attempt: expected_output,
        )
        changed_output = _technical_failure_adapter_output(
            task,
            detail="changed-after-provider",
        )
        changed_failure = FailureRecord.create(attempt, changed_output.failure)
        changed_outcome = CapabilityOutcome.create(
            attempt,
            task,
            changed_output,
            failure_ref=changed_failure.failure_ref,
            budget_units=task.declared_budget_units,
        )

        self.assertEqual(changed_output.output_digest, accepted_output.output_digest)
        self.assertNotEqual(
            changed_failure.technical_detail_ref,
            (
                FailureRecord.create(
                    attempt, accepted_output.failure
                ).technical_detail_ref
            ),
        )
        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "capability_call_acceptance_invalid",
        ):
            self.store.accept_capability_outcome(
                attempt,
                changed_outcome,
                (),
                (changed_failure,),
                _empty_settlement_authority(plan),
            )

    def test_boundary_evidence_kind_persists_and_unknown_kind_is_rejected(self):
        migration = self.store._fetchone(
            """
            SELECT migration_digest
            FROM waje_runtime.schema_migrations
            WHERE migration_id = %(migration_id)s
            """,
            {"migration_id": SINGLE_AUTHORITY_MIGRATION_ID},
        )
        self.assertEqual(
            migration,
            (SINGLE_AUTHORITY_MIGRATION_DIGEST,),
        )

        plan, _ = _accepted_plan(self.store)
        snapshot, _ = _persist_execution_closure(
            self.store,
            plan,
            evidence_kind="boundary",
        )
        persisted_kinds = self.store._fetchall(
            """
            SELECT evidence_kind
            FROM waje_runtime.capability_evidence_ledger_entries
            WHERE plan_revision_id = %(plan_revision_id)s
            ORDER BY entry_ref
            """,
            {"plan_revision_id": plan.plan_revision_id},
        )

        self.assertTrue(persisted_kinds)
        self.assertEqual(
            {str(row[0]) for row in persisted_kinds},
            {"boundary"},
        )

        source_entry_ref = snapshot.evidence_entry_refs[0]
        with self.assertRaises(CheckViolation):
            self.store._execute(
                """
                INSERT INTO waje_runtime.capability_evidence_ledger_entries(
                  entry_ref, run_attempt_id, authority_context_ref,
                  plan_revision_id, task_id, outcome_ref, evidence_ref,
                  binding_record_ref, execution_state, evidence_kind,
                  data_contract_state, maximum_claim_strength,
                  result_membership_digest,
                  completeness_membership_digest, content_digest, payload
                )
                SELECT
                  entry_ref || ':invalid-kind', run_attempt_id,
                  authority_context_ref, plan_revision_id, task_id,
                  outcome_ref, evidence_ref || ':invalid-kind',
                  binding_record_ref, execution_state, 'invalid-kind',
                  data_contract_state, maximum_claim_strength,
                  result_membership_digest,
                  completeness_membership_digest, content_digest, payload
                FROM waje_runtime.capability_evidence_ledger_entries
                WHERE entry_ref = %(source_entry_ref)s
                """,
                {"source_entry_ref": source_entry_ref},
            )
        self.store.connection.rollback()

    def test_capability_outcome_persists_exact_binding_authority_before_ledger(
        self,
    ):
        plan, _ = _accepted_plan(self.store)
        dependency = next(
            task
            for task in plan.capability_tasks
            if task.capability_id == "data_quality_profile"
        )
        task = next(
            task
            for task in plan.capability_tasks
            if task.capability_id == "compare_periods"
        )
        self.store.accept_capability_outcome(
            *_dependency_bundle(self.store, plan, dependency),
            _empty_settlement_authority(plan),
        )
        authority, binding = _compare_period_settlement_authority(plan, task)
        bundle = _binding_bundle(self.store, plan, task, binding)
        collisions = []
        insert_immutable = self.store._insert_immutable

        def capture_insert(statement, params, *, collision):
            collisions.append(collision)
            return insert_immutable(
                statement,
                params,
                collision=collision,
            )

        with patch.object(
            self.store,
            "_insert_immutable",
            side_effect=capture_insert,
        ):
            accepted = self.store.accept_capability_outcome(
                *bundle,
                authority,
            )

        self.assertEqual(accepted, bundle)
        self.assertLess(
            collisions.index("capability_binding_record"),
            collisions.index("capability_evidence_ledger_entry"),
        )
        persisted = self.store._fetchone(
            """
            SELECT
              EXISTS(
                SELECT 1 FROM waje_runtime.analysis_contracts
                WHERE analysis_contract_id = %(analysis_contract_id)s
              ),
              EXISTS(
                SELECT 1 FROM waje_runtime.query_contracts
                WHERE query_contract_id = %(query_contract_id)s
              ),
              EXISTS(
                SELECT 1 FROM waje_runtime.query_runs
                WHERE result_ref = %(result_ref)s
              ),
              EXISTS(
                SELECT 1 FROM waje_runtime.capability_binding_authority
                WHERE record_ref = %(binding_ref)s
              ),
              EXISTS(
                SELECT 1
                FROM waje_runtime.capability_evidence_ledger_entries
                WHERE entry_ref = %(entry_ref)s
              )
            """,
            {
                "analysis_contract_id": authority.analysis_contract[
                    "analysis_contract_id"
                ],
                "query_contract_id": authority.query_contracts[0].query_contract_id,
                "result_ref": authority.query_execution_records[0].result_ref,
                "binding_ref": binding.record_ref,
                "entry_ref": bundle[2][0].entry_ref,
            },
        )
        self.assertEqual(persisted, (True, True, True, True, True))
        resolver = self.store.runtime_evidence_resolver()
        self.assertEqual(
            resolver.resolve_capability_binding(binding.record_ref),
            binding,
        )
        for record in authority.query_execution_records:
            self.assertEqual(
                resolver.resolve_query_execution_record(record.record_ref),
                record,
            )
        for record in authority.rows_records:
            self.assertEqual(
                resolver.resolve_rows_record(record.record_ref),
                record,
            )
        for record in authority.snapshot_records:
            self.assertEqual(
                resolver.resolve_snapshot(record.snapshot_ref),
                record,
            )
        for record in authority.completeness_records:
            self.assertEqual(
                resolver.resolve_completeness(record.record_ref),
                record,
            )

        replayed = self.store.accept_capability_outcome(*bundle, authority)
        self.assertEqual(replayed, bundle)
        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "capability_outcome_bundle_invalid",
        ):
            self.store.accept_capability_outcome(
                *bundle,
                authority.for_binding_refs(()),
            )
        with self.assertRaises(EvidenceIntegrityError):
            self.store.accept_capability_outcome(
                *bundle,
                replace(
                    authority,
                    query_contracts=(
                        *authority.query_contracts,
                        authority.query_contracts[0],
                    ),
                ),
            )

    def test_ledger_failure_rolls_back_entire_binding_authority_closure(self):
        plan, _ = _accepted_plan(self.store)
        dependency = next(
            task
            for task in plan.capability_tasks
            if task.capability_id == "data_quality_profile"
        )
        task = next(
            task
            for task in plan.capability_tasks
            if task.capability_id == "compare_periods"
        )
        self.store.accept_capability_outcome(
            *_dependency_bundle(self.store, plan, dependency),
            _empty_settlement_authority(plan),
        )
        authority, binding = _compare_period_settlement_authority(plan, task)
        bundle = _binding_bundle(self.store, plan, task, binding)
        insert_immutable = self.store._insert_immutable

        def fail_ledger(statement, params, *, collision):
            if collision == "capability_evidence_ledger_entry":
                raise RuntimeError("injected_ledger_insert_failure")
            return insert_immutable(
                statement,
                params,
                collision=collision,
            )

        with patch.object(
            self.store,
            "_insert_immutable",
            side_effect=fail_ledger,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "injected_ledger_insert_failure",
            ):
                self.store.accept_capability_outcome(
                    *bundle,
                    authority,
                )

        query_record = authority.query_execution_records[0]
        rows_record = authority.rows_records[0]
        snapshot_record = authority.snapshot_records[0]
        completeness_record = authority.completeness_records[0]
        persisted = self.store._fetchone(
            """
            SELECT
              EXISTS(
                SELECT 1 FROM waje_runtime.analysis_contracts
                WHERE analysis_contract_id = %(analysis_contract_id)s
              ),
              EXISTS(
                SELECT 1 FROM waje_runtime.query_contracts
                WHERE query_contract_id = %(query_contract_id)s
              ),
              EXISTS(
                SELECT 1 FROM waje_runtime.query_runs
                WHERE result_ref = %(result_ref)s
              ),
              EXISTS(
                SELECT 1 FROM waje_runtime.snapshot_authority
                WHERE record_ref = %(snapshot_record_ref)s
              ),
              EXISTS(
                SELECT 1 FROM waje_runtime.rows_metadata_authority
                WHERE record_ref = %(rows_record_ref)s
              ),
              EXISTS(
                SELECT 1 FROM waje_runtime.query_execution_authority
                WHERE record_ref = %(query_record_ref)s
              ),
              EXISTS(
                SELECT 1 FROM waje_runtime.query_completeness_reports
                WHERE record_ref = %(completeness_record_ref)s
              ),
              EXISTS(
                SELECT 1 FROM waje_runtime.capability_binding_authority
                WHERE record_ref = %(binding_ref)s
              ),
              EXISTS(
                SELECT 1 FROM waje_runtime.capability_task_attempts
                WHERE attempt_id = %(attempt_id)s
              ),
              EXISTS(
                SELECT 1 FROM waje_runtime.capability_outcomes
                WHERE outcome_ref = %(outcome_ref)s
              ),
              EXISTS(
                SELECT 1
                FROM waje_runtime.capability_evidence_ledger_entries
                WHERE entry_ref = %(entry_ref)s
              )
            """,
            {
                "analysis_contract_id": authority.analysis_contract[
                    "analysis_contract_id"
                ],
                "query_contract_id": authority.query_contracts[0].query_contract_id,
                "result_ref": query_record.result_ref,
                "snapshot_record_ref": snapshot_record.record_ref,
                "rows_record_ref": rows_record.record_ref,
                "query_record_ref": query_record.record_ref,
                "completeness_record_ref": completeness_record.record_ref,
                "binding_ref": binding.record_ref,
                "attempt_id": bundle[0].attempt_id,
                "outcome_ref": bundle[1].outcome_ref,
                "entry_ref": bundle[2][0].entry_ref,
            },
        )
        self.assertEqual(persisted, (False,) * 11)

    def test_transition_insert_failure_rolls_back_stop_and_snapshot(self):
        (
            plan,
            snapshot,
            stop,
            transition,
            input_payload,
            output_payload,
            accepted_attempt_refs,
        ) = _settlement_bundle(self.store)

        with patch.object(
            self.store,
            "_save_transition_attempt_locked",
            side_effect=RuntimeError("injected_transition_insert_failure"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "injected_transition_insert_failure",
            ):
                self.store.accept_execution_settlement(
                    snapshot,
                    stop,
                    transition,
                    input_payload,
                    output_payload,
                    accepted_attempt_refs,
                )

        self.assertEqual(
            _settlement_counts(self.store, run_id=plan.run_attempt_id),
            (0, 0, 0),
        )
        self.assertIsNone(self.store.load_execution_snapshot(plan.plan_revision_id))
        self.assertNotIn(
            "execution_result_refs",
            self.store.get_run_request(plan.run_attempt_id),
        )

    def test_settlement_seals_complete_query_and_capability_attempt_set(self):
        (
            plan,
            snapshot,
            stop,
            transition,
            input_payload,
            output_payload,
            accepted_attempt_refs,
        ) = _settlement_bundle(self.store)

        self.store.accept_execution_settlement(
            snapshot,
            stop,
            transition,
            input_payload,
            output_payload,
            accepted_attempt_refs,
        )

        call_kinds = {
            str(row[0])
            for row in self.store._fetchall(
                """
                SELECT DISTINCT attempt.call_kind
                FROM waje_runtime.durable_call_attempts attempt
                WHERE attempt.attempt_ref = ANY(%(attempt_refs)s)
                """,
                {"attempt_refs": list(accepted_attempt_refs)},
            )
        }
        self.assertEqual(call_kinds, {"query", "capability"})
        self.assertEqual(
            self.store.attempt_journal.load_stage_attempt_refs(
                run_attempt_id=plan.run_attempt_id,
                transition_attempt_id=transition.attempt_id,
                stage_name=transition.node_name,
            ),
            accepted_attempt_refs,
        )
        self.assertEqual(
            self.store.load_execution_snapshot(plan.plan_revision_id),
            snapshot,
        )
        bundles = tuple(
            bundle
            for task in plan.capability_tasks
            if (
                bundle := self.store.load_capability_outcome(
                    plan.plan_revision_id,
                    task.task_id,
                )
            )
            is not None
        )
        execution_result = AuthoritativeExecutionResult.from_records(
            plan_revision=plan,
            execution_snapshot=snapshot,
            exploration_stop_record=stop,
            capability_outcome_bundles=bundles,
            durable_transition=transition,
        )
        request = self.store.get_run_request(plan.run_attempt_id)
        self.assertEqual(
            request["execution_result_refs"],
            {
                "schema_version": "single-authority-phase03.v1",
                "authoritative_execution_result_ref": (
                    execution_result.authoritative_execution_result_ref
                ),
                "intent_revision_id": plan.intent_revision_id,
                "authority_context_ref": plan.authority_context_ref,
                "plan_revision_id": plan.plan_revision_id,
                "execution_snapshot_ref": snapshot.execution_snapshot_ref,
                "stop_ref": stop.stop_ref,
                "accepted_transition_id": transition.transition_id,
            },
        )

    def test_settlement_atomically_closes_execution_with_partial_evidence(self):
        (
            plan,
            snapshot,
            stop,
            transition,
            input_payload,
            output_payload,
            accepted_attempt_refs,
        ) = _settlement_bundle(self.store)
        initial = self.store.latest_lifecycle_state(plan.run_attempt_id)
        self.assertIsNotNone(initial)
        self.assertEqual(initial.execution_state, "running")
        self.assertEqual(initial.interaction_state, "active")
        self.assertEqual(initial.evidence_state, "not_started")

        self.store.accept_execution_settlement(
            snapshot,
            stop,
            transition,
            input_payload,
            output_payload,
            accepted_attempt_refs,
        )

        settled = self.store.latest_lifecycle_state(plan.run_attempt_id)
        self.assertEqual(settled.state_revision, initial.state_revision + 1)
        self.assertEqual(settled.prior_state_digest, initial.content_digest)
        self.assertEqual(settled.execution_state, "complete")
        self.assertEqual(settled.interaction_state, "active")
        self.assertEqual(settled.evidence_state, "partial")
        self.assertEqual(settled.publication_state, "not_ready")

    def test_settlement_resumes_a_user_resolved_waiting_lifecycle(self):
        (
            plan,
            snapshot,
            stop,
            transition,
            input_payload,
            output_payload,
            accepted_attempt_refs,
        ) = _settlement_bundle(self.store)
        current = self.store.latest_lifecycle_state(plan.run_attempt_id)
        waiting = current.transition(execution_state="waiting")
        self.store.append_lifecycle_state(waiting)

        self.store.accept_execution_settlement(
            snapshot,
            stop,
            transition,
            input_payload,
            output_payload,
            accepted_attempt_refs,
        )

        settled = self.store.latest_lifecycle_state(plan.run_attempt_id)
        self.assertEqual(settled.state_revision, waiting.state_revision + 1)
        self.assertEqual(settled.prior_state_digest, waiting.content_digest)
        self.assertEqual(settled.execution_state, "complete")
        self.assertEqual(settled.interaction_state, "active")
        self.assertEqual(settled.evidence_state, "partial")

    def test_lifecycle_failure_rolls_back_the_complete_execution_settlement(self):
        (
            plan,
            snapshot,
            stop,
            transition,
            input_payload,
            output_payload,
            accepted_attempt_refs,
        ) = _settlement_bundle(self.store)
        initial_lifecycle_count = _lifecycle_count(
            self.store,
            run_id=plan.run_attempt_id,
        )

        with patch.object(
            self.store,
            "_append_lifecycle_state_locked",
            side_effect=RuntimeError("injected_lifecycle_insert_failure"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "injected_lifecycle_insert_failure",
            ):
                self.store.accept_execution_settlement(
                    snapshot,
                    stop,
                    transition,
                    input_payload,
                    output_payload,
                    accepted_attempt_refs,
                )

        self.assertEqual(
            _settlement_counts(self.store, run_id=plan.run_attempt_id),
            (0, 0, 0),
        )
        self.assertEqual(
            _lifecycle_count(self.store, run_id=plan.run_attempt_id),
            initial_lifecycle_count,
        )
        self.assertNotIn(
            "execution_result_refs",
            self.store.get_run_request(plan.run_attempt_id),
        )

    def test_cancelled_or_superseded_run_cannot_accept_execution_settlement(self):
        for field, value in (
            ("cancellation_state", "cancelled"),
            ("supersession_state", "superseded"),
        ):
            with self.subTest(field=field):
                (
                    plan,
                    snapshot,
                    stop,
                    transition,
                    input_payload,
                    output_payload,
                    accepted_attempt_refs,
                ) = _settlement_bundle(self.store)
                current = self.store.latest_lifecycle_state(plan.run_attempt_id)
                self.store.append_lifecycle_state(current.transition(**{field: value}))

                with self.assertRaisesRegex(
                    EvidenceIntegrityError,
                    "capability_execution_lifecycle_not_active",
                ):
                    self.store.accept_execution_settlement(
                        snapshot,
                        stop,
                        transition,
                        input_payload,
                        output_payload,
                        accepted_attempt_refs,
                    )

                self.assertEqual(
                    _settlement_counts(self.store, run_id=plan.run_attempt_id),
                    (0, 0, 0),
                )

    def test_settlement_rejects_missing_query_attempt_from_stage_closure(self):
        (
            plan,
            snapshot,
            stop,
            transition,
            input_payload,
            output_payload,
            accepted_attempt_refs,
        ) = _settlement_bundle(self.store)
        capability_only_refs = tuple(
            str(row[0])
            for row in self.store._fetchall(
                """
                SELECT attempt.attempt_ref
                FROM waje_runtime.durable_call_attempts attempt
                WHERE attempt.attempt_ref = ANY(%(attempt_refs)s)
                  AND attempt.call_kind = 'capability'
                ORDER BY attempt.attempt_ref
                """,
                {"attempt_refs": list(accepted_attempt_refs)},
            )
        )

        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "capability_execution_stage_attempt_closure_invalid",
        ):
            self.store.accept_execution_settlement(
                snapshot,
                stop,
                transition,
                input_payload,
                output_payload,
                capability_only_refs,
            )
        self.assertEqual(
            _settlement_counts(self.store, run_id=plan.run_attempt_id),
            (0, 0, 0),
        )

    def test_settlement_rejects_accepted_capability_without_outcome(self):
        (
            plan,
            snapshot,
            stop,
            transition,
            input_payload,
            output_payload,
            accepted_attempt_refs,
        ) = _settlement_bundle(self.store)
        task = plan.capability_tasks[0]
        rogue_ref = _accept_durable_call(
            self.store,
            DurableCallSpec.create(
                run_attempt_id=plan.run_attempt_id,
                intent_revision_id=plan.intent_revision_id,
                plan_revision_id=plan.plan_revision_id,
                task_id=task.task_id,
                stage_name="execute_capability_dag",
                call_kind="capability",
                operation_name="unsettled-capability-contract-test",
                input_ref=f"unsettled-capability:{plan.plan_revision_id}",
                input_payload={"unsettled_capability": plan.plan_revision_id},
            ),
        )

        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "capability_execution_outcome_attempt_closure_invalid",
        ):
            self.store.accept_execution_settlement(
                snapshot,
                stop,
                transition,
                input_payload,
                output_payload,
                tuple(sorted((*accepted_attempt_refs, rogue_ref))),
            )
        self.assertEqual(
            _settlement_counts(self.store, run_id=plan.run_attempt_id),
            (0, 0, 0),
        )

    def test_settlement_rejects_attempt_from_another_stage(self):
        (
            plan,
            snapshot,
            stop,
            transition,
            input_payload,
            output_payload,
            accepted_attempt_refs,
        ) = _settlement_bundle(self.store)
        task = plan.capability_tasks[0]
        wrong_stage_ref = _accept_durable_call(
            self.store,
            DurableCallSpec.create(
                run_attempt_id=plan.run_attempt_id,
                intent_revision_id=plan.intent_revision_id,
                plan_revision_id=plan.plan_revision_id,
                task_id=task.task_id,
                stage_name="bind_evidence_manifest",
                call_kind="query",
                operation_name="wrong-stage-query-contract-test",
                input_ref=f"wrong-stage-query:{plan.plan_revision_id}",
                input_payload={"wrong_stage_query": plan.plan_revision_id},
            ),
        )

        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "capability_execution_stage_attempt_closure_invalid",
        ):
            self.store.accept_execution_settlement(
                snapshot,
                stop,
                transition,
                input_payload,
                output_payload,
                tuple(sorted((*accepted_attempt_refs, wrong_stage_ref))),
            )
        self.assertEqual(
            _settlement_counts(self.store, run_id=plan.run_attempt_id),
            (0, 0, 0),
        )

    def test_settlement_rejects_non_execution_call_kind_in_execution_stage(self):
        (
            plan,
            snapshot,
            stop,
            transition,
            input_payload,
            output_payload,
            accepted_attempt_refs,
        ) = _settlement_bundle(self.store)
        invalid_kind_ref = _accept_durable_call(
            self.store,
            DurableCallSpec.create(
                run_attempt_id=plan.run_attempt_id,
                intent_revision_id=plan.intent_revision_id,
                plan_revision_id=plan.plan_revision_id,
                task_id=None,
                stage_name="execute_capability_dag",
                call_kind="semantic_provider",
                operation_name="invalid-execution-stage-provider",
                input_ref=f"invalid-stage-kind:{plan.plan_revision_id}",
                input_payload={"invalid_stage_kind": plan.plan_revision_id},
            ),
        )

        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "capability_execution_stage_call_kind_invalid",
        ):
            self.store.accept_execution_settlement(
                snapshot,
                stop,
                transition,
                input_payload,
                output_payload,
                tuple(sorted((*accepted_attempt_refs, invalid_kind_ref))),
            )
        self.assertEqual(
            _settlement_counts(self.store, run_id=plan.run_attempt_id),
            (0, 0, 0),
        )

    def test_exact_replay_preserves_one_accepted_execution_transition(self):
        (
            plan,
            snapshot,
            stop,
            transition,
            input_payload,
            output_payload,
            accepted_attempt_refs,
        ) = _settlement_bundle(self.store)

        initial_lifecycle_count = _lifecycle_count(
            self.store,
            run_id=plan.run_attempt_id,
        )
        inserted = self.store.accept_execution_settlement(
            snapshot,
            stop,
            transition,
            input_payload,
            output_payload,
            accepted_attempt_refs,
        )
        replayed = self.store.accept_execution_settlement(
            snapshot,
            stop,
            transition,
            input_payload,
            output_payload,
            accepted_attempt_refs,
        )

        self.assertEqual(inserted, snapshot)
        self.assertEqual(replayed, snapshot)
        self.assertEqual(
            self.store.load_execution_snapshot(plan.plan_revision_id),
            snapshot,
        )
        accepted = self.store.load_accepted_transition(
            run_attempt_id=plan.run_attempt_id,
            node_name="execute_capability_dag",
            input_digest=transition.input_digest,
        )
        self.assertIsNotNone(accepted)
        self.assertEqual(accepted["transition"], transition)
        self.assertEqual(accepted["input_payload"], input_payload)
        self.assertEqual(accepted["output_payload"], output_payload)
        self.assertEqual(
            _settlement_counts(self.store, run_id=plan.run_attempt_id),
            (1, 1, 1),
        )
        self.assertEqual(
            _lifecycle_count(self.store, run_id=plan.run_attempt_id),
            initial_lifecycle_count + 1,
        )

        second_transition = _settlement_transition(
            plan,
            parent_transition_id=transition.transition_id,
            input_payload=input_payload,
            output_payload=output_payload,
        )
        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "capability_execution_transition_parent_invalid",
        ):
            self.store.accept_execution_settlement(
                snapshot,
                stop,
                second_transition,
                input_payload,
                output_payload,
                accepted_attempt_refs,
            )
        self.assertEqual(
            _settlement_counts(self.store, run_id=plan.run_attempt_id),
            (1, 1, 1),
        )

    def test_execution_exact_replay_rejects_conflicting_stage_refs(self):
        (
            plan,
            snapshot,
            stop,
            transition,
            input_payload,
            output_payload,
            accepted_attempt_refs,
        ) = _settlement_bundle(self.store)
        self.store.accept_execution_settlement(
            snapshot,
            stop,
            transition,
            input_payload,
            output_payload,
            accepted_attempt_refs,
        )
        request = self.store.get_run_request(plan.run_attempt_id)
        request.pop("thread_id")
        request.pop("topic_id")
        request["execution_result_refs"] = {
            **request["execution_result_refs"],
            "execution_snapshot_ref": "execution-snapshot:conflict",
        }
        self.store._execute(
            """
            UPDATE waje_runtime.analysis_runs
            SET request = %(request)s::jsonb
            WHERE run_id = %(run_id)s
            """,
            {
                "run_id": plan.run_attempt_id,
                "request": json.dumps(request),
            },
        )

        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "^execution_result_refs_replay_conflict$",
        ):
            self.store.accept_execution_settlement(
                snapshot,
                stop,
                transition,
                input_payload,
                output_payload,
                accepted_attempt_refs,
            )

    def test_exact_replay_rejects_a_conflicting_lifecycle_head(self):
        (
            plan,
            snapshot,
            stop,
            transition,
            input_payload,
            output_payload,
            accepted_attempt_refs,
        ) = _settlement_bundle(self.store)
        self.store.accept_execution_settlement(
            snapshot,
            stop,
            transition,
            input_payload,
            output_payload,
            accepted_attempt_refs,
        )
        settled = self.store.latest_lifecycle_state(plan.run_attempt_id)
        self.store.append_lifecycle_state(
            settled.transition(
                execution_state="waiting",
                evidence_state="not_started",
            )
        )

        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "capability_execution_lifecycle_replay_conflict",
        ):
            self.store.accept_execution_settlement(
                snapshot,
                stop,
                transition,
                input_payload,
                output_payload,
                accepted_attempt_refs,
            )

    def test_non_idle_retry_state_cannot_accept_execution_settlement(self):
        (
            plan,
            snapshot,
            stop,
            transition,
            input_payload,
            output_payload,
            accepted_attempt_refs,
        ) = _settlement_bundle(self.store)
        current = self.store.latest_lifecycle_state(plan.run_attempt_id)
        self.store.append_lifecycle_state(current.transition(retry_state="running"))

        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "capability_execution_lifecycle_not_ready",
        ):
            self.store.accept_execution_settlement(
                snapshot,
                stop,
                transition,
                input_payload,
                output_payload,
                accepted_attempt_refs,
            )

        self.assertEqual(
            _settlement_counts(self.store, run_id=plan.run_attempt_id),
            (0, 0, 0),
        )

    def test_tampered_parent_is_rejected_before_settlement(self):
        (
            plan,
            snapshot,
            stop,
            _,
            input_payload,
            output_payload,
            accepted_attempt_refs,
        ) = _settlement_bundle(self.store)
        tampered = _settlement_transition(
            plan,
            parent_transition_id="transition-tampered-parent",
            input_payload=input_payload,
            output_payload=output_payload,
        )

        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "capability_execution_transition_parent_not_current_head",
        ):
            self.store.accept_execution_settlement(
                snapshot,
                stop,
                tampered,
                input_payload,
                output_payload,
                accepted_attempt_refs,
            )
        self.assertEqual(
            _settlement_counts(self.store, run_id=plan.run_attempt_id),
            (0, 0, 0),
        )

    def test_self_consistent_tampered_payload_is_rejected(self):
        for target in ("input", "output"):
            with self.subTest(target=target):
                (
                    plan,
                    snapshot,
                    stop,
                    transition,
                    input_payload,
                    output_payload,
                    accepted_attempt_refs,
                ) = _settlement_bundle(self.store)
                if target == "input":
                    input_payload = {
                        **input_payload,
                        "hard_budget_limit": 999,
                    }
                else:
                    output_payload = {
                        **output_payload,
                        "exploration_stop_record": {
                            **output_payload["exploration_stop_record"],
                            "reason": "no_ready_tasks",
                        },
                    }
                tampered = _settlement_transition(
                    plan,
                    parent_transition_id=transition.parent_transition_id,
                    input_payload=input_payload,
                    output_payload=output_payload,
                )

                with self.assertRaisesRegex(
                    EvidenceIntegrityError,
                    "capability_execution_transition_invalid",
                ):
                    self.store.accept_execution_settlement(
                        snapshot,
                        stop,
                        tampered,
                        input_payload,
                        output_payload,
                        accepted_attempt_refs,
                    )
                self.assertEqual(
                    _settlement_counts(
                        self.store,
                        run_id=plan.run_attempt_id,
                    ),
                    (0, 0, 0),
                )
