from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from bi_agent.runtime.analysis_contracts import (
    QueryContract,
    ResolvedWindow,
    query_contract_signature,
)
from bi_agent.runtime.temporal_comparison import EffectiveTemporalComparison


class WindowMetricEvidenceError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class WindowMetricObservation:
    observation_key: str
    value: Decimal


@dataclass(frozen=True)
class WindowMetricAggregate:
    window_id: str
    role: str
    aggregation: str
    required_complete_days: int
    value: Decimal
    observations: tuple[WindowMetricObservation, ...]

    @property
    def observation_keys(self) -> tuple[str, ...]:
        return tuple(item.observation_key for item in self.observations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "window_role": self.role,
            "aggregation": self.aggregation,
            "required_complete_days": self.required_complete_days,
            "observation_keys": self.observation_keys,
            "value": self.value,
        }


@dataclass(frozen=True)
class WindowMetricComparison:
    metric_id: str
    target: WindowMetricAggregate
    primary_baseline: WindowMetricAggregate
    comparisons: tuple[WindowMetricAggregate, ...]

    @staticmethod
    def changes(
        target: WindowMetricAggregate,
        baseline: WindowMetricAggregate,
    ) -> dict[str, Decimal | None]:
        absolute = target.value - baseline.value
        relative = absolute / baseline.value if baseline.value != 0 else None
        return {
            "absolute_change": absolute,
            "relative_change": relative,
        }

    def to_payload(self) -> dict[str, Any]:
        primary_changes = self.changes(self.target, self.primary_baseline)
        return {
            "metric": self.metric_id,
            "target_window_id": self.target.window_id,
            "baseline_window_id": self.primary_baseline.window_id,
            "target": self.target.to_dict(),
            "primary_baseline": self.primary_baseline.to_dict(),
            "comparisons": tuple(
                {
                    "baseline": baseline.to_dict(),
                    **self.changes(self.target, baseline),
                }
                for baseline in self.comparisons
            ),
            "target_value": self.target.value,
            "baseline_value": self.primary_baseline.value,
            **primary_changes,
        }


def aggregate_window_metric_comparison(
    contract: QueryContract,
    rows: Sequence[Mapping[str, Any]],
    *,
    metric_id: str,
    primary_baseline_window_id: str = "",
    allowed_query_intents: tuple[str, ...] = ("daily_metric_baselines",),
) -> WindowMetricComparison:
    _validate_contract(
        contract,
        metric_id=metric_id,
        allowed_query_intents=allowed_query_intents,
    )
    windows = tuple(contract.resolved_windows)
    by_id = {window.window_id: window for window in windows}
    rows_by_window: dict[str, list[Mapping[str, Any]]] = {
        window.window_id: [] for window in windows
    }
    seen_observations: set[tuple[str, str]] = set()
    for raw_row in rows:
        if not isinstance(raw_row, Mapping):
            raise WindowMetricEvidenceError("window_metric_row_invalid")
        window_id = str(raw_row.get("window_id") or "")
        window = by_id.get(window_id)
        if window is None:
            raise WindowMetricEvidenceError("window_metric_window_unknown")
        if str(raw_row.get("window_role") or "") != window.role:
            raise WindowMetricEvidenceError("window_metric_window_role_drift")
        observation_key = str(raw_row.get("observation_key") or "")
        try:
            observed_day = date.fromisoformat(observation_key)
            start = date.fromisoformat(window.start_inclusive)
            end = date.fromisoformat(window.end_exclusive)
        except (TypeError, ValueError) as exc:
            raise WindowMetricEvidenceError(
                "window_metric_observation_invalid"
            ) from exc
        if not start <= observed_day < end:
            raise WindowMetricEvidenceError("window_metric_observation_out_of_range")
        key = (window_id, observation_key)
        if key in seen_observations:
            raise WindowMetricEvidenceError("window_metric_observation_duplicate")
        seen_observations.add(key)
        _finite_decimal(raw_row.get(metric_id))
        rows_by_window[window_id].append(raw_row)

    targets = tuple(window for window in windows if window.role == "target")
    baselines = tuple(window for window in windows if window.role == "baseline")
    if len(targets) != 1:
        raise WindowMetricEvidenceError("window_metric_target_cardinality_invalid")
    if not baselines:
        raise WindowMetricEvidenceError("window_metric_baseline_missing")
    target = _aggregate_window(
        targets[0], rows_by_window[targets[0].window_id], metric_id
    )
    aggregated_baselines = tuple(
        _aggregate_window(window, rows_by_window[window.window_id], metric_id)
        for window in baselines
    )
    if primary_baseline_window_id:
        primary_candidates = tuple(
            baseline
            for baseline in aggregated_baselines
            if baseline.window_id == primary_baseline_window_id
        )
        if len(primary_candidates) != 1:
            raise WindowMetricEvidenceError("window_metric_primary_baseline_invalid")
        primary_baseline = primary_candidates[0]
    else:
        primary_baseline = aggregated_baselines[0]
    return WindowMetricComparison(
        metric_id=metric_id,
        target=target,
        primary_baseline=primary_baseline,
        comparisons=tuple(
            baseline
            for baseline in aggregated_baselines
            if baseline.window_id != primary_baseline.window_id
        ),
    )


def validate_event_window_metric_authority(
    contract: QueryContract,
    temporal_authority: EffectiveTemporalComparison,
    *,
    primary_baseline_window_id: str,
) -> None:
    if (
        not isinstance(temporal_authority, EffectiveTemporalComparison)
        or temporal_authority.mode != "event_relative"
        or not temporal_authority.event_ref
        or temporal_authority.baseline_window is None
    ):
        raise WindowMetricEvidenceError("event_window_temporal_authority_invalid")
    if not isinstance(contract, QueryContract):
        raise WindowMetricEvidenceError("event_window_query_contract_invalid")
    windows = tuple(contract.resolved_windows)
    targets = tuple(window for window in windows if window.role == "target")
    baselines = tuple(window for window in windows if window.role == "baseline")
    if (
        len(windows) != 2
        or len(targets) != 1
        or len(baselines) != 1
        or targets[0].window_id != "target_day"
        or baselines[0].window_id != "baseline_window"
        or primary_baseline_window_id != baselines[0].window_id
        or tuple(contract.window_refs) != (targets[0].window_id, baselines[0].window_id)
        or tuple(contract.result_shape.required_window_ids)
        != tuple(contract.window_refs)
    ):
        raise WindowMetricEvidenceError("event_window_physical_windows_invalid")
    _validate_resolved_window_against_authority(
        targets[0],
        temporal_authority.target_window,
    )
    _validate_resolved_window_against_authority(
        baselines[0],
        temporal_authority.baseline_window,
    )


def _validate_resolved_window_against_authority(
    window: ResolvedWindow,
    authority_window: Any,
) -> None:
    try:
        start = date.fromisoformat(str(authority_window.start or ""))
        end = date.fromisoformat(str(authority_window.end or ""))
    except (AttributeError, ValueError) as exc:
        raise WindowMetricEvidenceError(
            "event_window_temporal_material_invalid"
        ) from exc
    required_days = (end - start).days + 1
    aggregation = authority_window.aggregation
    if (
        required_days <= 0
        or not isinstance(aggregation, str)
        or not aggregation
        or window.role != authority_window.role
        or window.start_inclusive != start.isoformat()
        or window.end_exclusive != (end + timedelta(days=1)).isoformat()
        or window.required_complete_days != required_days
        or window.source_watermark_requirement != end.isoformat()
        or window.aggregation != aggregation
    ):
        raise WindowMetricEvidenceError("event_window_temporal_material_drift")


def _validate_contract(
    contract: QueryContract,
    *,
    metric_id: str,
    allowed_query_intents: tuple[str, ...],
) -> None:
    if not isinstance(contract, QueryContract):
        raise WindowMetricEvidenceError("window_metric_query_contract_invalid")
    try:
        signature = query_contract_signature(contract)
    except (TypeError, ValueError) as exc:
        raise WindowMetricEvidenceError("window_metric_query_contract_invalid") from exc
    if not contract.contract_signature or signature != contract.contract_signature:
        raise WindowMetricEvidenceError(
            "window_metric_query_contract_signature_invalid"
        )
    if contract.query_intent not in allowed_query_intents:
        raise WindowMetricEvidenceError("window_metric_query_intent_invalid")
    if (
        tuple(contract.result_shape.unique_key) != ("window_id", "observation_key")
        or tuple(contract.result_shape.grain) != ("window_id", "observation_key")
        or contract.dimension_bindings
    ):
        raise WindowMetricEvidenceError("window_metric_query_grain_invalid")
    metric_ids = tuple(binding.metric_id for binding in contract.metric_bindings)
    if metric_id not in metric_ids:
        raise WindowMetricEvidenceError("window_metric_metric_unbound")
    windows = tuple(contract.resolved_windows)
    window_ids = tuple(window.window_id for window in windows)
    if not windows or len(window_ids) != len(set(window_ids)):
        raise WindowMetricEvidenceError("window_metric_window_contract_invalid")
    if window_ids != tuple(contract.window_refs) or window_ids != tuple(
        contract.result_shape.required_window_ids
    ):
        raise WindowMetricEvidenceError("window_metric_window_contract_order_invalid")
    if any(
        window.role not in {"target", "baseline", "reference"} for window in windows
    ):
        raise WindowMetricEvidenceError("window_metric_window_role_invalid")
    if any(
        window.aggregation
        not in {
            "daily_total",
            "sum_of_complete_days",
            "mean_of_complete_days",
            "daily_series",
        }
        for window in windows
    ):
        raise WindowMetricEvidenceError("window_metric_aggregation_unsupported")
    if any(
        window.role in {"target", "baseline"} and window.aggregation == "daily_series"
        for window in windows
    ):
        raise WindowMetricEvidenceError("window_metric_aggregation_unsupported")


def _aggregate_window(
    window: ResolvedWindow,
    rows: Sequence[Mapping[str, Any]],
    metric_id: str,
) -> WindowMetricAggregate:
    start = date.fromisoformat(window.start_inclusive)
    end = date.fromisoformat(window.end_exclusive)
    interval_days = (end - start).days
    if window.required_complete_days <= 0:
        raise WindowMetricEvidenceError("window_metric_required_days_invalid")
    expected_keys = tuple(
        (start + timedelta(days=offset)).isoformat() for offset in range(interval_days)
    )
    observations = tuple(
        sorted(
            (
                WindowMetricObservation(
                    observation_key=str(row.get("observation_key") or ""),
                    value=_finite_decimal(row.get(metric_id)),
                )
                for row in rows
            ),
            key=lambda item: item.observation_key,
        )
    )
    actual_keys = tuple(item.observation_key for item in observations)
    if window.aggregation == "daily_total":
        if (
            window.required_complete_days != 1
            or interval_days != 1
            or len(observations) != 1
            or actual_keys != expected_keys
        ):
            raise WindowMetricEvidenceError("window_metric_daily_total_incomplete")
        value = observations[0].value
    elif window.aggregation in {
        "sum_of_complete_days",
        "mean_of_complete_days",
    }:
        if (
            interval_days != window.required_complete_days
            or len(observations) != window.required_complete_days
            or actual_keys != expected_keys
        ):
            raise WindowMetricEvidenceError("window_metric_complete_days_invalid")
        value = sum((item.value for item in observations), Decimal(0))
        if window.aggregation == "mean_of_complete_days":
            value /= Decimal(window.required_complete_days)
    else:
        raise WindowMetricEvidenceError("window_metric_aggregation_unsupported")
    return WindowMetricAggregate(
        window_id=window.window_id,
        role=window.role,
        aggregation=window.aggregation,
        required_complete_days=window.required_complete_days,
        value=value,
        observations=observations,
    )


def _finite_decimal(value: Any) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise WindowMetricEvidenceError("window_metric_value_invalid") from exc
    if not parsed.is_finite():
        raise WindowMetricEvidenceError("window_metric_value_invalid")
    return parsed
