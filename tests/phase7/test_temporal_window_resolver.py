from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from bi_agent.runtime.evidence_authority import canonical_digest
from bi_agent.runtime.single_authority import DecisionLedger, DecisionRecord
from bi_agent.runtime.temporal_comparison import resolve_effective_comparison
from bi_agent.runtime.window_resolver import (
    WindowResolutionError,
    resolve_temporal_windows,
)


AS_OF = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
TIMEZONE = "Africa/Lagos"


def _resolve_windows(
    temporal_authority,
    *,
    context_window_specs=(),
    watermark=date(2026, 12, 31),
):
    return resolve_temporal_windows(
        temporal_authority,
        context_window_specs=context_window_specs,
        as_of=AS_OF,
        timezone_name=TIMEZONE,
        dataset_watermarks={"paid_order_success": watermark},
        affected_capabilities=("compare_periods",),
        affected_claim_types=("comparative_change",),
    )


def _reseal(temporal_authority):
    content_digest = canonical_digest(temporal_authority._material_payload())
    return replace(
        temporal_authority,
        content_digest=content_digest,
        authority_ref="temporal-comparison:sha256:" + content_digest,
    )


@pytest.mark.parametrize(
    ("time_spec", "expected_end", "expected_days", "expected_aggregation"),
    (
        (
            {"kind": "date", "target": "2026-06-19"},
            "2026-06-20",
            1,
            "daily_total",
        ),
        (
            {
                "kind": "date_range",
                "start": "2026-06-01",
                "end": "2026-06-19",
            },
            "2026-06-20",
            19,
            "sum_of_complete_days",
        ),
    ),
)
def test_target_only_freezes_exact_inclusive_bounds_and_aggregation(
    time_spec,
    expected_end,
    expected_days,
    expected_aggregation,
) -> None:
    authority = resolve_effective_comparison(
        time_spec=time_spec,
        comparison_spec={"kind": "none"},
        decision_ledger=DecisionLedger(),
        require_physical_baseline=False,
    )

    result = _resolve_windows(authority)

    assert authority.target_window.aggregation == expected_aggregation
    assert len(result.windows) == 1
    target = result.windows[0]
    assert target.window_id == "target_day"
    assert target.end_exclusive == expected_end
    assert target.required_complete_days == expected_days
    assert target.aggregation == expected_aggregation
    assert result.gaps == ()


@pytest.mark.parametrize(
    (
        "baseline_id",
        "expected_start",
        "expected_end",
        "expected_days",
        "expected_aggregation",
    ),
    (
        (
            "previous_day",
            "2026-06-18",
            "2026-06-19",
            1,
            "sum_of_complete_days",
        ),
        (
            "rolling_7_day_baseline",
            "2026-06-12",
            "2026-06-19",
            7,
            "mean_of_complete_days",
        ),
        (
            "same_weekday_last_week",
            "2026-06-12",
            "2026-06-13",
            1,
            "sum_of_complete_days",
        ),
    ),
)
def test_canonical_daily_pair_preserves_id_and_aggregation(
    baseline_id,
    expected_start,
    expected_end,
    expected_days,
    expected_aggregation,
) -> None:
    decision = DecisionRecord.create(
        intent_revision_id="intent-case-b",
        slot_id="comparison_baseline",
        value={"baseline_id": baseline_id},
        source="user",
        status="user_confirmed",
        materiality="material",
        affected_plan_fields=("resolved_window_refs",),
        option_id=f"comparison_baseline.{baseline_id}",
    )
    authority = resolve_effective_comparison(
        time_spec={"kind": "date", "target": "2026-06-19"},
        comparison_spec={
            "kind": "decision_slot",
            "slot_id": "comparison_baseline",
        },
        decision_ledger=DecisionLedger().append(decision),
        require_physical_baseline=True,
    )

    result = _resolve_windows(authority)
    windows = {window.window_id: window for window in result.windows}

    assert tuple(windows) == ("target_day", baseline_id)
    assert windows["target_day"].start_inclusive == "2026-06-19"
    assert windows[baseline_id].start_inclusive == expected_start
    assert windows[baseline_id].end_exclusive == expected_end
    assert windows[baseline_id].required_complete_days == expected_days
    assert windows["target_day"].aggregation == "sum_of_complete_days"
    assert windows[baseline_id].aggregation == expected_aggregation


def test_multi_day_pair_uses_baseline_window_and_target_start_context() -> None:
    authority = resolve_effective_comparison(
        time_spec={
            "kind": "date_range",
            "start": "2026-04-01",
            "end": "2026-06-30",
        },
        comparison_spec={
            "kind": "fixed_window",
            "baseline_class": "prior_period",
            "baseline_start": "2026-01-01",
            "baseline_end": "2026-03-31",
            "aggregation": "sum_of_complete_days",
        },
        decision_ledger=DecisionLedger(),
        require_physical_baseline=True,
    )

    result = _resolve_windows(
        authority,
        context_window_specs=(
            {
                "capability_id": "rolling_window_compare",
                "relation": "trailing_complete_periods",
                "unit": "month",
                "count": 1,
            },
        ),
    )
    windows = {window.window_id: window for window in result.windows}

    assert windows["target_day"].end_exclusive == "2026-07-01"
    assert windows["target_day"].required_complete_days == 91
    assert windows["baseline_window"].start_inclusive == "2026-01-01"
    assert windows["baseline_window"].end_exclusive == "2026-04-01"
    assert windows["baseline_window"].required_complete_days == 90
    context = next(window for window in result.windows if window.role == "reference")
    assert context.start_inclusive == "2026-03-01"
    assert context.end_exclusive == "2026-04-01"
    assert context.required_complete_days == 31


def test_calendar_partition_can_project_evaluation_range_as_capability_frame() -> None:
    authority = resolve_effective_comparison(
        time_spec={
            "kind": "date_range",
            "start": "2024-01-01",
            "end": "2026-06-30",
        },
        comparison_spec={
            "kind": "calendar_partition",
            "baseline_class": "same_month_phase",
            "period_grain": "month",
            "partition_field": "month_phase",
            "target_members": ["start"],
            "baseline_members": ["mid", "end"],
            "aggregation": "mean_of_complete_days",
            "member_definitions": [
                {"member": "start", "day_start": 1, "day_end": 10},
                {"member": "mid", "day_start": 11, "day_end": 20},
                {"member": "end", "day_start": 21, "day_end": 31},
            ],
        },
        decision_ledger=DecisionLedger(),
        require_physical_baseline=False,
    )

    result = _resolve_windows(
        authority,
        context_window_specs=(
            {
                "capability_id": "cross_source_association",
                "relation": "evaluation_range",
                "unit": "day",
                "count": 912,
            },
        ),
    )

    assert len(result.windows) == 2
    evaluation = result.windows[0]
    assert evaluation.window_id == "target_day"
    assert evaluation.start_inclusive == "2024-01-01"
    assert evaluation.end_exclusive == "2026-07-01"
    assert evaluation.required_complete_days == 912
    assert evaluation.aggregation == "mean_of_complete_days"
    observation_frame = result.windows[1]
    assert observation_frame.window_id == (
        "context__cross_source_association__evaluation_range__912_day"
    )
    assert observation_frame.role == "reference"
    assert observation_frame.start_inclusive == evaluation.start_inclusive
    assert observation_frame.end_exclusive == evaluation.end_exclusive
    assert observation_frame.required_complete_days == 912


def test_event_decision_owns_physical_windows_for_custom_time() -> None:
    decision = DecisionRecord.create(
        intent_revision_id="intent-event",
        slot_id="event_relative_window",
        value={
            "kind": "event_relative_window",
            "event_ref": "business-event:campaign-june-2026",
            "target_start": "2026-06-16",
            "target_end": "2026-06-30",
            "baseline_start": "2026-06-01",
            "baseline_end": "2026-06-15",
            "aggregation": "sum_of_complete_days",
        },
        source="user",
        status="user_confirmed",
        materiality="material",
        affected_plan_fields=("resolved_window_refs",),
        option_id="event-relative.user-window",
    )
    authority = resolve_effective_comparison(
        time_spec={"kind": "custom", "expression": "6月活动后"},
        comparison_spec={
            "kind": "decision_slot",
            "slot_id": "event_relative_window",
        },
        decision_ledger=DecisionLedger().append(decision),
        require_physical_baseline=True,
    )

    result = _resolve_windows(authority)
    windows = {window.window_id: window for window in result.windows}

    assert tuple(windows) == ("target_day", "baseline_window")
    assert windows["target_day"].start_inclusive == "2026-06-16"
    assert windows["target_day"].required_complete_days == 15
    assert windows["baseline_window"].start_inclusive == "2026-06-01"
    assert windows["baseline_window"].required_complete_days == 15


def test_unresolved_temporal_authority_fails_closed() -> None:
    authority = resolve_effective_comparison(
        time_spec={"kind": "custom", "expression": "活动后"},
        comparison_spec={
            "kind": "decision_slot",
            "slot_id": "event_relative_window",
        },
        decision_ledger=DecisionLedger(),
        require_physical_baseline=False,
    )

    with pytest.raises(
        WindowResolutionError,
        match="temporal_authority_unresolved",
    ):
        _resolve_windows(authority)


def test_freshness_uses_latest_required_end_across_all_execution_windows() -> None:
    authority = resolve_effective_comparison(
        time_spec={
            "kind": "date_range",
            "start": "2026-01-01",
            "end": "2026-01-31",
        },
        comparison_spec={
            "kind": "fixed_window",
            "baseline_class": "custom_control_window",
            "baseline_start": "2026-03-01",
            "baseline_end": "2026-03-15",
            "aggregation": "sum_of_complete_days",
        },
        decision_ledger=DecisionLedger(),
        require_physical_baseline=True,
    )

    result = _resolve_windows(authority, watermark=date(2026, 2, 28))

    assert len(result.gaps) == 1
    gap = result.gaps[0]
    assert "execution_windows:2026-03-15" in gap.gap_id
    assert gap.diagnostic_context["latest_required_business_date"] == "2026-03-15"
    assert gap.diagnostic_context["required_window_ids"] == (
        "target_day",
        "baseline_window",
    )


def test_temporal_authority_digest_is_verified_before_physical_resolution() -> None:
    authority = resolve_effective_comparison(
        time_spec={"kind": "date", "target": "2026-06-19"},
        comparison_spec={"kind": "none"},
        decision_ledger=DecisionLedger(),
        require_physical_baseline=False,
    )

    with pytest.raises(
        WindowResolutionError,
        match="temporal_authority_integrity_invalid",
    ):
        _resolve_windows(replace(authority, content_digest="0" * 64))


@pytest.mark.parametrize(
    "target_window",
    (
        lambda window: replace(
            window,
            window_ref="window:target:2026-06-20",
            start="2026-06-20",
            end="2026-06-20",
        ),
        lambda window: replace(window, aggregation="max"),
    ),
)
def test_resealed_temporal_semantic_drift_fails_before_resolution(
    target_window,
) -> None:
    authority = resolve_effective_comparison(
        time_spec={"kind": "date", "target": "2026-06-19"},
        comparison_spec={"kind": "none"},
        decision_ledger=DecisionLedger(),
        require_physical_baseline=False,
    )
    drifted = _reseal(
        replace(authority, target_window=target_window(authority.target_window))
    )

    with pytest.raises(
        WindowResolutionError,
        match="temporal_authority_shape_invalid",
    ):
        _resolve_windows(drifted)


def test_upper_date_bound_fails_with_typed_window_error() -> None:
    authority = resolve_effective_comparison(
        time_spec={"kind": "date", "target": "9999-12-31"},
        comparison_spec={"kind": "none"},
        decision_ledger=DecisionLedger(),
        require_physical_baseline=False,
    )

    with pytest.raises(
        WindowResolutionError,
        match="temporal_window_out_of_range:target",
    ):
        _resolve_windows(authority, watermark=date.max)


def test_context_window_underflow_fails_with_typed_window_error() -> None:
    authority = resolve_effective_comparison(
        time_spec={"kind": "date", "target": "0001-01-01"},
        comparison_spec={"kind": "none"},
        decision_ledger=DecisionLedger(),
        require_physical_baseline=False,
    )

    with pytest.raises(
        WindowResolutionError,
        match="context_window_out_of_range:rolling_window_compare",
    ):
        _resolve_windows(
            authority,
            context_window_specs=(
                {
                    "capability_id": "rolling_window_compare",
                    "relation": "trailing_complete_periods",
                    "unit": "day",
                    "count": 1,
                },
            ),
            watermark=date.max,
        )
