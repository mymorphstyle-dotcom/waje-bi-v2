from dataclasses import replace

from bi_agent.runtime.window_metric_evidence import (
    aggregate_window_metric_comparison,
)
from tests.phase4.test_market_window_evidence import (
    _market_context,
    _resign_contract,
)


def test_explicit_primary_baseline_is_independent_of_signed_query_order():
    context = _market_context()
    contract = context["contract"]
    reordered_windows = (
        contract.resolved_windows[0],
        contract.resolved_windows[2],
        contract.resolved_windows[1],
        *contract.resolved_windows[3:],
    )
    reordered_ids = tuple(window.window_id for window in reordered_windows)
    reordered = _resign_contract(
        contract,
        resolved_windows=reordered_windows,
        window_refs=reordered_ids,
        result_shape=replace(
            contract.result_shape,
            required_window_ids=reordered_ids,
        ),
    )

    comparison = aggregate_window_metric_comparison(
        reordered,
        tuple(reversed(context["result"].rows)),
        metric_id="active_users",
        primary_baseline_window_id="previous_day",
    )

    assert comparison.primary_baseline.window_id == "previous_day"
    assert comparison.primary_baseline.value == 100
    assert comparison.changes(
        comparison.target,
        comparison.primary_baseline,
    )["absolute_change"] == 20
