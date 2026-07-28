from dataclasses import replace
from datetime import date, timedelta

from bi_agent.runtime.analysis_contracts import (
    MetricBinding,
    QueryContract,
    ResolvedWindow,
    ResultShape,
    query_contract_signature,
)
from bi_agent.runtime.window_metric_evidence import (
    aggregate_window_metric_comparison,
)


def test_explicit_primary_baseline_is_independent_of_signed_query_order():
    windows = (
        _window("target_day", "target", "2026-06-01", "2026-06-02"),
        _window("previous_week", "baseline", "2026-05-25", "2026-05-26"),
        _window("previous_day", "baseline", "2026-05-31", "2026-06-01"),
    )
    contract = QueryContract(
        query_contract_id="query:primary-window-order",
        analysis_contract_ref="analysis:primary-window-order",
        query_intent="daily_metric_baselines",
        dataset_snapshot_refs=("snapshot:market:1",),
        metric_bindings=(
            MetricBinding(
                metric_id="active_users",
                contract_ref="metric:active-users@1",
                dataset_id="market_dashboard",
                expression="active_users",
                aggregation="sum",
                required_fields=("active_users",),
                grain=("business_date",),
            ),
        ),
        dimension_bindings=(),
        window_refs=tuple(window.window_id for window in windows),
        resolved_windows=windows,
        filters=(),
        result_shape=ResultShape(
            required_fields=(
                "window_id",
                "window_role",
                "observation_key",
                "active_users",
            ),
            unique_key=("window_id", "observation_key"),
            grain=("window_id", "observation_key"),
            required_window_ids=tuple(window.window_id for window in windows),
        ),
        completeness_assertions=(),
        workload_class="interactive_aggregate",
        contract_signature="",
    )
    contract = replace(
        contract,
        contract_signature=query_contract_signature(contract),
    )

    comparison = aggregate_window_metric_comparison(
        contract,
        (
            _row("previous_day", "baseline", "2026-05-31", 100),
            _row("previous_week", "baseline", "2026-05-25", 90),
            _row("target_day", "target", "2026-06-01", 120),
        ),
        metric_id="active_users",
        primary_baseline_window_id="previous_day",
    )

    assert comparison.primary_baseline.window_id == "previous_day"
    assert comparison.primary_baseline.value == 100
    assert (
        comparison.changes(
            comparison.target,
            comparison.primary_baseline,
        )["absolute_change"]
        == 20
    )
    payload = comparison.to_payload()
    assert payload["interpretation_contract"]["contract_id"] == (
        "window-metric-comparison-interpretation.v1"
    )
    assert payload["interpretation_contract"]["absolute_change_formula"] == (
        "target_value - baseline_value"
    )
    assert payload["interpretation_contract"]["zero_baseline_policy"] == (
        "relative_change_unavailable"
    )


def test_calendar_partition_role_frame_compares_derived_daily_groups():
    window = ResolvedWindow(
        window_id="target_day",
        role="target",
        label="2026-06",
        start_inclusive="2026-06-01",
        end_exclusive="2026-07-01",
        timezone="Africa/Lagos",
        aggregation="mean_of_complete_days",
        required_complete_days=30,
        source_watermark_requirement="2026-06-30",
    )
    frame = {
        "schema_version": "calendar-partition-role-frame.v1",
        "partition_field": "month_phase",
        "target_members": ("start",),
        "baseline_members": ("mid", "end"),
        "aggregation": "mean_of_complete_days",
        "member_definitions": (
            {"member": "start", "day_start": 1, "day_end": 10},
            {"member": "mid", "day_start": 11, "day_end": 20},
            {"member": "end", "day_start": 21, "day_end": 31},
        ),
    }
    contract = QueryContract(
        query_contract_id="query:partition-role-frame",
        analysis_contract_ref="analysis:partition-role-frame",
        query_intent="daily_metric_baselines",
        dataset_snapshot_refs=("snapshot:market:1",),
        metric_bindings=(
            MetricBinding(
                metric_id="active_users",
                contract_ref="metric:active-users@1",
                dataset_id="market_dashboard",
                expression="active_users",
                aggregation="sum",
                required_fields=("active_users",),
                grain=("business_date",),
            ),
        ),
        dimension_bindings=(),
        window_refs=("target_day",),
        resolved_windows=(window,),
        filters=(),
        result_shape=ResultShape(
            required_fields=(
                "window_id",
                "window_role",
                "observation_key",
                "active_users",
            ),
            unique_key=("window_id", "observation_key"),
            grain=("window_id", "observation_key"),
            required_window_ids=("target_day",),
        ),
        completeness_assertions=(),
        workload_class="interactive_aggregate",
        contract_signature="",
        query_parameters={"calendar_partition_role_frame": frame},
    )
    contract = replace(
        contract,
        contract_signature=query_contract_signature(contract),
    )
    start = date(2026, 6, 1)
    rows = tuple(
        {
            "window_id": "target_day",
            "window_role": "target" if offset < 10 else "baseline",
            "observation_key": (start + timedelta(days=offset)).isoformat(),
            "active_users": offset + 1,
        }
        for offset in range(30)
    )

    comparison = aggregate_window_metric_comparison(
        contract,
        rows,
        metric_id="active_users",
        primary_baseline_window_id="target_day:partition:baseline",
    )

    assert comparison.target.window_id == "target_day:partition:target"
    assert comparison.target.required_complete_days == 10
    assert comparison.target.value == 5.5
    assert comparison.primary_baseline.required_complete_days == 20
    assert comparison.primary_baseline.value == 20.5


def _window(
    window_id: str,
    role: str,
    start: str,
    end: str,
) -> ResolvedWindow:
    return ResolvedWindow(
        window_id=window_id,
        role=role,
        label=window_id,
        start_inclusive=start,
        end_exclusive=end,
        timezone="Africa/Lagos",
        aggregation="daily_total",
        required_complete_days=1,
        source_watermark_requirement=f"{end}T00:00:00+01:00",
    )


def _row(window_id: str, role: str, day: str, value: int) -> dict[str, object]:
    return {
        "window_id": window_id,
        "window_role": role,
        "observation_key": day,
        "active_users": value,
    }
