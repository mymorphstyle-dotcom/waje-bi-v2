from types import SimpleNamespace

import pytest

from bi_agent.capabilities.pattern_scan import scan_pattern
from bi_agent.runtime.authoritative_task_inputs import (
    AuthoritativeTaskInputContractError,
    _pattern_payload,
)
from bi_agent.runtime.plan_compiler import AuthoritativePlanCompiler
from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry
from bi_agent.runtime.single_authority import DecisionLedger
from bi_agent.runtime.temporal_comparison import (
    resolve_effective_comparison,
    resolve_rolling_window_strategy,
)


def _window_pair_temporal_authority(
    *,
    start: str,
    end: str | None = None,
    baseline_start: str,
    baseline_end: str | None = None,
):
    comparison_end = baseline_end or baseline_start
    return resolve_effective_comparison(
        time_spec=(
            {"kind": "date", "target": start}
            if end is None
            else {"kind": "date_range", "start": start, "end": end}
        ),
        comparison_spec={
            "kind": "fixed_window",
            "baseline_class": "prior_period",
            "baseline_start": baseline_start,
            "baseline_end": comparison_end,
            "aggregation": "sum_of_complete_days",
        },
        decision_ledger=DecisionLedger(),
        require_physical_baseline=True,
    )


def _daily_rows(
    role: str,
    dates: tuple[str, ...],
    values: tuple[float, ...],
) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "observation_key": observed_on,
            "window_role": role,
            "value": value,
        }
        for observed_on, value in zip(dates, values, strict=True)
    )


def _scan(rows: tuple[dict[str, object], ...]):
    return scan_pattern(
        rows,
        pattern_family="rolling",
        observation_key="observation_key",
        window_role_key="window_role",
        target_role="target",
        baseline_role="baseline",
        value_key="value",
        rolling_span_days=3,
        rolling_step_days=2,
        min_periods=2,
        materiality_floor=0.0,
    )


def test_rolling_compare_pairs_window_local_means_by_relative_ordinal() -> None:
    rows = (
        *_daily_rows(
            "target",
            (
                "2026-06-10",
                "2026-06-11",
                "2026-06-12",
                "2026-06-13",
                "2026-06-14",
                "2026-06-15",
            ),
            (110, 120, 130, 140, 150, 160),
        ),
        *_daily_rows(
            "baseline",
            (
                "2026-05-01",
                "2026-05-02",
                "2026-05-03",
                "2026-05-04",
                "2026-05-05",
                "2026-05-06",
                "2026-05-07",
            ),
            (100, 100, 100, 100, 100, 100, 100),
        ),
    )

    result = _scan(rows)

    assert result.evidence_type == "statistical_association"
    assert result.comparable_periods == 2
    assert result.typed_payload["pairing_semantics"] == "relative_ordinal"
    assert result.typed_payload["target_rolling_periods"] == 2
    assert result.typed_payload["baseline_rolling_periods"] == 3
    pairs = result.typed_payload["rolling_pairs"]
    assert tuple(item["relative_index"] for item in pairs) == (0, 1)
    assert pairs[0]["target_start"] == "2026-06-10"
    assert pairs[0]["baseline_start"] == "2026-05-01"
    assert pairs[0]["target_mean"] == pytest.approx(120.0)
    assert pairs[1]["target_mean"] == pytest.approx(140.0)
    assert any(
        item["reason"] == "unpaired_rolling_periods"
        and item["window_role"] == "baseline"
        and item["count"] == 1
        for item in result.exceptions
    )


def test_single_day_pair_has_explicit_insufficient_evidence_boundary() -> None:
    result = _scan(
        (
            *_daily_rows("target", ("2026-06-10",), (120,)),
            *_daily_rows("baseline", ("2026-05-01",), (100,)),
        )
    )

    assert result.evidence_type == "insufficient_evidence"
    assert result.strength == "insufficient"
    assert result.wording_limit == "insufficient"
    assert result.comparable_periods == 0
    assert "no_comparable_periods" in result.limitations
    assert {(item["window_role"], item["reason"]) for item in result.exceptions} == {
        ("target", "insufficient_contiguous_days"),
        ("baseline", "insufficient_contiguous_days"),
    }


def test_one_complete_rolling_pair_remains_insufficient_for_min_periods() -> None:
    result = _scan(
        (
            *_daily_rows(
                "target",
                ("2026-06-10", "2026-06-11", "2026-06-12"),
                (110, 120, 130),
            ),
            *_daily_rows(
                "baseline",
                ("2026-05-01", "2026-05-02", "2026-05-03"),
                (100, 100, 100),
            ),
        )
    )

    assert result.comparable_periods == 1
    assert result.evidence_type == "insufficient_evidence"
    assert result.strength == "insufficient"
    assert "insufficient_comparable_periods" in result.limitations


def test_rolling_parameters_are_mandatory_and_positive() -> None:
    with pytest.raises(ValueError, match="min_periods is required"):
        scan_pattern((), pattern_family="rolling")
    with pytest.raises(ValueError, match="rolling_span_days"):
        scan_pattern((), pattern_family="rolling", min_periods=2)


def test_authoritative_rolling_payload_builds_adjacent_series_from_context() -> None:
    contract = SimpleNamespace(
        query_intent="daily_metric_baselines",
        query_contract_id="query:rolling",
        result_shape=SimpleNamespace(
            required_fields=("window_id", "window_role", "observation_key")
        ),
        metric_bindings=(SimpleNamespace(metric_id="paid_amount"),),
        dimension_bindings=(),
    )
    binding = {
        "pattern_mode": "rolling",
        "query_families": {"primary": "daily_metric_baselines"},
        "fields": {
            "observation_key": "observation_key",
            "window_role_key": "window_role",
            "target_role": "target",
            "baseline_role": "baseline",
            "context_role": "reference",
            "value_key": "value",
        },
        "parameters": {
            "materiality_floor": 0.0,
            "rolling_span_policy": "target_window_duration_with_minimum",
            "minimum_span_days": 3,
            "rolling_step_policy": "target_window_duration",
            "min_periods": 2,
        },
    }
    rows = (
        {
            "window_id": "context_window",
            "window_role": "reference",
            "observation_key": "2026-06-06",
            "paid_amount": 100,
        },
        {
            "window_id": "context_window",
            "window_role": "reference",
            "observation_key": "2026-06-07",
            "paid_amount": 110,
        },
        {
            "window_id": "context_window",
            "window_role": "reference",
            "observation_key": "2026-06-08",
            "paid_amount": 120,
        },
        {
            "window_id": "context_window",
            "window_role": "reference",
            "observation_key": "2026-06-09",
            "paid_amount": 130,
        },
        {
            "window_id": "target_window",
            "window_role": "target",
            "observation_key": "2026-06-10",
            "paid_amount": 140,
        },
    )

    payload = _pattern_payload(
        capability_id="rolling_window_compare",
        rows=rows,
        contracts=(contract,),
        metric_id="paid_amount",
        binding=binding,
        temporal_authority=_window_pair_temporal_authority(
            start="2026-06-10",
            baseline_start="2026-06-09",
        ),
    )

    assert payload["rows"] == (
        {
            "observation_key": "2026-06-07",
            "window_role": "target",
            "value": 110,
        },
        {
            "observation_key": "2026-06-08",
            "window_role": "target",
            "value": 120,
        },
        {
            "observation_key": "2026-06-09",
            "window_role": "target",
            "value": 130,
        },
        {
            "observation_key": "2026-06-10",
            "window_role": "target",
            "value": 140,
        },
        {
            "observation_key": "2026-06-06",
            "window_role": "baseline",
            "value": 100,
        },
        {
            "observation_key": "2026-06-07",
            "window_role": "baseline",
            "value": 110,
        },
        {
            "observation_key": "2026-06-08",
            "window_role": "baseline",
            "value": 120,
        },
        {
            "observation_key": "2026-06-09",
            "window_role": "baseline",
            "value": 130,
        },
    )
    assert "comparison_id" not in payload["rows"][0]
    result = scan_pattern(
        payload["rows"],
        pattern_family="rolling",
        observation_key=payload["observation_key"],
        window_role_key=payload["window_role_key"],
        target_role=payload["target_role"],
        baseline_role=payload["baseline_role"],
        value_key=payload["value_key"],
        rolling_span_days=payload["rolling_span_days"],
        rolling_step_days=payload["rolling_step_days"],
        min_periods=payload["min_periods"],
        materiality_floor=payload["materiality_floor"],
    )
    assert result.comparable_periods == 2


def test_authoritative_calendar_pattern_payload_carries_accepted_aggregation() -> None:
    contract = SimpleNamespace(
        query_intent="time_bucket_scan",
        query_contract_id="query:month-phase",
        result_shape=SimpleNamespace(
            required_fields=(
                "window_role",
                "month_phase",
                "observation_key",
                "paid_amount",
            )
        ),
        metric_bindings=(SimpleNamespace(metric_id="paid_amount"),),
        dimension_bindings=(),
    )
    binding = {
        "pattern_mode": "intra_period",
        "query_families": {"primary": "time_bucket_scan"},
        "fields": {
            "window_role_key": "window_role",
            "phase_key": "month_phase",
            "observation_key": "observation_key",
            "period_key": "calendar_month",
            "value_key": "amount",
        },
        "parameters": {"materiality_floor": 0.0, "min_periods": 2},
    }
    authority = resolve_effective_comparison(
        time_spec={
            "kind": "date_range",
            "start": "2024-01-01",
            "end": "2024-02-29",
        },
        comparison_spec={
            "kind": "calendar_partition",
            "baseline_class": "prior_period",
            "period_grain": "month",
            "partition_field": "month_phase",
            "target_members": ["start"],
            "baseline_members": ["end"],
            "aggregation": "sum_of_complete_days",
            "member_definitions": [
                {"member": "start", "day_start": 1, "day_end": 5},
                {"member": "mid", "day_start": 6, "day_end": 24},
                {"member": "end", "day_start": 25, "day_end": 31},
            ],
        },
        decision_ledger=DecisionLedger(),
        require_physical_baseline=False,
    )

    payload = _pattern_payload(
        capability_id="compare_period_phases",
        rows=(
            {
                "window_role": "target",
                "month_phase": "start",
                "observation_key": "2024-01-01",
                "paid_amount": 100,
            },
            {
                "window_role": "target",
                "month_phase": "end",
                "observation_key": "2024-01-31",
                "paid_amount": 120,
            },
        ),
        contracts=(contract,),
        metric_id="paid_amount",
        binding=binding,
        temporal_authority=authority,
    )

    assert payload["aggregation"] == "sum_of_complete_days"
    assert payload["baseline_class"] == "prior_period"
    assert payload["period_grain"] == "month"


def test_authoritative_rolling_payload_rejects_primary_baseline_rows() -> None:
    contract = SimpleNamespace(
        query_intent="daily_metric_baselines",
        query_contract_id="query:rolling",
        result_shape=SimpleNamespace(
            required_fields=("window_role", "observation_key")
        ),
        metric_bindings=(SimpleNamespace(metric_id="paid_amount"),),
        dimension_bindings=(),
    )
    binding = {
        "pattern_mode": "rolling",
        "query_families": {"primary": "daily_metric_baselines"},
        "fields": {
            "observation_key": "observation_key",
            "window_role_key": "window_role",
            "target_role": "target",
            "baseline_role": "baseline",
            "context_role": "reference",
            "value_key": "value",
        },
        "parameters": {
            "materiality_floor": 0.0,
            "rolling_span_policy": "target_window_duration_with_minimum",
            "minimum_span_days": 3,
            "rolling_step_policy": "target_window_duration",
            "min_periods": 2,
        },
    }

    with pytest.raises(
        AuthoritativeTaskInputContractError,
        match="authoritative_rolling_window_role_invalid",
    ):
        _pattern_payload(
            capability_id="rolling_window_compare",
            rows=(
                {
                    "window_role": "baseline",
                    "observation_key": "2026-05-01",
                    "paid_amount": 100,
                },
            ),
            contracts=(contract,),
            metric_id="paid_amount",
            binding=binding,
            temporal_authority=_window_pair_temporal_authority(
                start="2026-06-10",
                baseline_start="2026-06-09",
            ),
        )


def test_runtime_contract_declares_daily_series_and_noncausal_ceiling() -> None:
    registry = RuntimeContractRegistry.from_path(
        "contracts/runtime/clickhouse-analysis-bindings.yaml"
    )
    contract = registry.capability_inputs("rolling_window_compare")

    assert contract["temporal_compatibility"] == {
        "modes": [
            "single_day_window_pair",
            "aggregate_window_pair",
            "event_relative",
        ],
        "window_roles": ["target", "reference"],
        "consumption_semantics": ["daily_series", "capability_context"],
        "calendar_partition_fields": [],
    }
    assert contract["task_input_binding"]["parameters"] == {
        "materiality_floor": 0.0,
        "rolling_span_policy": "target_window_duration_with_minimum",
        "minimum_span_days": 3,
        "rolling_step_policy": "target_window_duration",
        "min_periods": 8,
    }
    assert contract["supported_evidence_types"] == [
        "statistical_association",
        "insufficient_evidence",
    ]
    assert contract["maximum_claim_strength"] == "directional"
    assert contract["context_window_policy"]["execution_default"] == {
        "unit": "day",
        "count": 10,
    }


@pytest.mark.parametrize(
    ("authority", "expected"),
    (
        (
            _window_pair_temporal_authority(
                start="2026-06-10",
                baseline_start="2026-06-09",
            ),
            (3, 1, 10),
        ),
        (
            _window_pair_temporal_authority(
                start="2026-06-01",
                end="2026-06-07",
                baseline_start="2026-05-25",
                baseline_end="2026-05-31",
            ),
            (7, 7, 56),
        ),
        (
            _window_pair_temporal_authority(
                start="2026-05-01",
                end="2026-05-31",
                baseline_start="2026-04-01",
                baseline_end="2026-04-30",
            ),
            (31, 31, 248),
        ),
        (
            _window_pair_temporal_authority(
                start="2026-05-03",
                end="2026-05-12",
                baseline_start="2026-04-23",
                baseline_end="2026-05-02",
            ),
            (10, 10, 80),
        ),
    ),
)
def test_rolling_strategy_follows_typed_target_duration(authority, expected) -> None:
    registry = RuntimeContractRegistry.from_path(
        "contracts/runtime/clickhouse-analysis-bindings.yaml"
    )
    contract = registry.capability_inputs("rolling_window_compare")

    strategy = resolve_rolling_window_strategy(
        authority,
        parameters=contract["task_input_binding"]["parameters"],
        maximum_context_days=contract["context_window_policy"]["count_bounds"][
            "day"
        ][1],
    )

    assert (
        strategy.rolling_span_days,
        strategy.rolling_step_days,
        strategy.context_days,
    ) == expected
    assert strategy.min_periods == 8
    assert strategy.context_limited is False
    specs = AuthoritativePlanCompiler(
        runtime_registry=registry
    )._compile_context_window_specs(
        ({"capability_id": "rolling_window_compare"},),
        temporal_authority=authority,
    )
    assert len(specs) == 1
    assert (specs[0].unit, specs[0].count) == ("day", expected[2])
