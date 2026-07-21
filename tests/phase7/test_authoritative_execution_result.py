from __future__ import annotations

from dataclasses import FrozenInstanceError
import json

import pytest

from bi_agent.runtime.authoritative_execution_result import (
    AuthoritativeExecutionResult,
    AuthoritativeExecutionResultContractError,
    validate_typed_authoritative_execution_result,
)
from bi_agent.runtime.capability_authority import (
    CAPABILITY_EVIDENCE_OBSERVATION_BYTE_LIMIT,
    CapabilityAdapterOutput,
    CapabilityAttempt,
    CapabilityAuthorityContractError,
    CapabilityEvidence,
    CapabilityFailure,
    CapabilityOutcome,
    EvidenceLedgerEntry,
    ExecutionSnapshot,
    ExplorationStopRecord,
    FailureRecord,
)
from bi_agent.runtime.capability_scheduler import (
    capability_execution_transition_payloads,
)
from bi_agent.runtime.evidence_authority import canonical_digest
from bi_agent.runtime.plan_authority import (
    AnalysisAxis,
    ClaimObligation,
    EvidenceRequirement,
    PlanRevision,
)
from bi_agent.runtime.single_authority import DurableTransition
from tests.support.temporal_authority import resolved_test_temporal_authority


def _plan() -> PlanRevision:
    obligation = ClaimObligation.create(
        claim_kind="comparative_change",
        role="user_required",
        subject={
            "target_metric_ref": "paid_amount",
            "scope": {"scope_type": "full_sample", "filters": []},
            "outcome_refs": ("outcome:comparative_change",),
            "goal_refs": ("explain_change",),
        },
        evidence_requirement=EvidenceRequirement.create(
            operator="any_of",
            evidence_kinds=("observed",),
        ),
        success_policy={
            "policy": "verified_or_explicit_boundary",
            "minimum_claim_strength": "directional",
        },
    )
    axis = AnalysisAxis.create(
        axis_id="authoritative_execution_test",
        role="required",
        axis_kind="change_validation",
        target_metric_refs=("paid_amount",),
        metric_refs=(),
        dimension_refs=("region",),
        context_source_refs=(),
        capability_refs=("compare_periods", "data_quality_profile"),
        reconciliation_group="paid_amount_change",
        selection_policy="primary_baseline_required",
        source_refs=("contract:test",),
        goal_refs=("explain_change",),
        supports_obligation_ids=(obligation.obligation_id,),
    )
    temporal_authority = resolved_test_temporal_authority(
        time_spec={"kind": "date", "target": "2026-06-19"},
        comparison_spec={
            "kind": "fixed_window",
            "baseline_class": "prior_period",
            "baseline_start": "2026-06-18",
            "baseline_end": "2026-06-18",
            "aggregation": "sum_of_complete_days",
        },
        require_physical_baseline=True,
    )
    return PlanRevision.create(
        run_attempt_id="run-authoritative-execution",
        supersedes_plan_revision_id=None,
        intent_revision_id="intent-authoritative-execution",
        decision_refs=("decision:baseline",),
        authority_context_ref="authority-context:execution",
        planner_proposal_ref="planner-proposal:execution",
        proposal_admission_ref="proposal-admission:execution",
        temporal_authority=temporal_authority,
        resolved_window_refs=temporal_authority.resolved_window_refs,
        context_window_specs=(),
        claim_obligations=(obligation,),
        analysis_axes=(axis,),
        capability_task_specs=(
            {
                "task_key": "compare",
                "capability_id": "compare_periods",
                "normalized_input_refs": (
                    "authority-context:execution",
                    "raw-row:must-not-be-public",
                ),
                "dependency_task_keys": (),
                "obligation_edges": (
                    {
                        "obligation_id": obligation.obligation_id,
                        "required": True,
                    },
                ),
                "execution_rank": 1,
                "declared_budget_units": 1,
                "governor_inputs": {
                    "expected_information_gain": "obligation_closing",
                    "materiality": "user_required",
                    "actionability": "decision_supporting",
                    "statistical_risk": "contract_bounded",
                },
                "execution_policy": {
                    "degradation_policy": {
                        "missing_required_input": "block_claim",
                    },
                    "integrity_failure": "fail_closed",
                    "input_states": (),
                },
            },
            {
                "task_key": "quality",
                "capability_id": "data_quality_profile",
                "normalized_input_refs": ("authority-context:execution",),
                "dependency_task_keys": (),
                "obligation_edges": (
                    {
                        "obligation_id": obligation.obligation_id,
                        "required": True,
                    },
                ),
                "execution_rank": 2,
                "declared_budget_units": 1,
                "governor_inputs": {
                    "expected_information_gain": "obligation_closing",
                    "materiality": "user_required",
                    "actionability": "decision_supporting",
                    "statistical_risk": "contract_bounded",
                },
                "execution_policy": {
                    "degradation_policy": {
                        "missing_required_input": "report_contract_gap",
                    },
                    "integrity_failure": "fail_closed",
                    "input_states": (),
                },
            },
        ),
        assumption_refs=(),
        budget_policy_ref="budget-policy:execution",
        contract_versions={
            "runtime": "single-authority-phase03.v1",
            "provider_audit": "internal-provider-audit",
        },
    )


def _successful_bundle(
    plan: PlanRevision,
    task,
    *,
    evidence_kind: str = "observed",
):
    attempt = CapabilityAttempt.create(plan, task)
    evidence = CapabilityEvidence.create(
        evidence_ref=f"evidence:{task.task_id}",
        binding_record_ref=f"binding:{task.task_id}",
        execution_state="available",
        evidence_kind=evidence_kind,
        data_contract_state="complete",
        supported_claim_kinds=("comparative_change",),
        evidence_strength="high",
        maximum_claim_strength="directional",
        observation_facts=({"name": "paid_amount_delta", "value": 125.0},),
        scope="full_sample",
        window_refs=plan.resolved_window_refs,
        dimension_path=("region", "lagos"),
        limitation_refs=("limitation:sample-window",),
        result_refs=("result:paid-amount-compare",),
        completeness_report_refs=("completeness:paid-amount-compare",),
        hierarchy_qualified=True,
    )
    output = CapabilityAdapterOutput.create(
        status="succeeded",
        output_payload={"raw_rows": [{"user_id": "internal-row"}]},
        evidence=(evidence,),
        affected_obligation_ids=task.supports_obligation_ids,
        limitation_refs=("limitation:sample-window",),
        retryability="never",
    )
    outcome = CapabilityOutcome.create(
        attempt,
        task,
        output,
        failure_ref=None,
        budget_units=1,
    )
    ledger = EvidenceLedgerEntry.create(plan, task, outcome, evidence)
    return attempt, outcome, (ledger,), ()


def test_outcome_does_not_promote_evidence_local_limitation_to_task_scope() -> None:
    plan = _plan()
    task = next(item for item in plan.capability_tasks if item.task_key == "compare")
    attempt = CapabilityAttempt.create(plan, task)
    evidence = CapabilityEvidence.create(
        evidence_ref="evidence:local-region-boundary",
        binding_record_ref="binding:local-region-boundary",
        execution_state="available",
        evidence_kind="observed",
        data_contract_state="complete",
        supported_claim_kinds=("comparative_change",),
        evidence_strength="high",
        maximum_claim_strength="directional",
        observation_facts=({"name": "paid_amount_delta", "value": 125.0},),
        scope="full_sample",
        window_refs=plan.resolved_window_refs,
        dimension_path=("region",),
        limitation_refs=("limitation:sparse-region-members",),
        result_refs=("result:paid-amount-compare",),
        completeness_report_refs=("completeness:paid-amount-compare",),
        hierarchy_qualified=True,
    )
    output = CapabilityAdapterOutput.create(
        status="succeeded",
        output_payload={"status": "ready"},
        evidence=(evidence,),
        affected_obligation_ids=task.supports_obligation_ids,
        limitation_refs=(),
        retryability="never",
    )

    outcome = CapabilityOutcome.create(
        attempt,
        task,
        output,
        failure_ref=None,
        budget_units=1,
    )

    assert evidence.limitation_refs == ("limitation:sparse-region-members",)
    assert outcome.limitation_refs == ()


def test_capability_evidence_rejects_unbounded_observation_payload() -> None:
    with pytest.raises(
        CapabilityAuthorityContractError,
        match="capability_evidence_observation_budget_exceeded",
    ):
        CapabilityEvidence.create(
            evidence_ref="evidence:oversized-observation",
            binding_record_ref="binding:oversized-observation",
            execution_state="available",
            evidence_kind="observed",
            data_contract_state="complete",
            supported_claim_kinds=("comparative_change",),
            evidence_strength="high",
            maximum_claim_strength="directional",
            observation_facts=(
                {
                    "projection_kind": "claim_material_summary",
                    "summary": "x" * (CAPABILITY_EVIDENCE_OBSERVATION_BYTE_LIMIT + 1),
                },
            ),
            scope="full_sample",
            window_refs=("window:target", "window:baseline"),
            dimension_path=(),
            limitation_refs=(),
            result_refs=("result:oversized-observation",),
            completeness_report_refs=("completeness:oversized-observation",),
            hierarchy_qualified=False,
        )


def _failed_bundle(plan: PlanRevision, task):
    attempt = CapabilityAttempt.create(plan, task)
    failure = CapabilityFailure.create(
        layer="query",
        kind="query_transport_failed",
        scope="task",
        affected_refs=(task.task_id, *task.supports_obligation_ids),
        integrity_level="task",
        retryability="same_input",
        user_actionable=False,
        business_boundary="data_quality_result_unavailable",
        technical_detail_ref="technical-detail:provider-debug-owner-42",
    )
    output = CapabilityAdapterOutput.create(
        status="technical_failed",
        output_payload={"provider_audit": "secret-provider-trace"},
        evidence=(),
        affected_obligation_ids=task.supports_obligation_ids,
        limitation_refs=("limitation:data-quality-unavailable",),
        retryability="same_input",
        failure=failure,
    )
    failure_record = FailureRecord.create(attempt, failure)
    outcome = CapabilityOutcome.create(
        attempt,
        task,
        output,
        failure_ref=failure_record.failure_ref,
        budget_units=1,
    )
    return attempt, outcome, (), (failure_record,)


def _records(*, evidence_kind: str = "observed"):
    plan = _plan()
    tasks = {task.task_key: task for task in plan.capability_tasks}
    bundles = (
        _successful_bundle(
            plan,
            tasks["compare"],
            evidence_kind=evidence_kind,
        ),
        _failed_bundle(plan, tasks["quality"]),
    )
    outcomes = tuple(bundle[1] for bundle in bundles)
    evidence = tuple(item for bundle in bundles for item in bundle[2])
    failures = tuple(item for bundle in bundles for item in bundle[3])
    stop = ExplorationStopRecord.create(
        plan,
        outcomes,
        reason="plan_exhausted",
        hard_budget_limit=None,
    )
    snapshot = ExecutionSnapshot.create(
        plan,
        stop,
        outcomes,
        evidence,
        failures,
    )
    input_payload, output_payload = capability_execution_transition_payloads(
        plan,
        snapshot,
        stop,
    )
    transition = DurableTransition.create(
        node_name="execute_capability_dag",
        parent_transition_id="transition:phase02-plan-bound",
        run_attempt_id=plan.run_attempt_id,
        intent_revision_id=plan.intent_revision_id,
        decision_ledger_position=1,
        input_digest=canonical_digest(input_payload),
        output_digest=canonical_digest(output_payload),
        execution_attempt=1,
        provider_ref="provider-audit:internal-runtime",
        model_ref="deterministic-capability-dag.v1",
        status="succeeded",
        acceptance_state="accepted",
        next_transition="phase03_evidence_bound",
        started_at="2026-07-18T08:00:00+00:00",
        finished_at="2026-07-18T08:00:01+00:00",
    )
    return plan, snapshot, stop, bundles, transition


def _result(*, evidence_kind: str = "observed") -> AuthoritativeExecutionResult:
    plan, snapshot, stop, bundles, transition = _records(evidence_kind=evidence_kind)
    return AuthoritativeExecutionResult.from_records(
        plan_revision=plan,
        execution_snapshot=snapshot,
        exploration_stop_record=stop,
        capability_outcome_bundles=bundles,
        durable_transition=transition,
    )


def test_result_is_evidence_ready_and_keeps_complete_internal_records() -> None:
    result = _result()

    assert result.schema_version == "single-authority-phase03.v1"
    assert result.status == "evidence_ready"
    assert result.plan_revision_id == result.plan_revision.plan_revision_id
    assert result.execution_snapshot_ref == (
        result.execution_snapshot.execution_snapshot_ref
    )
    assert result.stop_ref == result.exploration_stop_record.stop_ref
    assert result.transition_id == result.durable_transition.transition_id
    assert len(result.capability_outcome_bundles) == 2
    assert result.authoritative_execution_result_ref == (
        "authoritative-execution-result:sha256:" + result.content_digest
    )
    internal = result.to_dict()
    assert any(
        failure["technical_detail_ref"] == "technical-detail:provider-debug-owner-42"
        for bundle in internal["capability_outcome_bundles"]
        for failure in bundle["failure_records"]
    )
    with pytest.raises(FrozenInstanceError):
        result.status = "draft"


def test_strict_roundtrip_and_content_addressed_identity() -> None:
    result = _result()
    payload = result.to_dict()

    assert AuthoritativeExecutionResult.from_dict(payload) == result

    extra = dict(payload)
    extra["unexpected"] = True
    with pytest.raises(
        AuthoritativeExecutionResultContractError,
        match="authoritative_execution_result_shape_invalid",
    ):
        AuthoritativeExecutionResult.from_dict(extra)

    bad_ref = dict(payload)
    bad_ref["authoritative_execution_result_ref"] = "result:tampered"
    with pytest.raises(
        AuthoritativeExecutionResultContractError,
        match="authoritative_execution_result_ref_invalid",
    ):
        AuthoritativeExecutionResult.from_dict(bad_ref)

    bad_digest = dict(payload)
    bad_digest["bundle_set_digest"] = "0" * 64
    with pytest.raises(
        AuthoritativeExecutionResultContractError,
        match="authoritative_execution_result_bundle_digest_invalid",
    ):
        AuthoritativeExecutionResult.from_dict(bad_digest)

    bad_plan = result.to_dict()
    bad_plan["plan_revision"]["content_digest"] = "0" * 64
    with pytest.raises(
        AuthoritativeExecutionResultContractError,
        match="authoritative_execution_result_record_invalid",
    ):
        AuthoritativeExecutionResult.from_dict(bad_plan)


def test_typed_validation_does_not_serialize_capability_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _result()

    def forbidden_serialization(*_args, **_kwargs):
        raise AssertionError("typed_execution_validation_serialized_evidence")

    monkeypatch.setattr(result.__class__, "to_dict", forbidden_serialization)
    monkeypatch.setattr(EvidenceLedgerEntry, "to_dict", forbidden_serialization)

    assert validate_typed_authoritative_execution_result(result) is result


def test_record_closure_rejects_missing_bundle_and_wrong_transition_digest() -> None:
    plan, snapshot, stop, bundles, transition = _records()

    with pytest.raises(
        AuthoritativeExecutionResultContractError,
        match="authoritative_execution_result_outcome_closure_invalid",
    ):
        AuthoritativeExecutionResult.from_records(
            plan_revision=plan,
            execution_snapshot=snapshot,
            exploration_stop_record=stop,
            capability_outcome_bundles=bundles[:1],
            durable_transition=transition,
        )

    wrong_transition = DurableTransition.create(
        node_name=transition.node_name,
        parent_transition_id=transition.parent_transition_id,
        run_attempt_id=transition.run_attempt_id,
        intent_revision_id=transition.intent_revision_id,
        decision_ledger_position=transition.decision_ledger_position,
        input_digest=transition.input_digest,
        output_digest="0" * 64,
        execution_attempt=transition.execution_attempt,
        provider_ref=transition.provider_ref,
        model_ref=transition.model_ref,
        status=transition.status,
        acceptance_state=transition.acceptance_state,
        next_transition=transition.next_transition,
        started_at=transition.started_at,
        finished_at=transition.finished_at,
    )
    with pytest.raises(
        AuthoritativeExecutionResultContractError,
        match="authoritative_execution_result_transition_digest_invalid",
    ):
        AuthoritativeExecutionResult.from_records(
            plan_revision=plan,
            execution_snapshot=snapshot,
            exploration_stop_record=stop,
            capability_outcome_bundles=bundles,
            durable_transition=wrong_transition,
        )


def test_bundle_order_cannot_change_result_identity() -> None:
    plan, snapshot, stop, bundles, transition = _records()
    forward = AuthoritativeExecutionResult.from_records(
        plan_revision=plan,
        execution_snapshot=snapshot,
        exploration_stop_record=stop,
        capability_outcome_bundles=bundles,
        durable_transition=transition,
    )
    reversed_result = AuthoritativeExecutionResult.from_records(
        plan_revision=plan,
        execution_snapshot=snapshot,
        exploration_stop_record=stop,
        capability_outcome_bundles=tuple(reversed(bundles)),
        durable_transition=transition,
    )

    assert reversed_result == forward


def test_public_projection_is_an_explicit_business_safe_whitelist() -> None:
    result = _result()
    public = result.public_projection()

    assert set(public) == {
        "schema_version",
        "status",
        "result_ref",
        "plan_revision_id",
        "execution_snapshot_ref",
        "tasks",
        "outcomes",
        "obligations",
        "evidence",
        "failures",
        "limitations",
        "stop",
    }
    assert public["status"] == "evidence_ready"
    assert public["evidence"][0]["dimension_path"] == ["region", "lagos"]
    assert public["evidence"][0]["scope"] == "full_sample"
    assert public["evidence"][0]["window_refs"] == sorted(
        result.plan_revision.resolved_window_refs
    )
    assert public["evidence"][0]["hierarchy_qualified"] is True
    assert "observations" not in public["evidence"][0]
    assert public["failures"] == [
        {
            "failure_ref": public["failures"][0]["failure_ref"],
            "task_id": public["failures"][0]["task_id"],
            "scope": "task",
            "integrity_level": "task",
            "retryability": "same_input",
            "user_actionable": False,
            "business_boundary": "data_quality_result_unavailable",
        }
    ]
    assert "limitation:data-quality-unavailable" in public["limitations"]
    serialized = json.dumps(public, ensure_ascii=False, sort_keys=True)
    for internal_value in (
        "technical_detail_ref",
        "technical-detail:provider-debug-owner-42",
        "raw-row:must-not-be-public",
        "internal-row",
        "owner_id",
        "internal-owner",
        "internal-debug-context",
        "provider_ref",
        "provider-audit:internal-runtime",
        "model_ref",
        "internal-provider-audit",
    ):
        assert internal_value not in serialized
