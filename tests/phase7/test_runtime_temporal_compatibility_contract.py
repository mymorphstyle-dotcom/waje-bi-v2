from copy import deepcopy
from pathlib import Path

import pytest

from bi_agent.runtime.contracts import load_contract
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)
from tools.data.load_gameplay_events_clickhouse import (
    RUNTIME_BINDING_REF as GAMEPLAY_RUNTIME_BINDING_REF,
)
from tools.data.load_market_dashboard_clickhouse import (
    RUNTIME_BINDING_REF as MARKET_RUNTIME_BINDING_REF,
)
from tools.data.register_existing_paid_success_snapshot import (
    RUNTIME_BINDING_REF as PAID_SUCCESS_RUNTIME_BINDING_REF,
)


ROOT = Path(__file__).resolve().parents[2]
SOURCE_BINDING_CONTRACTS = (
    "contracts/sources/external-events.source.yaml",
    "contracts/sources/gameplay.source.yaml",
    "contracts/sources/internal-operation-events.source.yaml",
    "contracts/sources/market-dashboard.source.yaml",
)


SAFE_TEMPORAL_COMPATIBILITY = {
    "cross_source_association": {
        "modes": [
            "target_only",
            "single_day_window_pair",
            "aggregate_window_pair",
            "event_relative",
        ],
        "window_roles": ["reference"],
        "consumption_semantics": ["daily_series", "capability_context"],
        "calendar_partition_fields": [],
    },
    "cross_source_panel_association": {
        "modes": [
            "target_only",
            "single_day_window_pair",
            "aggregate_window_pair",
            "event_relative",
        ],
        "window_roles": ["reference"],
        "consumption_semantics": ["daily_series", "capability_context"],
        "calendar_partition_fields": [],
    },
    "market_health_compare": {
        "modes": [
            "single_day_window_pair",
            "aggregate_window_pair",
            "event_relative",
        ],
        "window_roles": ["target", "baseline"],
        "consumption_semantics": ["complete_day_sum_or_mean"],
        "calendar_partition_fields": [],
    },
    "market_channel_context": {
        "modes": [
            "target_only",
            "single_day_window_pair",
            "aggregate_window_pair",
            "calendar_partition",
            "event_relative",
        ],
        "window_roles": ["target", "baseline"],
        "consumption_semantics": ["evaluation_window"],
        "calendar_partition_fields": [],
    },
    "source_reconciliation": {
        "modes": [
            "target_only",
            "single_day_window_pair",
            "aggregate_window_pair",
            "calendar_partition",
            "event_relative",
        ],
        "window_roles": ["target", "baseline"],
        "consumption_semantics": ["evaluation_window"],
        "calendar_partition_fields": [],
    },
    "compare_periods": {
        "modes": [
            "single_day_window_pair",
            "aggregate_window_pair",
            "event_relative",
        ],
        "window_roles": ["target", "baseline"],
        "consumption_semantics": ["complete_day_sum_or_mean"],
        "calendar_partition_fields": [],
    },
    "rolling_window_compare": {
        "modes": [
            "single_day_window_pair",
            "aggregate_window_pair",
            "event_relative",
        ],
        "window_roles": ["target", "reference"],
        "consumption_semantics": ["daily_series", "capability_context"],
        "calendar_partition_fields": [],
    },
    "data_quality_profile": {
        "modes": [
            "target_only",
            "single_day_window_pair",
            "aggregate_window_pair",
            "calendar_partition",
            "event_relative",
        ],
        "window_roles": ["target", "baseline"],
        "consumption_semantics": ["evaluation_window"],
        "calendar_partition_fields": [],
    },
    "event_evidence": {
        "modes": [
            "target_only",
            "single_day_window_pair",
            "aggregate_window_pair",
            "calendar_partition",
            "event_relative",
        ],
        "window_roles": ["target", "baseline"],
        "consumption_semantics": ["evaluation_window"],
        "calendar_partition_fields": [],
    },
    "event_window_compare": {
        "modes": ["event_relative"],
        "window_roles": ["target", "baseline"],
        "consumption_semantics": ["complete_day_sum_or_mean"],
        "calendar_partition_fields": [],
    },
    "formula_decompose": {
        "modes": [
            "single_day_window_pair",
            "aggregate_window_pair",
            "event_relative",
        ],
        "window_roles": ["target", "baseline"],
        "consumption_semantics": ["source_window_aggregate"],
        "calendar_partition_fields": [],
    },
    "outlier_scan": {
        "modes": [
            "target_only",
            "single_day_window_pair",
            "aggregate_window_pair",
            "event_relative",
        ],
        "window_roles": ["target", "reference"],
        "consumption_semantics": ["daily_series", "capability_context"],
        "calendar_partition_fields": [],
    },
    "outlier_contribution": {
        "modes": [
            "single_day_window_pair",
            "aggregate_window_pair",
            "event_relative",
        ],
        "window_roles": ["target", "baseline"],
        "consumption_semantics": ["daily_series"],
        "calendar_partition_fields": [],
    },
    "metric_coverage_profile": {
        "modes": [
            "target_only",
            "single_day_window_pair",
            "aggregate_window_pair",
            "calendar_partition",
            "event_relative",
        ],
        "window_roles": ["target", "baseline"],
        "consumption_semantics": ["evaluation_window"],
        "calendar_partition_fields": [],
    },
    "metric_timeseries": {
        "modes": [
            "target_only",
            "single_day_window_pair",
            "aggregate_window_pair",
            "calendar_partition",
            "event_relative",
        ],
        "window_roles": ["target", "baseline"],
        "consumption_semantics": ["daily_series"],
        "calendar_partition_fields": [],
    },
    "compare_period_phases": {
        "modes": ["calendar_partition"],
        "window_roles": ["target"],
        "consumption_semantics": ["daily_series", "partition_members"],
        "calendar_partition_fields": ["month_phase"],
    },
    "weekday_calendar_compare": {
        "modes": ["calendar_partition"],
        "window_roles": ["target"],
        "consumption_semantics": ["daily_series", "partition_members"],
        "calendar_partition_fields": ["iso_weekday"],
    },
    "candidate_dimension_screen": {
        "modes": [
            "single_day_window_pair",
            "aggregate_window_pair",
            "event_relative",
        ],
        "window_roles": ["target", "baseline"],
        "consumption_semantics": ["source_window_aggregate"],
        "calendar_partition_fields": [],
    },
    "change_point_scan": {
        "modes": [
            "target_only",
            "single_day_window_pair",
            "aggregate_window_pair",
            "event_relative",
        ],
        "window_roles": ["reference"],
        "consumption_semantics": ["daily_series", "capability_context"],
        "calendar_partition_fields": [],
    },
}


def _payload() -> dict:
    return load_contract(CANONICAL_RUNTIME_BINDINGS_PATH)


def test_canonical_temporal_compatibility_only_opens_audited_capabilities() -> None:
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)

    actual = {
        capability_id: registry.capability_inputs(capability_id)[
            "temporal_compatibility"
        ]
        for capability_id in registry.capability_ids
        if "temporal_compatibility" in registry.capability_inputs(capability_id)
    }

    assert registry.contract_version == "15"
    assert actual == SAFE_TEMPORAL_COMPATIBILITY


def test_window_source_reconciliation_policy_is_versioned_and_fail_closed() -> None:
    payload = load_contract(CANONICAL_RUNTIME_BINDINGS_PATH)
    registry = RuntimeContractRegistry(payload)
    contract = registry.capability_inputs("source_reconciliation")
    parameters = contract["task_input_binding"]["parameters"]

    assert contract["evidence_contract"] == (
        "bounded-window-source-reconciliation.v1"
    )
    assert parameters["bounded_window_relative_tolerance"] == 0.002
    assert parameters["bounded_change_residual_share"] == 0.01
    assert parameters["hard_observation_relative_limit"] == 0.01
    assert contract["minimum_readiness"]["accepted_completeness"] == ["complete"]

    drifted = deepcopy(payload)
    drifted["capability_inputs"]["source_reconciliation"]["task_input_binding"][
        "parameters"
    ]["hard_observation_relative_limit"] = 0.001
    with pytest.raises(
        ValueError,
        match="runtime_window_reconciliation_contract_invalid:source_reconciliation",
    ):
        RuntimeContractRegistry(drifted)


def test_source_load_bindings_follow_the_current_runtime_contract() -> None:
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    expected_ref = (
        "contracts/runtime/clickhouse-analysis-bindings.yaml@"
        + registry.contract_version
    )

    for relative_path in SOURCE_BINDING_CONTRACTS:
        assert (
            load_contract(ROOT / relative_path)["runtime_binding"]["binding_ref"]
            == expected_ref
        )
    assert {
        GAMEPLAY_RUNTIME_BINDING_REF,
        MARKET_RUNTIME_BINDING_REF,
        PAID_SUCCESS_RUNTIME_BINDING_REF,
    } == {expected_ref}


def test_temporal_compatibility_requires_exact_shape() -> None:
    payload = _payload()
    del payload["capability_inputs"]["compare_periods"]["temporal_compatibility"][
        "window_roles"
    ]

    with pytest.raises(
        ValueError,
        match=(
            "runtime_capability_temporal_compatibility_shape_invalid:compare_periods"
        ),
    ):
        RuntimeContractRegistry(payload)


@pytest.mark.parametrize(
    ("semantics", "error"),
    (
        (
            ["complete_day_sum_or_mean", "unknown_semantic"],
            "runtime_capability_temporal_consumption_semantics_invalid:compare_periods",
        ),
        (
            ["capability_context"],
            "runtime_capability_temporal_result_grain_semantics_invalid:"
            "compare_periods",
        ),
    ),
)
def test_temporal_compatibility_rejects_unknown_or_missing_result_semantics(
    semantics: list[str],
    error: str,
) -> None:
    payload = _payload()
    payload["capability_inputs"]["compare_periods"]["temporal_compatibility"][
        "consumption_semantics"
    ] = semantics

    with pytest.raises(ValueError, match=error):
        RuntimeContractRegistry(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("modes", ["single_day_window_pair", "single_day_window_pair"]),
        ("window_roles", ["target", "unknown_role"]),
        (
            "calendar_partition_fields",
            ["month_phase", "month_phase"],
        ),
    ),
)
def test_temporal_compatibility_rejects_duplicate_or_unknown_values(
    field: str,
    value: list[str],
) -> None:
    payload = _payload()
    payload["capability_inputs"]["compare_periods"]["temporal_compatibility"][field] = (
        value
    )

    with pytest.raises(
        ValueError,
        match=f"runtime_capability_temporal_{field}_invalid:compare_periods",
    ):
        RuntimeContractRegistry(payload)


def test_capability_context_owns_only_reference_windows() -> None:
    payload = _payload()
    payload["capability_inputs"]["change_point_scan"]["temporal_compatibility"][
        "window_roles"
    ] = ["target", "reference"]

    with pytest.raises(
        ValueError,
        match=(
            "runtime_capability_temporal_context_ownership_invalid:change_point_scan"
        ),
    ):
        RuntimeContractRegistry(payload)


def test_partition_members_require_one_supported_calendar_field() -> None:
    payload = _payload()
    payload["capability_inputs"]["compare_period_phases"]["temporal_compatibility"][
        "calendar_partition_fields"
    ] = []

    with pytest.raises(
        ValueError,
        match=(
            "runtime_capability_temporal_partition_members_invalid:"
            "compare_period_phases"
        ),
    ):
        RuntimeContractRegistry(payload)


def test_source_window_aggregate_requires_complete_window_query_shape() -> None:
    payload = _payload()
    payload["query_shapes"]["component_driver_scan"]["result_semantics"] = (
        "complete_context_rows"
    )

    with pytest.raises(
        ValueError,
        match=(
            "runtime_capability_temporal_aggregate_shape_invalid:"
            "formula_decompose:component_driver_scan"
        ),
    ):
        RuntimeContractRegistry(payload)
