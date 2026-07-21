from dataclasses import replace

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
