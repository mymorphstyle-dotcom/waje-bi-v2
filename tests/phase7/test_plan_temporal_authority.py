from __future__ import annotations

import pytest

from bi_agent.runtime.plan_authority import (
    AnalysisAxis,
    ClaimObligation,
    EvidenceRequirement,
    PlanAuthorityContractError,
    PlanRevision,
)
from bi_agent.runtime.single_authority import DecisionLedger, DecisionRecord
from bi_agent.runtime.temporal_comparison import EffectiveTemporalComparison
from tests.support.temporal_authority import resolved_test_temporal_authority


EVALUATION_RANGE = {
    "kind": "date_range",
    "start": "2024-01-01",
    "end": "2026-12-31",
}


def _calendar_authority(
    *,
    target_members: list[str],
    baseline_members: list[str],
) -> EffectiveTemporalComparison:
    return resolved_test_temporal_authority(
        time_spec=EVALUATION_RANGE,
        comparison_spec={
            "kind": "calendar_partition",
            "baseline_class": "prior_period",
            "period_grain": "year",
            "partition_field": "quarter_of_year",
            "target_members": target_members,
            "baseline_members": baseline_members,
            "aggregation": "mean_of_complete_days",
        },
        require_physical_baseline=False,
    )


def _plan(
    temporal_authority: EffectiveTemporalComparison,
    *,
    decision_refs: tuple[str, ...] = (),
    resolved_window_refs: tuple[str, ...] | None = None,
) -> PlanRevision:
    obligation = ClaimObligation.create(
        claim_kind="comparative_change",
        role="user_required",
        subject={
            "target_metric_ref": "paid_amount",
            "scope": {"scope_type": "full_sample", "filters": []},
            "outcome_refs": ("outcome:comparison",),
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
        axis_id="temporal_authority_test",
        role="required",
        axis_kind="comparison",
        target_metric_refs=("paid_amount",),
        metric_refs=(),
        dimension_refs=(),
        context_source_refs=(),
        capability_refs=("temporal_authority_fixture",),
        reconciliation_group="paid_amount",
        selection_policy="retain_all_qualified_evidence",
        source_refs=("contract:test",),
        goal_refs=("explain_change",),
        supports_obligation_ids=(obligation.obligation_id,),
    )
    return PlanRevision.create(
        run_attempt_id="run-plan-temporal-authority",
        supersedes_plan_revision_id=None,
        intent_revision_id="intent-plan-temporal-authority",
        decision_refs=decision_refs,
        authority_context_ref="authority-context:temporal-authority",
        planner_proposal_ref="planner-proposal:temporal-authority",
        proposal_admission_ref="proposal-admission:temporal-authority",
        temporal_authority=temporal_authority,
        resolved_window_refs=(
            temporal_authority.resolved_window_refs
            if resolved_window_refs is None
            else resolved_window_refs
        ),
        context_window_specs=(),
        claim_obligations=(obligation,),
        analysis_axes=(axis,),
        capability_task_specs=(
            {
                "task_key": "temporal_authority_fixture",
                "capability_id": "temporal_authority_fixture",
                "normalized_input_refs": (
                    "authority-context:temporal-authority",
                    *temporal_authority.resolved_window_refs,
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
                    "degradation_policy": {"missing_required_input": "block_claim"},
                    "integrity_failure": "fail_closed",
                    "input_states": (),
                },
            },
        ),
        assumption_refs=(),
        budget_policy_ref="budget-policy:temporal-authority",
        contract_versions={"runtime": "test.v1"},
    )


def test_plan_digest_and_roundtrip_freeze_complete_temporal_authority() -> None:
    first_authority = _calendar_authority(
        target_members=["Q2"],
        baseline_members=["Q1"],
    )
    second_authority = _calendar_authority(
        target_members=["Q3"],
        baseline_members=["Q1"],
    )
    assert first_authority.resolved_window_refs == second_authority.resolved_window_refs

    first = _plan(first_authority)
    second = _plan(second_authority)

    assert first.plan_revision_id != second.plan_revision_id
    assert first.content_digest != second.content_digest
    assert first.to_dict()["temporal_authority"] == first_authority.to_dict()
    assert PlanRevision.from_dict(first.to_dict()) == first


def test_plan_rejects_resolved_window_refs_drift_from_temporal_authority() -> None:
    authority = _calendar_authority(
        target_members=["Q2"],
        baseline_members=["Q1"],
    )

    with pytest.raises(
        PlanAuthorityContractError,
        match="plan_revision_temporal_window_refs_mismatch",
    ):
        _plan(authority, resolved_window_refs=("window:drifted",))


def test_plan_requires_temporal_decision_source_in_decision_refs() -> None:
    decision = DecisionRecord.create(
        intent_revision_id="intent-plan-temporal-authority",
        slot_id="comparison_baseline",
        value={"baseline_id": "previous_day"},
        source="user",
        status="user_confirmed",
        materiality="material",
        affected_plan_fields=("resolved_window_refs",),
        option_id="comparison_baseline.previous_day",
    )
    authority = resolved_test_temporal_authority(
        time_spec={"kind": "date", "target": "2026-06-19"},
        comparison_spec={
            "kind": "decision_slot",
            "slot_id": "comparison_baseline",
        },
        decision_ledger=DecisionLedger().append(decision),
        require_physical_baseline=True,
    )

    with pytest.raises(
        PlanAuthorityContractError,
        match="plan_revision_temporal_decision_ref_missing",
    ):
        _plan(authority)

    assert (
        _plan(authority, decision_refs=(decision.decision_id,)).temporal_authority
        == authority
    )


def test_plan_preserves_every_atomic_temporal_decision_ref() -> None:
    intent_revision_id = "intent-plan-temporal-authority"
    boundary_decision = DecisionRecord.create(
        intent_revision_id=intent_revision_id,
        slot_id="month_phase_definition",
        value={
            "value_ref": "definition_1",
            "member_definitions": [
                {"member": "start", "day_start": 1, "day_end": 10},
                {"member": "mid", "day_start": 11, "day_end": 20},
                {"member": "end", "day_start": 21, "day_end": 31},
            ],
        },
        source="user",
        status="user_confirmed",
        materiality="material",
        affected_plan_fields=(
            "comparison_spec.member_definitions",
            "resolved_window_refs",
        ),
        option_id="month_phase_definition.definition_1",
    )
    aggregation_decision = DecisionRecord.create(
        intent_revision_id=intent_revision_id,
        slot_id="phase_aggregation",
        value={"aggregation": "mean_of_complete_days"},
        source="user",
        status="user_confirmed",
        materiality="material",
        affected_plan_fields=(
            "comparison_spec.aggregation",
            "resolved_window_refs",
        ),
        option_id="phase_aggregation.mean_of_complete_days",
    )
    ledger = DecisionLedger().append(boundary_decision).append(
        aggregation_decision
    )
    authority = resolved_test_temporal_authority(
        time_spec={
            "kind": "date_range",
            "start": "2024-01-01",
            "end": "2026-05-31",
        },
        comparison_spec={
            "kind": "calendar_partition",
            "baseline_class": "same_month_phase",
            "period_grain": "month",
            "partition_field": "month_phase",
            "target_members": ["start"],
            "baseline_members": ["mid", "end"],
            "aggregation": "sum_of_complete_days",
            "member_definitions": [
                {"member": "start", "day_start": 1, "day_end": 7},
                {"member": "mid", "day_start": 8, "day_end": 21},
                {"member": "end", "day_start": 22, "day_end": 31},
            ],
        },
        decision_ledger=ledger,
        require_physical_baseline=False,
    )
    expected_refs = (
        boundary_decision.decision_id,
        aggregation_decision.decision_id,
    )

    assert set(authority.decision_refs) == set(expected_refs)
    assert _plan(authority, decision_refs=expected_refs).temporal_authority == authority
    with pytest.raises(
        PlanAuthorityContractError,
        match="plan_revision_temporal_decision_ref_missing",
    ):
        _plan(authority, decision_refs=(boundary_decision.decision_id,))


def test_plan_roundtrips_confirmed_calendar_partition_values_unchanged() -> None:
    intent_revision_id = "intent-plan-temporal-authority"
    definitions = [
        {"member": "start", "day_start": 1, "day_end": 10},
        {"member": "mid", "day_start": 11, "day_end": 20},
        {"member": "end", "day_start": 21, "day_end": 31},
    ]
    boundary_decision = DecisionRecord.create(
        intent_revision_id=intent_revision_id,
        slot_id="month_phase_definition",
        value={
            "value_ref": "definition_1",
            "member_definitions": definitions,
        },
        source="user",
        status="user_confirmed",
        materiality="material",
        affected_plan_fields=(
            "comparison_spec.member_definitions",
            "resolved_window_refs",
        ),
        option_id="month_phase_definition.definition_1",
    )
    aggregation_decision = DecisionRecord.create(
        intent_revision_id=intent_revision_id,
        slot_id="phase_aggregation",
        value={"aggregation": "sum_of_complete_days"},
        source="user",
        status="user_confirmed",
        materiality="material",
        affected_plan_fields=(
            "comparison_spec.aggregation",
            "resolved_window_refs",
        ),
        option_id="phase_aggregation.sum_of_complete_days",
    )
    ledger = DecisionLedger().append(boundary_decision).append(
        aggregation_decision
    )
    authority = resolved_test_temporal_authority(
        time_spec={
            "kind": "date_range",
            "start": "2024-01-01",
            "end": "2026-05-31",
        },
        comparison_spec={
            "kind": "calendar_partition",
            "baseline_class": "same_month_phase",
            "period_grain": "month",
            "partition_field": "month_phase",
            "target_members": ["start"],
            "baseline_members": ["mid", "end"],
            "aggregation": "sum_of_complete_days",
            "member_definitions": definitions,
        },
        decision_ledger=ledger,
        require_physical_baseline=False,
    )
    decision_refs = (
        boundary_decision.decision_id,
        aggregation_decision.decision_id,
    )
    plan = _plan(authority, decision_refs=decision_refs)

    assert authority.source == "decision"
    assert set(authority.decision_refs) == set(decision_refs)
    assert PlanRevision.from_dict(plan.to_dict()) == plan


def test_plan_rejects_unresolved_temporal_authority() -> None:
    authority = resolved_test_temporal_authority(
        time_spec={
            "kind": "date_range",
            "start": "2026-04-01",
            "end": "2026-06-30",
        },
        comparison_spec={
            "kind": "decision_slot",
            "slot_id": "comparison_window",
        },
        require_physical_baseline=False,
    )

    with pytest.raises(
        PlanAuthorityContractError,
        match="plan_revision_temporal_authority_unresolved",
    ):
        _plan(authority)
