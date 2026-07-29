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
from bi_agent.runtime.event_window_derivation import (
    EventWindowDerivationError,
    validate_event_window_set,
)
from bi_agent.runtime.temporal_comparison import EffectiveTemporalComparison
from bi_agent.runtime.temporal_comparison import (
    TemporalComparisonContractError,
    calendar_partition_evaluation_role_for_date,
    calendar_partition_role_for_date,
    validate_calendar_partition_role_frame,
)


class WindowMetricEvidenceError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


WINDOW_METRIC_INTERPRETATION_CONTRACT = {
    "contract_id": "window-metric-comparison-interpretation.v1",
    "analysis_role": "observed_window_comparison",
    "comparison_subject": "same_metric_across_resolved_windows",
    "target_value_definition": "aggregate_metric_over_target_window",
    "baseline_value_definition": "aggregate_metric_over_primary_baseline_window",
    "absolute_change_formula": "target_value - baseline_value",
    "relative_change_formula": "absolute_change / baseline_value",
    "zero_baseline_policy": "relative_change_unavailable",
    "completeness_authority": "required_complete_days_and_observation_keys",
    "causal_interpretation": "forbidden",
}

EVENT_WINDOW_SET_INTERPRETATION_CONTRACT = {
    "contract_id": "event-window-set-comparison-interpretation.v1",
    "analysis_role": "observed_event_window_comparison",
    "comparison_subject": "same_metric_after_each_event_vs_before_each_event",
    "target_value_definition": "aggregate_metric_over_post_event_window",
    "baseline_value_definition": "aggregate_metric_over_pre_event_window",
    "window_length_definition": "event_duration",
    "absolute_change_formula": "target_value - baseline_value",
    "relative_change_formula": "absolute_change / baseline_value",
    "zero_baseline_policy": "relative_change_unavailable",
    "completeness_authority": "full_daily_evaluation_frame_and_complete_derived_windows",
    "causal_interpretation": "forbidden",
    "writer_fact_selection": {
        "mode": "named_fact_subset",
        "fact_names": [
            "event_occurrence_count",
            "post_event_higher_count",
            "post_event_lower_count",
            "post_event_unchanged_count",
            "displayed_comparison_count",
        ],
    },
}


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
            "interpretation_contract": dict(WINDOW_METRIC_INTERPRETATION_CONTRACT),
            **primary_changes,
        }


def aggregate_window_metric_comparison(
    contract: QueryContract,
    rows: Sequence[Mapping[str, Any]],
    *,
    metric_id: str,
    primary_baseline_window_id: str = "",
    allowed_query_intents: tuple[str, ...] = (
        "daily_metric_baselines",
        "component_driver_scan",
    ),
) -> WindowMetricComparison:
    partition_frame = _partition_role_frame(contract)
    _validate_contract(
        contract,
        metric_id=metric_id,
        allowed_query_intents=allowed_query_intents,
        partition_role_frame=partition_frame is not None,
    )
    if partition_frame is not None:
        return _aggregate_partition_role_comparison(
            contract,
            rows,
            metric_id=metric_id,
            primary_baseline_window_id=primary_baseline_window_id,
            frame=partition_frame,
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


def aggregate_derived_event_window_set(
    contract: QueryContract,
    rows: Sequence[Mapping[str, Any]],
    *,
    metric_id: str,
    event_window_set: Mapping[str, Any],
    temporal_authority: EffectiveTemporalComparison,
    derivation_policy: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        validated_set = validate_event_window_set(
            event_window_set,
            temporal_authority=temporal_authority,
            policy=derivation_policy,
        )
    except EventWindowDerivationError as exc:
        raise WindowMetricEvidenceError(str(exc)) from exc
    _validate_contract(
        contract,
        metric_id=metric_id,
        allowed_query_intents=("daily_metric_baselines",),
    )
    windows = tuple(contract.resolved_windows)
    if (
        temporal_authority.mode != "calendar_partition"
        or len(windows) != 1
        or windows[0].role != "target"
        or tuple(contract.window_refs) != (windows[0].window_id,)
    ):
        raise WindowMetricEvidenceError(
            "event_window_set_query_authority_invalid"
        )
    physical = windows[0]
    try:
        physical_start = date.fromisoformat(physical.start_inclusive)
        physical_end = date.fromisoformat(physical.end_exclusive)
        authority_start = date.fromisoformat(
            str(temporal_authority.target_window.start or "")
        )
        authority_end = date.fromisoformat(
            str(temporal_authority.target_window.end or "")
        ) + timedelta(days=1)
    except ValueError as exc:
        raise WindowMetricEvidenceError(
            "event_window_set_query_authority_invalid"
        ) from exc
    if (
        physical_start != authority_start
        or physical_end != authority_end
        or physical.required_complete_days != (physical_end - physical_start).days
    ):
        raise WindowMetricEvidenceError(
            "event_window_set_query_authority_invalid"
        )
    by_day: dict[date, Decimal] = {}
    for raw_row in rows:
        if not isinstance(raw_row, Mapping):
            raise WindowMetricEvidenceError("event_window_set_row_invalid")
        if (
            raw_row.get("window_id") != physical.window_id
            or raw_row.get("window_role") != "target"
        ):
            raise WindowMetricEvidenceError("event_window_set_row_invalid")
        try:
            observed_day = date.fromisoformat(
                str(raw_row.get("observation_key") or "")
            )
        except ValueError as exc:
            raise WindowMetricEvidenceError(
                "event_window_set_observation_invalid"
            ) from exc
        if (
            not physical_start <= observed_day < physical_end
            or observed_day in by_day
        ):
            raise WindowMetricEvidenceError(
                "event_window_set_observation_invalid"
            )
        by_day[observed_day] = _finite_decimal(raw_row.get(metric_id))
    expected_days = {
        physical_start + timedelta(days=offset)
        for offset in range((physical_end - physical_start).days)
    }
    if set(by_day) != expected_days:
        raise WindowMetricEvidenceError(
            "event_window_set_complete_days_invalid"
        )

    comparisons = []
    for occurrence in validated_set["occurrences"]:
        required_days = int(occurrence["required_complete_days"])
        baseline_start = date.fromisoformat(occurrence["baseline_start_date"])
        target_start = date.fromisoformat(occurrence["target_start_date"])
        baseline_days = tuple(
            baseline_start + timedelta(days=offset)
            for offset in range(required_days)
        )
        target_days = tuple(
            target_start + timedelta(days=offset)
            for offset in range(required_days)
        )
        if any(day not in by_day for day in (*baseline_days, *target_days)):
            raise WindowMetricEvidenceError(
                "event_window_set_complete_days_invalid"
            )
        baseline_value = sum(
            (by_day[day] for day in baseline_days),
            Decimal(0),
        )
        target_value = sum(
            (by_day[day] for day in target_days),
            Decimal(0),
        )
        if occurrence["aggregation"] == "mean_of_complete_days":
            divisor = Decimal(required_days)
            baseline_value /= divisor
            target_value /= divisor
        absolute_change = target_value - baseline_value
        relative_change = (
            absolute_change / baseline_value if baseline_value != 0 else None
        )
        comparisons.append(
            {
                "occurrence_ref": occurrence["occurrence_ref"],
                "source_family": occurrence["source_family"],
                "event_type": occurrence["event_type"],
                "affected_scope": occurrence["affected_scope"],
                "authority": occurrence["authority"],
                "evidence_level": occurrence["evidence_level"],
                "wording_limit": occurrence["wording_limit"],
                "event_start_date": occurrence["event_start_date"],
                "event_end_date": occurrence["event_end_date"],
                "baseline_start_date": occurrence["baseline_start_date"],
                "baseline_end_date": occurrence["baseline_end_date"],
                "target_start_date": occurrence["target_start_date"],
                "target_end_date": occurrence["target_end_date"],
                "aggregation": occurrence["aggregation"],
                "required_complete_days": required_days,
                "baseline_value": baseline_value,
                "target_value": target_value,
                "absolute_change": absolute_change,
                "relative_change": relative_change,
                "direction": (
                    "higher"
                    if absolute_change > 0
                    else "lower"
                    if absolute_change < 0
                    else "unchanged"
                ),
            }
        )
    return {
        "metric": metric_id,
        "comparison_relation": "post_event_vs_pre_event",
        "event_ref": validated_set["event_ref"],
        "temporal_authority_ref": validated_set["temporal_authority_ref"],
        "source_temporal_authority_ref": validated_set[
            "source_temporal_authority_ref"
        ],
        "event_occurrence_count": len(comparisons),
        "comparisons": tuple(comparisons),
        "excluded_occurrence_counts": dict(
            validated_set["excluded_occurrence_counts"]
        ),
        "interpretation_contract": dict(
            EVENT_WINDOW_SET_INTERPRETATION_CONTRACT
        ),
    }


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
    partition_role_frame: bool = False,
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
    expected_grain = (
        ("window_id", "observation_key", "window_role")
        if partition_role_frame
        and contract.result_shape.result_semantics == "complete_window_aggregate"
        else ("window_id", "observation_key")
    )
    if (
        tuple(contract.result_shape.unique_key) != expected_grain
        or tuple(contract.result_shape.grain) != expected_grain
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


def _partition_role_frame(
    contract: QueryContract,
) -> Mapping[str, Any] | None:
    raw = contract.query_parameters.get("calendar_partition_role_frame")
    if raw is None:
        return None
    try:
        return validate_calendar_partition_role_frame(raw)
    except TemporalComparisonContractError as exc:
        raise WindowMetricEvidenceError(
            "window_metric_partition_frame_invalid"
        ) from exc


def _aggregate_partition_role_comparison(
    contract: QueryContract,
    rows: Sequence[Mapping[str, Any]],
    *,
    metric_id: str,
    primary_baseline_window_id: str,
    frame: Mapping[str, Any],
) -> WindowMetricComparison:
    windows = tuple(contract.resolved_windows)
    if len(windows) != 1 or windows[0].role != "target":
        raise WindowMetricEvidenceError(
            "window_metric_partition_window_invalid"
        )
    physical = windows[0]
    target_id = f"{physical.window_id}:partition:target"
    baseline_id = f"{physical.window_id}:partition:baseline"
    if primary_baseline_window_id and primary_baseline_window_id != baseline_id:
        raise WindowMetricEvidenceError(
            "window_metric_primary_baseline_invalid"
        )
    aggregate_result = (
        contract.result_shape.result_semantics == "complete_window_aggregate"
    )
    by_role: dict[str, list[WindowMetricObservation]] = {
        "target": [],
        "baseline": [],
    }
    seen: set[tuple[str, str]] = set()
    try:
        start = date.fromisoformat(physical.start_inclusive)
        end = date.fromisoformat(physical.end_exclusive)
    except ValueError as exc:
        raise WindowMetricEvidenceError(
            "window_metric_partition_window_invalid"
        ) from exc
    if aggregate_result:
        day_counts: dict[str, int] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                raise WindowMetricEvidenceError("window_metric_row_invalid")
            role = str(row.get("window_role") or "")
            if (
                role not in by_role
                or row.get("window_id") != physical.window_id
                or row.get("observation_key") != physical.window_id
                or by_role[role]
            ):
                raise WindowMetricEvidenceError(
                    "window_metric_partition_aggregate_invalid"
                )
            complete_days = row.get("source_complete_days")
            if (
                isinstance(complete_days, bool)
                or not isinstance(complete_days, int)
                or complete_days <= 0
            ):
                raise WindowMetricEvidenceError(
                    "window_metric_partition_complete_days_invalid"
                )
            by_role[role].append(
                WindowMetricObservation(
                    observation_key=physical.window_id,
                    value=_finite_decimal(row.get(metric_id)),
                )
            )
            day_counts[role] = complete_days
        if set(day_counts) != set(by_role):
            raise WindowMetricEvidenceError(
                "window_metric_partition_aggregate_invalid"
            )
    else:
        expected_dates: dict[str, tuple[str, ...]] = {
            role: tuple(
                (start + timedelta(days=offset)).isoformat()
                for offset in range((end - start).days)
                if calendar_partition_evaluation_role_for_date(
                    start + timedelta(days=offset),
                    frame,
                    evaluation_start=start,
                    evaluation_end_exclusive=end,
                )
                == role
            )
            for role in by_role
        }
        for row in rows:
            if not isinstance(row, Mapping):
                raise WindowMetricEvidenceError("window_metric_row_invalid")
            role = str(row.get("window_role") or "")
            observation_key = str(row.get("observation_key") or "")
            key = (role, observation_key)
            try:
                observed = date.fromisoformat(observation_key)
            except ValueError as exc:
                raise WindowMetricEvidenceError(
                    "window_metric_observation_invalid"
                ) from exc
            if (
                role not in by_role
                or row.get("window_id") != physical.window_id
                or not start <= observed < end
                or calendar_partition_role_for_date(observed, frame) != role
                or key in seen
            ):
                raise WindowMetricEvidenceError(
                    "window_metric_partition_observation_invalid"
                )
            seen.add(key)
            by_role[role].append(
                WindowMetricObservation(
                    observation_key=observation_key,
                    value=_finite_decimal(row.get(metric_id)),
                )
            )
        for role, observations in by_role.items():
            observations.sort(key=lambda item: item.observation_key)
            if tuple(item.observation_key for item in observations) != expected_dates[
                role
            ]:
                raise WindowMetricEvidenceError(
                    "window_metric_partition_complete_days_invalid"
                )
        day_counts = {role: len(items) for role, items in by_role.items()}
    if any(count <= 0 for count in day_counts.values()):
        raise WindowMetricEvidenceError(
            "window_metric_partition_complete_days_invalid"
        )

    def aggregate(role: str, window_id: str) -> WindowMetricAggregate:
        observations = tuple(by_role[role])
        total = sum((item.value for item in observations), Decimal(0))
        value = observations[0].value if aggregate_result else total
        if not aggregate_result and frame["aggregation"] == "mean_of_complete_days":
            value /= Decimal(day_counts[role])
        return WindowMetricAggregate(
            window_id=window_id,
            role=role,
            aggregation=str(frame["aggregation"]),
            required_complete_days=day_counts[role],
            value=value,
            observations=observations,
        )

    target = aggregate("target", target_id)
    baseline = aggregate("baseline", baseline_id)
    return WindowMetricComparison(
        metric_id=metric_id,
        target=target,
        primary_baseline=baseline,
        comparisons=(),
    )
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
