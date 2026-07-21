from __future__ import annotations

from typing import Any, Mapping

from bi_agent.runtime.single_authority import DecisionLedger, DecisionRecord
from bi_agent.runtime.temporal_comparison import (
    EffectiveTemporalComparison,
    resolve_effective_comparison,
)


def resolved_test_temporal_authority(
    *,
    time_spec: Mapping[str, Any],
    comparison_spec: Mapping[str, Any],
    decision_ledger: DecisionLedger = DecisionLedger(),
    require_physical_baseline: bool,
) -> EffectiveTemporalComparison:
    """Build test authority from explicit temporal inputs, never from window refs."""

    return resolve_effective_comparison(
        time_spec=time_spec,
        comparison_spec=comparison_spec,
        decision_ledger=decision_ledger,
        require_physical_baseline=require_physical_baseline,
    )


def resolved_test_daily_pair_authority(
    *,
    target: str,
    baseline_id: str,
) -> EffectiveTemporalComparison:
    """Build one explicit canonical daily comparison for compiler fixtures."""

    time_spec = {"kind": "date", "target": target}
    comparison_spec = {
        "kind": "decision_slot",
        "slot_id": "comparison_baseline",
    }
    decision = DecisionRecord.create(
        intent_revision_id=f"intent:test:{target}",
        slot_id="comparison_baseline",
        value={"baseline_id": baseline_id},
        source="user",
        status="user_confirmed",
        materiality="material",
        affected_plan_fields=("resolved_window_refs",),
        option_id=f"comparison_baseline.{baseline_id}",
    )
    return resolved_test_temporal_authority(
        time_spec=time_spec,
        comparison_spec=comparison_spec,
        decision_ledger=DecisionLedger().append(decision),
        require_physical_baseline=True,
    )
