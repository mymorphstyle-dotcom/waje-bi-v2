from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from bi_agent.capabilities.change_point_scan import (
    ChangePointScanContractError,
    change_point_scan,
)
from bi_agent.capabilities.market_channel_context import (
    MarketChannelContextContractError,
    market_channel_context,
)
from bi_agent.capabilities.metric_coverage_profile import (
    MetricCoverageProfileContractError,
    metric_coverage_profile,
)
from bi_agent.capabilities.source_reconciliation import (
    SourceReconciliationContractError,
    source_reconciliation,
)
from bi_agent.runtime.analysis_contracts import (
    CapabilityExecutionPlan,
    CapabilityInputSlot,
    CompletenessReport,
    DimensionBinding,
    MetricBinding,
    QueryContract,
    QueryResultEnvelope,
    ResolvedWindow,
    ResultShape,
)
from bi_agent.runtime.analysis_contract_compiler import _snapshot_evidence_gaps
from bi_agent.runtime.authoritative_task_inputs import (
    AuthoritativeTaskInputContractError,
    _deduplicated_metric_timeseries_rows,
    _market_channel_context_payload,
    _metric_coverage_profile_payload,
    _source_reconciliation_payload,
)
from bi_agent.runtime.capability_execution import BoundCapabilityInput
from bi_agent.runtime.dataset_catalog import DatasetSnapshot
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)


def test_change_point_scan_detects_only_a_statistical_level_shift_candidate() -> None:
    rows = tuple(
        {"observation_key": f"2026-01-{day:02d}", "paid_amount": value}
        for day, value in reversed(
            tuple(enumerate((10, 10, 10, 10, 20, 20, 20, 20), start=1))
        )
    )

    result = change_point_scan(
        rows,
        time_key="observation_key",
        value_key="paid_amount",
        min_total_samples=8,
        min_segment_samples=4,
        min_relative_level_shift=0.4,
        min_standardized_level_shift=2.0,
        max_candidates=3,
        result_refs=("result:timeseries",),
    )

    assert result.evidence_type == "statistical_association"
    assert result.strength == "anomaly_candidate"
    assert result.wording_limit == "anomaly_candidate"
    assert result.typed_payload["claim_ceiling"] == "anomaly_candidate"
    assert result.typed_payload["causal_claim_allowed"] is False
    assert result.typed_payload["candidate_count"] == 1
    candidate = result.typed_payload["candidates"][0]
    assert candidate["right_start_time"] == "2026-01-05"
    assert candidate["left_mean"] == 10.0
    assert candidate["right_mean"] == 20.0
    assert candidate["zero_variance_shift"] is True
    assert result.result_refs == ("result:timeseries",)


def test_change_point_scan_returns_typed_insufficient_for_short_series() -> None:
    result = change_point_scan(
        ({"day": "2026-01-01", "value": 1},),
        time_key="day",
        value_key="value",
        min_total_samples=6,
        min_segment_samples=3,
        min_relative_level_shift=0.2,
        min_standardized_level_shift=2.0,
        max_candidates=2,
    )

    assert result.evidence_type == "insufficient_evidence"
    assert result.strength == "insufficient"
    assert result.typed_payload["candidate_count"] == 0
    assert result.limitations == ("insufficient_ordered_samples",)


def test_change_point_scan_rejects_malformed_series_and_hidden_thresholds() -> None:
    rows = (
        {"day": "2026-01-01", "value": 1},
        {"day": "2026-01-01", "value": 2},
        {"day": "2026-01-02", "value": 3},
        {"day": "2026-01-03", "value": 4},
    )
    with pytest.raises(
        ChangePointScanContractError,
        match="change_point_scan_time_duplicate",
    ):
        change_point_scan(
            rows,
            time_key="day",
            value_key="value",
            min_total_samples=4,
            min_segment_samples=2,
            min_relative_level_shift=0.2,
            min_standardized_level_shift=2.0,
            max_candidates=2,
        )

    with pytest.raises(
        ChangePointScanContractError,
        match="change_point_scan_min_total_samples_invalid",
    ):
        change_point_scan(
            (),
            time_key="day",
            value_key="value",
            min_total_samples=3,
            min_segment_samples=2,
            min_relative_level_shift=0.2,
            min_standardized_level_shift=2.0,
            max_candidates=2,
        )


def _coverage_record(
    *,
    result_ref: str,
    dataset_id: str,
    completeness_status: str = "complete",
    analysis_readiness: str = "ready",
    observed_days: int = 2,
) -> dict:
    return {
        "result_ref": result_ref,
        "dataset_id": dataset_id,
        "snapshot_refs": (f"snapshot:{dataset_id}:r1",),
        "completeness_report_ref": f"completeness:{result_ref}",
        "completeness_status": completeness_status,
        "analysis_readiness": analysis_readiness,
        "windows": (
            {
                "window_id": "target",
                "required_days": 2,
                "observed_days": observed_days,
            },
        ),
    }


def test_metric_coverage_profile_preserves_dataset_and_window_trust_boundaries() -> (
    None
):
    rows = (
        {
            "result_ref": "result:paid",
            "window_id": "target",
            "observation_key": "2026-01-01",
            "paid_amount": 10,
            "source_row_count": 3,
        },
        {
            "result_ref": "result:paid",
            "window_id": "target",
            "observation_key": "2026-01-02",
            "paid_amount": 20,
            "source_row_count": 4,
        },
        {
            "result_ref": "result:attempt",
            "window_id": "target",
            "observation_key": "2026-01-01",
            "paid_amount": None,
            "source_row_count": 2,
        },
    )
    result = metric_coverage_profile(
        rows,
        metric_id="paid_amount",
        value_key="paid_amount",
        result_ref_key="result_ref",
        window_id_key="window_id",
        observation_key="observation_key",
        source_row_count_key="source_row_count",
        coverage_records=(
            _coverage_record(result_ref="result:paid", dataset_id="paid_order_success"),
            _coverage_record(
                result_ref="result:attempt",
                dataset_id="payment_attempt",
                completeness_status="partial",
                analysis_readiness="degraded",
                observed_days=1,
            ),
        ),
        result_refs=("result:paid", "result:attempt"),
    )

    assert result.evidence_type == "trust_boundary"
    assert result.strength == "trust_boundary"
    assert result.typed_payload["claim_ceiling"] == "trust_boundary"
    profiles = {
        item["dataset_id"]: item for item in result.typed_payload["dataset_profiles"]
    }
    assert profiles["paid_order_success"]["coverage_state"] == "covered"
    assert profiles["paid_order_success"]["metric_non_null_ratio"] == 1.0
    assert profiles["payment_attempt"]["coverage_state"] == "limited"
    assert profiles["payment_attempt"]["metric_non_null_ratio"] == 0.0
    assert "coverage_limited:payment_attempt" in result.limitations


def test_metric_coverage_profile_uses_typed_insufficient_and_rejects_bad_links() -> (
    None
):
    empty = metric_coverage_profile(
        (),
        metric_id="paid_amount",
        value_key="paid_amount",
        result_ref_key="result_ref",
        window_id_key="window_id",
        observation_key="observation_key",
        source_row_count_key="source_row_count",
        coverage_records=(),
    )
    assert empty.evidence_type == "insufficient_evidence"
    assert empty.typed_payload["dataset_profiles"] == ()
    assert empty.limitations == ("coverage_evidence_absent",)

    with pytest.raises(
        MetricCoverageProfileContractError,
        match="metric_coverage_result_ref_unbound",
    ):
        metric_coverage_profile(
            (
                {
                    "result_ref": "result:unknown",
                    "window_id": "target",
                    "observation_key": "2026-01-01",
                    "paid_amount": 1,
                    "source_row_count": 1,
                },
            ),
            metric_id="paid_amount",
            value_key="paid_amount",
            result_ref_key="result_ref",
            window_id_key="window_id",
            observation_key="observation_key",
            source_row_count_key="source_row_count",
            coverage_records=(
                _coverage_record(
                    result_ref="result:paid", dataset_id="paid_order_success"
                ),
            ),
        )

    with pytest.raises(
        MetricCoverageProfileContractError,
        match="metric_coverage_window_row_count_mismatch",
    ):
        metric_coverage_profile(
            (
                {
                    "result_ref": "result:paid",
                    "window_id": "target",
                    "observation_key": "2026-01-01",
                    "paid_amount": 1,
                    "source_row_count": 1,
                },
            ),
            metric_id="paid_amount",
            value_key="paid_amount",
            result_ref_key="result_ref",
            window_id_key="window_id",
            observation_key="observation_key",
            source_row_count_key="source_row_count",
            coverage_records=(
                _coverage_record(
                    result_ref="result:paid",
                    dataset_id="paid_order_success",
                ),
            ),
        )


def _channel_completeness(
    *,
    status: str = "complete",
    readiness: str = "ready",
    reconciliation: str = "passed",
) -> dict:
    return {
        "result_ref": "result:channel",
        "completeness_report_ref": "completeness:channel",
        "completeness_status": status,
        "analysis_readiness": readiness,
        "reconciliation_status": reconciliation,
    }


def test_market_channel_context_reports_coverage_without_attribution() -> None:
    result = market_channel_context(
        (
            {
                "window_id": "target",
                "observation_key": "2026-01-02",
                "channel": "A",
                "paid_amount": 12,
            },
            {
                "window_id": "baseline",
                "observation_key": "2026-01-01",
                "channel": "A",
                "paid_amount": 10,
            },
            {
                "window_id": "target",
                "observation_key": "2026-01-02",
                "channel": "B",
                "paid_amount": 5,
            },
        ),
        metric_id="paid_amount",
        value_key="paid_amount",
        channel_key="channel",
        window_id_key="window_id",
        observation_key="observation_key",
        required_window_ids=("target", "baseline"),
        required_window_presence="all",
        completeness_records=(_channel_completeness(),),
        result_refs=("result:channel",),
    )

    assert result.evidence_type == "trust_boundary"
    assert result.strength == "trust_boundary"
    assert result.wording_limit == "context_only"
    assert result.typed_payload["attribution_claim_allowed"] is False
    interpretation = result.typed_payload["interpretation_contract"]
    assert interpretation["source_availability"] == "available"
    assert interpretation["evidence_role"] == "background_context"
    assert interpretation["allowed_use"] == "background_and_candidate_localization"
    assert interpretation["blocked_use"] == "direct_attribution_or_causal_conclusion"
    assert (
        interpretation["customer_wording_policy"]
        == "describe_role_limit_not_missing_data"
    )
    contexts = {item["channel"]: item for item in result.typed_payload["channels"]}
    assert contexts["A"]["comparable"] is True
    assert contexts["B"]["comparable"] is False
    assert contexts["B"]["missing_window_ids"] == ("baseline",)
    assert "channel_window_coverage_incomplete:B" in result.limitations
    partition = interpretation[
        "channel_count_partition"
    ]
    assert partition["whole"] == "channel_count"
    assert partition["parts"] == (
        "comparable_channel_count",
        "incomplete_channel_count",
    )


def test_market_channel_context_zero_fills_reconciled_group_absence() -> None:
    result = market_channel_context(
        (
            {
                "window_id": "target",
                "observation_key": "2026-01-02",
                "channel": "A",
                "paid_amount": 12,
            },
            {
                "window_id": "baseline",
                "observation_key": "2026-01-01",
                "channel": "A",
                "paid_amount": 10,
            },
            {
                "window_id": "target",
                "observation_key": "2026-01-02",
                "channel": "B",
                "paid_amount": 5,
            },
        ),
        metric_id="paid_amount",
        value_key="paid_amount",
        channel_key="channel",
        window_id_key="window_id",
        observation_key="observation_key",
        required_window_ids=("target", "baseline"),
        required_window_presence="reconciled_zero_fill",
        completeness_records=(_channel_completeness(),),
    )

    contexts = {item["channel"]: item for item in result.typed_payload["channels"]}
    assert contexts["B"]["comparable"] is True
    assert contexts["B"]["observed_window_ids"] == ("target",)
    assert contexts["B"]["zero_filled_window_ids"] == ("baseline",)
    assert contexts["B"]["missing_window_ids"] == ()
    assert result.numeric_facts["channel_count"] == 2
    assert result.numeric_facts["comparable_channel_count"] == 2
    assert result.numeric_facts["incomplete_channel_count"] == 0
    assert result.numeric_facts["zero_filled_channel_count"] == 1
    assert "channel_window_coverage_incomplete:B" not in result.limitations


def test_market_channel_context_does_not_zero_fill_without_reconciliation() -> None:
    result = market_channel_context(
        (
            {
                "window_id": "target",
                "observation_key": "2026-01-02",
                "channel": "B",
                "paid_amount": 5,
            },
        ),
        metric_id="paid_amount",
        value_key="paid_amount",
        channel_key="channel",
        window_id_key="window_id",
        observation_key="observation_key",
        required_window_ids=("target", "baseline"),
        required_window_presence="reconciled_zero_fill",
        completeness_records=(
            _channel_completeness(
                status="partial",
                readiness="blocked",
                reconciliation="failed",
            ),
        ),
    )

    channel = result.typed_payload["channels"][0]
    assert result.typed_payload["zero_fill_authorized"] is False
    assert channel["comparable"] is False
    assert channel["zero_filled_window_ids"] == ()
    assert channel["missing_window_ids"] == ("baseline",)
    assert "channel_window_coverage_incomplete:B" in result.limitations


def test_market_channel_context_keeps_failed_reconciliation_as_trust_boundary() -> None:
    result = market_channel_context(
        ({"window": "target", "day": "2026-01-02", "channel": "A", "value": 12},),
        metric_id="paid_amount",
        value_key="value",
        channel_key="channel",
        window_id_key="window",
        observation_key="day",
        required_window_ids=("target",),
        required_window_presence="all",
        completeness_records=(
            _channel_completeness(
                status="partial",
                readiness="blocked",
                reconciliation="failed",
            ),
        ),
    )
    assert result.evidence_type == "trust_boundary"
    assert result.typed_payload["comparison_authorized"] is False
    assert result.typed_payload["reconciliation_state"] == "failed"
    assert result.numeric_facts["comparable_channel_count"] == 0
    assert result.typed_payload["channels"][0]["comparable"] is False
    assert "overall_channel_reconciliation_failed" in result.limitations

    with pytest.raises(
        MarketChannelContextContractError,
        match="market_channel_context_observation_duplicate",
    ):
        market_channel_context(
            (
                {"window": "target", "day": "2026-01-02", "channel": "A", "value": 12},
                {"window": "target", "day": "2026-01-02", "channel": "A", "value": 12},
            ),
            metric_id="paid_amount",
            value_key="value",
            channel_key="channel",
            window_id_key="window",
            observation_key="day",
            required_window_ids=("target",),
            required_window_presence="all",
            completeness_records=(_channel_completeness(),),
        )


def _source(
    source_id: str,
    result_ref: str,
    rows: tuple[dict, ...],
    *,
    tolerance: float = 0.01,
    strategy: str = "additive_sum",
) -> dict:
    return {
        "source_id": source_id,
        "result_ref": result_ref,
        "metric_contract_ref": "contracts/metrics/paid-amount.metric.yaml@0.1",
        "reconciliation_tolerance": tolerance,
        "reconciliation_strategy": strategy,
        "rows": rows,
    }


def _reconciliation_policy(
    *,
    authoritative_source_id: str = "market_dashboard",
    partition_source_id: str = "market_dashboard_channel",
) -> dict:
    return {
        "contract_id": "bounded-window-source-reconciliation.v1",
        "authoritative_source_id": authoritative_source_id,
        "partition_source_id": partition_source_id,
        "window_id_key": "window_id",
        "window_role_key": "window_role",
        "bounded_window_relative_tolerance": 0.002,
        "bounded_change_residual_share": 0.01,
        "hard_observation_relative_limit": 0.01,
    }


def test_source_reconciliation_accepts_bounded_window_and_retains_residual() -> None:
    result = source_reconciliation(
        (
            _source(
                "market_dashboard",
                "result:overall",
                (
                    {
                        "window_id": "target",
                        "window_role": "target",
                        "observation_key": "2026-04-20",
                        "paid_amount": 273_324_149,
                    },
                    {
                        "window_id": "baseline",
                        "window_role": "baseline",
                        "observation_key": "2026-04-19",
                        "paid_amount": 294_619_582,
                    },
                ),
            ),
            _source(
                "market_dashboard_channel",
                "result:channel",
                (
                    {
                        "window_id": "target",
                        "window_role": "target",
                        "observation_key": "2026-04-20",
                        "paid_amount": 273_036_151,
                    },
                    {
                        "window_id": "baseline",
                        "window_role": "baseline",
                        "observation_key": "2026-04-19",
                        "paid_amount": 294_285_084,
                    },
                ),
            ),
        ),
        join_keys=("window_id", "observation_key"),
        value_key="paid_amount",
        reconciliation_tolerance=0.01,
        reconciliation_strategy="additive_sum",
        reconciliation_policy=_reconciliation_policy(),
        result_refs=("result:overall", "result:channel"),
    )

    assert result.evidence_type == "accounting_contribution"
    assert result.strength == "quantified_contribution"
    payload = result.typed_payload
    assert payload["reconciliation_state"] == "bounded_match"
    assert payload["metric_claim_allowed"] is True
    assert payload["window_reconciliations"] == (
        {
            "window_id": "baseline",
            "window_role": "baseline",
            "observation_count": 1,
            "authoritative_total": Decimal("294619582"),
            "partition_total": Decimal("294285084"),
            "residual": Decimal("334498"),
            "absolute_residual": Decimal("334498"),
            "residual_ratio": Decimal("334498") / Decimal("294619582"),
            "state": "bounded_match",
        },
        {
            "window_id": "target",
            "window_role": "target",
            "observation_count": 1,
            "authoritative_total": Decimal("273324149"),
            "partition_total": Decimal("273036151"),
            "residual": Decimal("287998"),
            "absolute_residual": Decimal("287998"),
            "residual_ratio": Decimal("287998") / Decimal("273324149"),
            "state": "bounded_match",
        },
    )
    change = payload["change_reconciliation"]
    assert change["authoritative_change"] == Decimal("-21295433")
    assert change["partition_change"] == Decimal("-21248933")
    assert change["residual_change"] == Decimal("-46500")
    assert change["closure_identity"]["closed"] is True
    assert change["state"] == "bounded_match"
    assert payload["residual_bucket"]["business_label"] == "未调和部分"
    assert payload["residual_bucket"]["ranking_eligible"] is False


def test_source_reconciliation_uses_canonical_numeric_join_identity_and_exact_decimals() -> (
    None
):
    result = source_reconciliation(
        (
            _source(
                "left",
                "result:left",
                (
                    {
                        "window_id": "target",
                        "window_role": "target",
                        "id": 1,
                        "value": Decimal("1.0000000000000000000000000001"),
                    },
                    {
                        "window_id": "target",
                        "window_role": "target",
                        "id": -0.0,
                        "value": Decimal("2"),
                    },
                ),
                tolerance=0,
            ),
            _source(
                "right",
                "result:right",
                (
                    {
                        "window_id": "target",
                        "window_role": "target",
                        "id": 1.0,
                        "value": Decimal("1.0000000000000000000000000002"),
                    },
                    {
                        "window_id": "target",
                        "window_role": "target",
                        "id": 0,
                        "value": Decimal("2"),
                    },
                ),
                tolerance=0,
            ),
        ),
        join_keys=("window_id", "id"),
        value_key="value",
        reconciliation_tolerance=0,
        reconciliation_strategy="additive_sum",
        reconciliation_policy=_reconciliation_policy(
            authoritative_source_id="left",
            partition_source_id="right",
        ),
    )

    assert result.typed_payload["reconciliation_state"] == "bounded_match"
    values = result.typed_payload["observation_reconciliations"]
    precise = next(
        item for item in values if item["authoritative_value"] != Decimal("2")
    )
    assert precise["authoritative_value"] == Decimal(
        "1.0000000000000000000000000001"
    )
    assert precise["partition_value"] == Decimal(
        "1.0000000000000000000000000002"
    )


def test_source_reconciliation_rejects_contract_drift_and_duplicate_join_keys() -> None:
    with pytest.raises(
        SourceReconciliationContractError,
        match="source_reconciliation_metric_contract_inconsistent",
    ):
        source_reconciliation(
            (
                _source(
                    "left",
                    "result:left",
                    (
                        {
                            "window_id": "target",
                            "window_role": "target",
                            "id": "x",
                            "value": 1,
                        },
                    ),
                ),
                _source(
                    "right",
                    "result:right",
                    (
                        {
                            "window_id": "target",
                            "window_role": "target",
                            "id": "x",
                            "value": 1,
                        },
                    ),
                    tolerance=0.02,
                ),
            ),
            join_keys=("window_id", "id"),
            value_key="value",
            reconciliation_tolerance=0.01,
            reconciliation_strategy="additive_sum",
            reconciliation_policy=_reconciliation_policy(
                authoritative_source_id="left",
                partition_source_id="right",
            ),
        )

    with pytest.raises(
        SourceReconciliationContractError,
        match="source_reconciliation_join_key_duplicate",
    ):
        source_reconciliation(
            (
                _source(
                    "left",
                    "result:left",
                    (
                        {
                            "window_id": "target",
                            "window_role": "target",
                            "id": "x",
                            "value": 1,
                        },
                        {
                            "window_id": "target",
                            "window_role": "target",
                            "id": "x",
                            "value": 2,
                        },
                    ),
                ),
                _source(
                    "right",
                    "result:right",
                    (
                        {
                            "window_id": "target",
                            "window_role": "target",
                            "id": "x",
                            "value": 1,
                        },
                    ),
                ),
            ),
            join_keys=("window_id", "id"),
            value_key="value",
            reconciliation_tolerance=0.01,
            reconciliation_strategy="additive_sum",
            reconciliation_policy=_reconciliation_policy(
                authoritative_source_id="left",
                partition_source_id="right",
            ),
        )


def test_source_reconciliation_returns_typed_insufficient_without_pairs() -> None:
    result = source_reconciliation(
        (
            _source(
                "left",
                "result:left",
                (
                    {
                        "window_id": "target",
                        "window_role": "target",
                        "id": "left",
                        "value": 1,
                    },
                ),
            ),
            _source(
                "right",
                "result:right",
                (
                    {
                        "window_id": "target",
                        "window_role": "target",
                        "id": "right",
                        "value": 1,
                    },
                ),
            ),
        ),
        join_keys=("window_id", "id"),
        value_key="value",
        reconciliation_tolerance=0.01,
        reconciliation_strategy="additive_sum",
        reconciliation_policy=_reconciliation_policy(
            authoritative_source_id="left",
            partition_source_id="right",
        ),
    )
    assert result.evidence_type == "insufficient_evidence"
    assert result.strength == "insufficient"
    assert result.typed_payload["reconciliation_state"] == "incomplete"
    assert "no_reconciled_pairs" in result.limitations


@pytest.mark.parametrize("observation_count", (1, 7, 30, 45))
def test_source_reconciliation_scales_across_day_week_month_and_custom_windows(
    observation_count: int,
) -> None:
    authoritative_rows = tuple(
        {
            "window_id": window_id,
            "window_role": window_role,
            "observation_key": f"{window_id}-{index:02d}",
            "paid_amount": value,
        }
        for window_id, window_role, value in (
            ("target", "target", 100_000),
            ("baseline", "baseline", 90_000),
        )
        for index in range(observation_count)
    )
    partition_rows = tuple(
        {
            **row,
            "paid_amount": row["paid_amount"] - (
                100 if row["window_role"] == "target" else 90
            ),
        }
        for row in authoritative_rows
    )
    result = source_reconciliation(
        (
            _source("market_dashboard", "result:overall", authoritative_rows),
            _source(
                "market_dashboard_channel", "result:partition", partition_rows
            ),
        ),
        join_keys=("window_id", "observation_key"),
        value_key="paid_amount",
        reconciliation_tolerance=0.01,
        reconciliation_strategy="additive_sum",
        reconciliation_policy=_reconciliation_policy(),
    )

    assert result.typed_payload["reconciliation_state"] == "bounded_match"
    assert all(
        window["observation_count"] == observation_count
        for window in result.typed_payload["window_reconciliations"]
    )


def test_source_reconciliation_rejects_large_observation_even_when_window_is_small() -> (
    None
):
    result = source_reconciliation(
        (
            _source(
                "market_dashboard",
                "result:overall",
                (
                    {
                        "window_id": "target",
                        "window_role": "target",
                        "observation_key": "normal",
                        "paid_amount": 100_000,
                    },
                    {
                        "window_id": "target",
                        "window_role": "target",
                        "observation_key": "outlier",
                        "paid_amount": 100,
                    },
                ),
            ),
            _source(
                "market_dashboard_channel",
                "result:partition",
                (
                    {
                        "window_id": "target",
                        "window_role": "target",
                        "observation_key": "normal",
                        "paid_amount": 100_000,
                    },
                    {
                        "window_id": "target",
                        "window_role": "target",
                        "observation_key": "outlier",
                        "paid_amount": 0,
                    },
                ),
            ),
        ),
        join_keys=("window_id", "observation_key"),
        value_key="paid_amount",
        reconciliation_tolerance=0.01,
        reconciliation_strategy="additive_sum",
        reconciliation_policy=_reconciliation_policy(),
    )

    assert result.evidence_type == "insufficient_evidence"
    assert result.typed_payload["reconciliation_state"] == "failed"
    assert "window_reconciliation_threshold_exceeded" in result.limitations


def test_context_only_snapshot_gap_requires_current_window_reconciliation() -> None:
    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    snapshot = DatasetSnapshot(
        snapshot_ref="snapshot:market-channel:test",
        dataset_id="market_dashboard_channel",
        physical_table="analytics.market_dashboard_channel_daily",
        watermark="2026-06-20",
        schema_fingerprint="schema:test",
        schema_fields=("business_date", "channel", "paid_amount"),
        contract_ref="contract:market-channel",
        loaded_at="2026-06-21T00:00:00+00:00",
        status="active",
        evidence_state="context_only",
        reconciliation_status="mismatch",
    )

    (gap,) = _snapshot_evidence_gaps(
        (snapshot,),
        {"market_dashboard_channel": ("source_reconciliation",)},
        registry,
    )

    assert gap.gap_id == (
        "dataset:market_dashboard_channel:requires_window_reconciliation:"
        "capability:source_reconciliation"
    )
    assert gap.diagnostic_context == {
        "resolution_mode": "current_window_reconciliation",
        "resolver_capability_id": "source_reconciliation",
        "reconciliation_contract": "bounded-window-source-reconciliation.v1",
    }


def _materializer_contract(
    *,
    query_id: str,
    query_intent: str,
    dataset_id: str,
    required_fields: tuple[str, ...],
    dimensions: tuple[DimensionBinding, ...] = (),
    contract_ref: str = "contracts/metrics/paid-amount.metric.yaml@0.1",
    tolerance: float = 0.01,
) -> QueryContract:
    windows = (
        ResolvedWindow(
            window_id="target",
            role="target",
            label="2026-01-02",
            start_inclusive="2026-01-02",
            end_exclusive="2026-01-03",
            timezone="Africa/Lagos",
            aggregation="daily_total",
            required_complete_days=1,
            source_watermark_requirement="2026-01-02",
        ),
        ResolvedWindow(
            window_id="baseline",
            role="baseline",
            label="2026-01-01",
            start_inclusive="2026-01-01",
            end_exclusive="2026-01-02",
            timezone="Africa/Lagos",
            aggregation="daily_total",
            required_complete_days=1,
            source_watermark_requirement="2026-01-01",
        ),
    )
    return QueryContract(
        query_contract_id=query_id,
        analysis_contract_ref="analysis:test",
        query_intent=query_intent,
        dataset_snapshot_refs=(f"snapshot:{dataset_id}:r1",),
        metric_bindings=(
            MetricBinding(
                metric_id="paid_amount",
                contract_ref=contract_ref,
                dataset_id=dataset_id,
                expression="sum(paid_amount)",
                aggregation="sum",
                required_fields=("paid_amount",),
                grain=("window_id", "observation_key"),
                reconciliation_tolerance=tolerance,
                reconciliation_strategy="additive_sum",
            ),
        ),
        dimension_bindings=dimensions,
        window_refs=("target", "baseline"),
        resolved_windows=windows,
        filters=(),
        result_shape=ResultShape(
            required_fields=required_fields,
            unique_key=(
                "window_id",
                "observation_key",
                *(item.dimension_id for item in dimensions),
            ),
            grain=(
                "window_id",
                "observation_key",
                *(item.dimension_id for item in dimensions),
            ),
            required_window_ids=("target", "baseline"),
        ),
        completeness_assertions=("required_fields_present",),
        workload_class="interactive_aggregate",
        contract_signature=f"signature:{query_id}",
    )


def _materializer_result(
    contract: QueryContract,
    rows: tuple[dict, ...],
    *,
    assertion_results: tuple[dict, ...] = (),
    completeness_status: str = "complete",
    analysis_readiness: str = "ready",
) -> tuple[QueryResultEnvelope, CompletenessReport]:
    result = QueryResultEnvelope(
        query_contract_ref=contract.query_contract_id,
        query_id=f"clickhouse:{contract.query_contract_id}",
        query_hash=f"hash:{contract.query_contract_id}",
        result_ref=f"result:{contract.query_contract_id}",
        execution_status="succeeded",
        rows_ref=f"rows:{contract.query_contract_id}",
        row_count=len(rows),
        completeness_report_ref=f"completeness:{contract.query_contract_id}",
        rows=rows,
        observed_schema={},
        observed_windows=("target", "baseline"),
        observed_grain=contract.result_shape.grain,
        source_snapshot_refs=contract.dataset_snapshot_refs,
    )
    report = CompletenessReport(
        report_ref=result.completeness_report_ref,
        query_contract_ref=contract.query_contract_id,
        result_ref=result.result_ref,
        completeness_status=completeness_status,
        analysis_readiness=analysis_readiness,
        assertion_results=assertion_results,
        failure_reasons=tuple(
            dict.fromkeys(
                reason
                for assertion in assertion_results
                for reason in assertion.get("failure_reasons", ())
            )
        ),
        coverage_summary={
            "window_day_counts": {"target": 1, "baseline": 1},
        },
    )
    return result, report


def _materializer_bound_and_plan(
    contracts_and_rows: tuple[tuple[QueryContract, tuple[dict, ...]], ...],
    *,
    capability_id: str,
) -> tuple[BoundCapabilityInput, CapabilityExecutionPlan]:
    bound = object.__new__(BoundCapabilityInput)
    rows_by_slot = {}
    slots = []
    for index, (contract, rows) in enumerate(contracts_and_rows):
        slot_id = f"slot:{index}"
        rows_by_slot[slot_id] = rows
        slots.append(
            CapabilityInputSlot(
                slot_id=slot_id,
                query_contract_refs=(contract.query_contract_id,),
                required=True,
                accepted_completeness=("complete", "partial"),
                required_fields=contract.result_shape.required_fields,
                required_window_ids=contract.window_refs,
            )
        )
    object.__setattr__(bound, "rows_by_slot", rows_by_slot)
    return bound, CapabilityExecutionPlan(
        capability_id=capability_id,
        capability_contract_ref=f"contract:{capability_id}",
        required_input_slots=tuple(slots),
        optional_input_slots=(),
        merge_strategy="by_query_family",
        minimum_readiness={
            "required_slots": "all",
            "accepted_completeness": ("complete", "partial"),
        },
        degradation_policy={},
        supported_evidence_types=("trust_boundary", "insufficient_evidence"),
        maximum_claim_strength="trust_boundary",
    )


def test_materializer_builds_metric_coverage_records_from_query_authority() -> None:
    contract = _materializer_contract(
        query_id="quality",
        query_intent="data_quality_probe",
        dataset_id="paid_order_success",
        required_fields=(
            "window_id",
            "observation_key",
            "paid_amount",
            "source_row_count",
        ),
    )
    rows = (
        {
            "window_id": "target",
            "observation_key": "2026-01-02",
            "paid_amount": 10,
            "source_row_count": 4,
        },
        {
            "window_id": "baseline",
            "observation_key": "2026-01-01",
            "paid_amount": 8,
            "source_row_count": 3,
        },
    )
    result, report = _materializer_result(contract, rows)
    bound, plan = _materializer_bound_and_plan(
        ((contract, rows),), capability_id="metric_coverage_profile"
    )
    payload = _metric_coverage_profile_payload(
        bound=bound,
        execution_plan=plan,
        contracts=(contract,),
        result_by_query={contract.query_contract_id: result},
        report_by_query={contract.query_contract_id: report},
        metric_id="paid_amount",
        binding={
            "query_families": {"primary": "data_quality_probe"},
            "fields": {
                "value_key": "paid_amount",
                "result_ref_key": "result_ref",
                "window_id_key": "window_id",
                "observation_key": "observation_key",
                "source_row_count_key": "source_row_count",
            },
            "parameters": {
                "coverage_records_source": "query_contract_and_completeness"
            },
        },
        capability_id="metric_coverage_profile",
    )

    assert {row["result_ref"] for row in payload["rows"]} == {result.result_ref}
    assert payload["coverage_records"] == (
        {
            "result_ref": result.result_ref,
            "dataset_id": "paid_order_success",
            "snapshot_refs": ("snapshot:paid_order_success:r1",),
            "completeness_report_ref": report.report_ref,
            "completeness_status": "complete",
            "analysis_readiness": "ready",
            "windows": (
                {"window_id": "target", "required_days": 1, "observed_days": 1},
                {"window_id": "baseline", "required_days": 1, "observed_days": 1},
            ),
        },
    )


def test_materializer_normalizes_channel_reconciliation_without_claim_upgrade() -> None:
    channel_dimension = DimensionBinding(
        dimension_id="channel",
        contract_ref="contract:channel",
        dataset_id="market_dashboard_channel",
        source_field="channel",
        allowed_grains=("day",),
    )
    contract = _materializer_contract(
        query_id="channel",
        query_intent="channel_context_probe",
        dataset_id="market_dashboard_channel",
        required_fields=(
            "window_id",
            "observation_key",
            "channel",
            "paid_amount",
        ),
        dimensions=(channel_dimension,),
    )
    rows = (
        {
            "window_id": "target",
            "observation_key": "2026-01-02",
            "channel": "A",
            "paid_amount": 10,
        },
        {
            "window_id": "baseline",
            "observation_key": "2026-01-01",
            "channel": "A",
            "paid_amount": 8,
        },
    )
    result, report = _materializer_result(
        contract,
        rows,
        assertion_results=(
            {
                "assertion": "overall_channel_reconciliation",
                "passed": False,
                "failure_reasons": ("overall_channel_reconciliation_failed",),
                "failure_classes": ("reconciliation",),
                "details": {"status": "failed"},
            },
        ),
        completeness_status="partial",
        analysis_readiness="blocked",
    )
    bound, plan = _materializer_bound_and_plan(
        ((contract, rows),), capability_id="market_channel_context"
    )
    payload = _market_channel_context_payload(
        bound=bound,
        execution_plan=plan,
        contracts=(contract,),
        result_by_query={contract.query_contract_id: result},
        report_by_query={contract.query_contract_id: report},
        metric_id="paid_amount",
        binding={
            "query_families": {"primary": "channel_context_probe"},
            "fields": {
                "channel_key": "channel",
                "window_id_key": "window_id",
                "observation_key": "observation_key",
            },
            "parameters": {
                "value_key_source": "requested_metric",
                "required_window_presence": "all",
                "completeness_source": "bound_input",
            },
        },
        capability_id="market_channel_context",
    )

    assert payload["value_key"] == "paid_amount"
    assert payload["required_window_ids"] == ("target", "baseline")
    assert payload["completeness_records"][0]["reconciliation_status"] == ("failed")


def test_materializer_requires_two_consistent_source_metric_contracts() -> None:
    left = _materializer_contract(
        query_id="overall",
        query_intent="source_reconciliation_probe",
        dataset_id="market_dashboard",
        required_fields=(
            "window_id",
            "window_role",
            "observation_key",
            "paid_amount",
        ),
    )
    right = _materializer_contract(
        query_id="channel-total",
        query_intent="source_reconciliation_probe",
        dataset_id="market_dashboard_channel",
        required_fields=(
            "window_id",
            "window_role",
            "observation_key",
            "paid_amount",
        ),
    )
    rows = (
        {
            "window_id": "target",
            "window_role": "target",
            "observation_key": "2026-01-02",
            "paid_amount": 10,
        },
        {
            "window_id": "baseline",
            "window_role": "baseline",
            "observation_key": "2026-01-01",
            "paid_amount": 8,
        },
    )
    left_result, _ = _materializer_result(left, rows)
    right_result, _ = _materializer_result(right, rows)
    bound, plan = _materializer_bound_and_plan(
        ((left, rows), (right, rows)), capability_id="source_reconciliation"
    )
    binding = {
        "query_families": {"primary": "source_reconciliation_probe"},
        "fields": {
            "join_keys": ("window_id", "observation_key"),
            "value_key": "paid_amount",
            "window_id_key": "window_id",
            "window_role_key": "window_role",
        },
        "parameters": {
            "required_source_count": 2,
            "tolerance_source": "metric_contract",
            "strategy_source": "metric_contract",
            "reconciliation_contract": "bounded-window-source-reconciliation.v1",
            "authoritative_source_id": "market_dashboard",
            "partition_source_id": "market_dashboard_channel",
            "bounded_window_relative_tolerance": 0.002,
            "bounded_change_residual_share": 0.01,
            "hard_observation_relative_limit": 0.01,
            "context_only_resolution": "current_window_reconciliation",
        },
    }
    payload = _source_reconciliation_payload(
        bound=bound,
        execution_plan=plan,
        contracts=(left, right),
        result_by_query={
            left.query_contract_id: left_result,
            right.query_contract_id: right_result,
        },
        metric_id="paid_amount",
        binding=binding,
        capability_id="source_reconciliation",
    )
    assert tuple(source["source_id"] for source in payload["sources"]) == (
        "market_dashboard",
        "market_dashboard_channel",
    )
    assert str(payload["reconciliation_tolerance"]) == "0.01"
    assert payload["reconciliation_policy"] == {
        "contract_id": "bounded-window-source-reconciliation.v1",
        "authoritative_source_id": "market_dashboard",
        "partition_source_id": "market_dashboard_channel",
        "window_id_key": "window_id",
        "window_role_key": "window_role",
        "bounded_window_relative_tolerance": Decimal("0.002"),
        "bounded_change_residual_share": Decimal("0.01"),
        "hard_observation_relative_limit": Decimal("0.01"),
    }

    drifted = replace(
        right,
        metric_bindings=(
            replace(right.metric_bindings[0], reconciliation_tolerance=0.02),
        ),
    )
    with pytest.raises(
        AuthoritativeTaskInputContractError,
        match="authoritative_source_reconciliation_metric_contract_inconsistent",
    ):
        _source_reconciliation_payload(
            bound=bound,
            execution_plan=plan,
            contracts=(left, drifted),
            result_by_query={
                left.query_contract_id: left_result,
                drifted.query_contract_id: right_result,
            },
            metric_id="paid_amount",
            binding=binding,
            capability_id="source_reconciliation",
        )


def test_materializer_deduplicates_only_identical_overlapping_time_points() -> None:
    rows = (
        {"observation_key": "2026-01-01", "paid_amount": 8, "window_id": "a"},
        {"observation_key": "2026-01-01", "paid_amount": 8, "window_id": "b"},
        {"observation_key": "2026-01-02", "paid_amount": 10, "window_id": "a"},
    )
    normalized = _deduplicated_metric_timeseries_rows(
        rows,
        time_key="observation_key",
        value_key="paid_amount",
    )
    assert len(normalized) == 2

    with pytest.raises(
        ValueError,
        match="authoritative_metric_timeseries_overlap_mismatch",
    ):
        _deduplicated_metric_timeseries_rows(
            (
                rows[0],
                {**rows[1], "paid_amount": 9},
            ),
            time_key="observation_key",
            value_key="paid_amount",
        )
