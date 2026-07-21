from __future__ import annotations

from types import SimpleNamespace

from bi_agent.capabilities.outlier_contribution import outlier_contribution
from bi_agent.capabilities.outlier_scan import outlier_scan
from bi_agent.runtime.analysis_contracts import (
    MetricBinding,
    QueryContract,
    ResolvedWindow,
    ResultShape,
    query_contract_signature,
)
from bi_agent.runtime.authoritative_task_inputs import _task_payload
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)


def _daily_query_contract() -> QueryContract:
    windows = (
        ResolvedWindow(
            window_id="target-window",
            role="target",
            label="2026-06-10..2026-06-12",
            start_inclusive="2026-06-10",
            end_exclusive="2026-06-13",
            timezone="Africa/Lagos",
            aggregation="sum_of_complete_days",
            required_complete_days=3,
            source_watermark_requirement="2026-06-12",
        ),
        ResolvedWindow(
            window_id="baseline-window",
            role="baseline",
            label="2026-05-01..2026-05-02",
            start_inclusive="2026-05-01",
            end_exclusive="2026-05-03",
            timezone="Africa/Lagos",
            aggregation="sum_of_complete_days",
            required_complete_days=2,
            source_watermark_requirement="2026-05-02",
        ),
        ResolvedWindow(
            window_id="reference-window",
            role="reference",
            label="2026-06-03..2026-06-09",
            start_inclusive="2026-06-03",
            end_exclusive="2026-06-10",
            timezone="Africa/Lagos",
            aggregation="sum_of_complete_days",
            required_complete_days=7,
            source_watermark_requirement="2026-06-09",
            capability_refs=("outlier_scan",),
        ),
    )
    metric = MetricBinding(
        metric_id="paid_amount",
        contract_ref="contract:paid-amount",
        dataset_id="paid_order_success",
        expression="sum(paid_amount)",
        aggregation="sum",
        required_fields=("paid_amount",),
        grain=("window_id", "observation_key"),
    )
    logical = {
        "analysis_contract_ref": "analysis:outlier-materializer",
        "query_intent": "daily_metric_baselines",
        "dataset_snapshot_refs": ("snapshot:paid:r1",),
        "metric_bindings": (metric,),
        "dimension_bindings": (),
        "window_refs": tuple(window.window_id for window in windows),
        "resolved_windows": windows,
        "filters": (),
        "result_shape": ResultShape(
            required_fields=(
                "window_id",
                "window_role",
                "observation_key",
                "paid_amount",
            ),
            unique_key=("window_id", "observation_key"),
            grain=("window_id", "observation_key"),
            required_window_ids=tuple(window.window_id for window in windows),
        ),
        "completeness_assertions": (
            "required_fields_present",
            "required_windows_complete",
        ),
        "workload_class": "interactive_aggregate",
    }
    return QueryContract(
        query_contract_id="query:outlier-materializer",
        contract_signature=query_contract_signature(logical),
        **logical,
    )


def _daily_rows() -> tuple[dict[str, object], ...]:
    return (
        {
            "window_id": "baseline-window",
            "window_role": "baseline",
            "observation_key": "2026-05-01",
            "paid_amount": 8.0,
        },
        {
            "window_id": "baseline-window",
            "window_role": "baseline",
            "observation_key": "2026-05-02",
            "paid_amount": 12.0,
        },
        *(
            {
                "window_id": "reference-window",
                "window_role": "reference",
                "observation_key": f"2026-06-{day:02d}",
                "paid_amount": 10.0,
            }
            for day in range(3, 10)
        ),
        {
            "window_id": "target-window",
            "window_role": "target",
            "observation_key": "2026-06-10",
            "paid_amount": 12.0,
        },
        {
            "window_id": "target-window",
            "window_role": "target",
            "observation_key": "2026-06-11",
            "paid_amount": 9.0,
        },
        {
            "window_id": "target-window",
            "window_role": "target",
            "observation_key": "2026-06-12",
            "paid_amount": 20.0,
        },
    )


def _materialized_payload(capability_id: str):
    contract = _daily_query_contract()
    rows = _daily_rows()
    axis = SimpleNamespace(
        axis_id="anomaly_validation",
        analysis_axis_ref="analysis-axis:anomaly-validation",
        target_metric_refs=("paid_amount",),
    )
    task = SimpleNamespace(
        task_id=f"task:{capability_id}",
        capability_id=capability_id,
        normalized_input_refs=(axis.analysis_axis_ref,),
    )
    plan = SimpleNamespace(analysis_axes=(axis,))
    intent = SimpleNamespace(target_metric_refs=("paid_amount",))
    slot = SimpleNamespace(
        slot_id="daily_metric_baselines",
        query_contract_refs=(contract.query_contract_id,),
    )
    execution_plan = SimpleNamespace(
        required_input_slots=(slot,),
        optional_input_slots=(),
    )
    bound = SimpleNamespace(
        rows_by_slot={slot.slot_id: rows},
        query_contract_refs=(contract.query_contract_id,),
    )
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    return _task_payload(
        plan=plan,
        task=task,
        intent=intent,
        bound=bound,
        execution_plan=execution_plan,
        query_by_ref={contract.query_contract_id: contract},
        result_by_query={},
        report_by_query={},
        registry=registry,
    )


def test_outlier_scan_payload_preserves_daily_rows_and_target_scope_fields() -> None:
    payload = _materialized_payload("outlier_scan")

    assert payload == {
        "rows": _daily_rows(),
        "value_key": "paid_amount",
        "period_key": "observation_key",
        "group_key": "window_role",
        "target_group": "target",
        "reference_group": "reference",
        "min_reference_samples": 7,
        "mad_threshold": 6.0,
    }
    evidence = outlier_scan(**payload)
    assert evidence.typed_payload["target_period_count"] == 3
    assert evidence.typed_payload["reference_period_count"] == 7
    assert evidence.typed_payload["excluded_other_group_period_count"] == 2


def test_outlier_contribution_payload_preserves_unequal_daily_windows() -> None:
    payload = _materialized_payload("outlier_contribution")

    assert payload["rows"] == _daily_rows()
    assert payload["period_key"] == "observation_key"
    assert payload["period_grain"] == "day"
    assert payload["group_key"] == "window_role"
    assert payload["target_group"] == "target"
    assert payload["baseline_group"] == "baseline"
    assert payload["amount_key"] == "paid_amount"
    assert all("comparison_id" not in row for row in payload["rows"])
    evidence = outlier_contribution(**payload)
    assert evidence.typed_payload["target_period_count"] == 3
    assert evidence.typed_payload["baseline_period_count"] == 2
    assert evidence.typed_payload["baseline_daily_mean"] == 10.0
    assert evidence.typed_payload["total_delta"] == 11.0
