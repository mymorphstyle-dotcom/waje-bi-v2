from dataclasses import dataclass
from datetime import date, timedelta
from statistics import mean, median
from typing import Any, Iterable, Mapping, Optional


_MATERIAL_EXCEPTION_PERIOD_LIMIT = 12
_CALENDAR_GROUP_AGGREGATIONS = frozenset(
    {"sum_of_complete_days", "mean_of_complete_days"}
)


@dataclass(frozen=True)
class PatternScanResult:
    evidence_ref: str
    capability: str
    evidence_type: str
    strength: str
    wording_limit: str
    numeric_facts: dict[str, Any]
    typed_payload: dict[str, Any]
    limitations: tuple[str, ...]
    result_refs: tuple[str, ...]
    pattern_family: str
    established: bool
    direction_ratio: float
    median_uplift: float
    comparable_periods: int
    min_periods: int
    exceptions: tuple[dict[str, Any], ...]


def scan_pattern(
    rows: Iterable[dict[str, Any]],
    *,
    pattern_family: str,
    materiality_floor: float = 0.0,
    min_periods: Optional[int] = None,
    result_refs: tuple[str, ...] = (),
    evidence_ref: Optional[str] = None,
    **params: Any,
) -> PatternScanResult:
    rows = list(rows)
    if pattern_family == "rolling" and min_periods is None:
        raise ValueError("min_periods is required for rolling patterns")
    min_periods = (
        min_periods
        if min_periods is not None
        else (24 if pattern_family == "intra_period" else 2)
    )
    if type(min_periods) is not int or min_periods <= 0:
        raise ValueError("min_periods must be a positive integer")

    scanners = {
        "intra_period": _scan_intra_period,
        "weekly": _scan_weekly,
        "rolling": _scan_rolling,
        "lag_recovery": _scan_lag_recovery,
        "custom_baseline": _scan_custom_baseline,
    }
    if pattern_family not in scanners:
        raise ValueError(f"unsupported pattern_family: {pattern_family}")

    uplifts, exceptions, scan_payload = scanners[pattern_family](
        rows,
        materiality_floor,
        params,
    )
    comparable_periods = len(uplifts)
    direction_consistency_ratio = (
        sum(1 for uplift in uplifts if uplift > 0) / comparable_periods
        if comparable_periods
        else 0.0
    )
    materiality_hit_ratio = (
        sum(1 for uplift in uplifts if uplift >= materiality_floor) / comparable_periods
        if comparable_periods
        else 0.0
    )
    direction_ratio = materiality_hit_ratio
    median_uplift = median(uplifts) if uplifts else 0.0
    established = (
        comparable_periods >= min_periods
        and materiality_hit_ratio >= 0.70
        and median_uplift >= materiality_floor
    )
    insufficient_boundary = (
        pattern_family == "rolling" and comparable_periods < min_periods
    )
    wording_limit = (
        "insufficient"
        if insufficient_boundary
        else _wording_limit(
            established, direction_ratio, median_uplift, materiality_floor
        )
    )
    strength = (
        "insufficient"
        if insufficient_boundary
        else _strength(established, direction_ratio, median_uplift, materiality_floor)
    )
    evidence_type = (
        "statistical_association"
        if comparable_periods and not insufficient_boundary
        else "insufficient_evidence"
    )
    limitations = tuple(
        reason
        for reason, present in (
            ("no_comparable_periods", comparable_periods == 0),
            ("insufficient_comparable_periods", 0 < comparable_periods < min_periods),
            ("weak_direction", comparable_periods > 0 and direction_ratio < 0.70),
            (
                "below_materiality_floor",
                comparable_periods > 0 and median_uplift < materiality_floor,
            ),
        )
        if present
    )

    numeric_facts = {
        "materiality_floor": materiality_floor,
        "direction_ratio": direction_ratio,
        "direction_consistency_ratio": direction_consistency_ratio,
        "materiality_hit_ratio": materiality_hit_ratio,
        "median_uplift": median_uplift,
        "comparable_periods": comparable_periods,
        "min_periods": min_periods,
    }
    interpretation_contract = {
        "contract_id": "pattern-scan-interpretation.v1",
        "analysis_role": "pattern_stability_context",
        "ratio_semantics": {
            "direction_consistency_ratio": (
                "exact_share_of_comparable_periods_with_positive_uplift"
            ),
            "materiality_hit_ratio": (
                "exact_share_of_comparable_periods_meeting_materiality_floor"
            ),
        },
        "ratio_display_policy": "exact_percentage_or_equivalent_decimal",
        "numeric_qualifier_policy": "must_be_mathematically_entailed",
        "single_period_movement_relationship": "context_only_no_override",
        "exception_period_semantics": (
            "bounded_minority_direction_periods_relative_to_the_observed_"
            "dominant_direction"
        ),
    }
    period_direction_summary = _period_direction_summary(
        scan_payload.get("period_comparisons", ()),
    )
    typed_payload = {
        "interpretation_contract": interpretation_contract,
        "pattern_family": pattern_family,
        "materiality_floor": materiality_floor,
        "direction_ratio": direction_ratio,
        "direction_consistency_ratio": direction_consistency_ratio,
        "materiality_hit_ratio": materiality_hit_ratio,
        "median_uplift": median_uplift,
        "comparable_periods": comparable_periods,
        "min_periods": min_periods,
        "exceptions": exceptions,
        "period_direction_summary": period_direction_summary,
        "claim_material_observations": (
            {"interpretation_contract": interpretation_contract},
            {"period_direction_summary": period_direction_summary},
        ),
        **scan_payload,
    }
    return PatternScanResult(
        evidence_ref=evidence_ref or f"pattern_scan:{pattern_family}",
        capability="pattern_scan",
        evidence_type=evidence_type,
        strength=strength,
        wording_limit=wording_limit,
        numeric_facts=numeric_facts,
        typed_payload=typed_payload,
        limitations=limitations,
        result_refs=result_refs,
        pattern_family=pattern_family,
        established=established,
        direction_ratio=direction_ratio,
        median_uplift=median_uplift,
        comparable_periods=comparable_periods,
        min_periods=min_periods,
        exceptions=tuple(exceptions),
    )


def _scan_intra_period(rows, materiality_floor, params):
    period_key = params.get("period_key") or _first_key(rows, "month", "period")
    group_key = params.get("group_key") or "phase"
    selected = params.get("target_phases") or params.get("target_phase")
    baseline_selected = params.get("baseline_phases") or params.get("baseline_phase")
    return _selected_group_scan(
        rows,
        period_key=period_key,
        group_key=group_key,
        selected=selected,
        baseline_selected=baseline_selected,
        baseline_class=_calendar_baseline_class(params),
        period_grain=_calendar_period_grain(params),
        aggregation=_calendar_group_aggregation(params),
        materiality_floor=materiality_floor,
        required_field="target_phase or target_phases",
    )


def _scan_weekly(rows, materiality_floor, params):
    period_key = params.get("week_key") or _first_key(rows, "week", "period")
    group_key = params.get("weekday_key") or "weekday"
    selected = params.get("target_weekdays") or params.get("target_weekday")
    baseline_selected = params.get("baseline_weekdays") or params.get(
        "baseline_weekday"
    )
    return _selected_group_scan(
        rows,
        period_key=period_key,
        group_key=group_key,
        selected=selected,
        baseline_selected=baseline_selected,
        baseline_class=_calendar_baseline_class(params),
        period_grain=_calendar_period_grain(params),
        aggregation=_calendar_group_aggregation(params),
        materiality_floor=materiality_floor,
        required_field="target_weekday or target_weekdays",
    )


def _selected_group_scan(
    rows,
    *,
    period_key,
    group_key,
    selected,
    baseline_selected,
    baseline_class,
    period_grain,
    aggregation,
    materiality_floor,
    required_field,
):
    if isinstance(selected, (str, int)):
        selected = (selected,)
    if isinstance(baseline_selected, (str, int)):
        baseline_selected = (baseline_selected,)
    selected = tuple(selected or ())
    baseline_selected = tuple(baseline_selected or ())
    if not selected:
        raise ValueError(f"{required_field} is required for calendar patterns")
    if set(selected).intersection(baseline_selected):
        raise ValueError("calendar pattern target and baseline members overlap")
    grouped = _aggregate(
        rows,
        period_key,
        group_key,
        aggregation=aggregation,
    )
    uplifts = []
    exceptions = []
    period_comparisons = []
    for period, groups in sorted(grouped.items()):
        selected_values = [groups[item] for item in selected if item in groups]
        if not selected_values or not groups:
            exceptions.append({"period": period, "reason": "incomplete"})
            continue
        target = mean(selected_values)
        baseline_period = _calendar_baseline_period(
            period,
            baseline_class=baseline_class,
            period_grain=period_grain,
        )
        baseline_groups = grouped.get(baseline_period, {})
        baseline_values = (
            [
                baseline_groups[item]
                for item in baseline_selected
                if item in baseline_groups
            ]
            if baseline_selected
            else [
                value
                for item, value in baseline_groups.items()
                if item not in set(selected)
            ]
        )
        baseline = mean(baseline_values) if baseline_values else None
        _add_comparison(
            period,
            target,
            baseline,
            materiality_floor,
            uplifts,
            exceptions,
            period_comparisons,
            baseline_period=baseline_period,
        )
    return (
        uplifts,
        exceptions,
        {
            "aggregation": aggregation,
            "baseline_class": baseline_class,
            "period_grain": period_grain,
            "period_comparisons": tuple(period_comparisons),
        },
    )


def _scan_rolling(rows, materiality_floor, params):
    rolling_span_days = _required_positive_int(params, "rolling_span_days")
    rolling_step_days = _required_positive_int(params, "rolling_step_days")
    observation_key = _required_string(params, "observation_key")
    window_role_key = _required_string(params, "window_role_key")
    target_role = _required_string(params, "target_role")
    baseline_role = _required_string(params, "baseline_role")
    value_key = _required_string(params, "value_key")
    if target_role == baseline_role:
        raise ValueError("rolling target_role and baseline_role must differ")

    observations = _rolling_observations_by_role(
        rows,
        observation_key=observation_key,
        window_role_key=window_role_key,
        target_role=target_role,
        baseline_role=baseline_role,
        value_key=value_key,
    )
    target_periods, target_exceptions = _rolling_periods(
        observations[target_role],
        window_role=target_role,
        rolling_span_days=rolling_span_days,
        rolling_step_days=rolling_step_days,
    )
    baseline_periods, baseline_exceptions = _rolling_periods(
        observations[baseline_role],
        window_role=baseline_role,
        rolling_span_days=rolling_span_days,
        rolling_step_days=rolling_step_days,
    )
    uplifts: list[float] = []
    exceptions = [*target_exceptions, *baseline_exceptions]
    period_comparisons: list[dict[str, Any]] = []
    rolling_pairs = []
    for relative_index, (target, baseline) in enumerate(
        zip(target_periods, baseline_periods, strict=False)
    ):
        period = f"relative:{relative_index}"
        previous_count = len(uplifts)
        _add_comparison(
            period,
            target["mean"],
            baseline["mean"],
            materiality_floor,
            uplifts,
            exceptions,
            period_comparisons,
        )
        if len(uplifts) == previous_count:
            continue
        rolling_pairs.append(
            {
                "relative_index": relative_index,
                "target_start": target["start"],
                "target_end": target["end"],
                "target_mean": target["mean"],
                "baseline_start": baseline["start"],
                "baseline_end": baseline["end"],
                "baseline_mean": baseline["mean"],
                "uplift": uplifts[-1],
            }
        )
    if len(target_periods) != len(baseline_periods):
        longer_role = (
            target_role
            if len(target_periods) > len(baseline_periods)
            else baseline_role
        )
        exceptions.append(
            {
                "period": "unpaired",
                "reason": "unpaired_rolling_periods",
                "window_role": longer_role,
                "count": abs(len(target_periods) - len(baseline_periods)),
            }
        )
    return (
        uplifts,
        exceptions,
        {
            "rolling_span_days": rolling_span_days,
            "rolling_step_days": rolling_step_days,
            "target_rolling_periods": len(target_periods),
            "baseline_rolling_periods": len(baseline_periods),
            "rolling_pairs": tuple(rolling_pairs),
            "period_comparisons": tuple(period_comparisons),
            "pairing_semantics": "relative_ordinal",
        },
    )


def _rolling_observations_by_role(
    rows: Iterable[Mapping[str, Any]],
    *,
    observation_key: str,
    window_role_key: str,
    target_role: str,
    baseline_role: str,
    value_key: str,
) -> dict[str, dict[date, float]]:
    observations: dict[str, dict[date, float]] = {
        target_role: {},
        baseline_role: {},
    }
    for row in rows:
        role = row.get(window_role_key)
        if role not in observations:
            raise ValueError(f"rolling window_role is invalid: {role}")
        raw_observation = row.get(observation_key)
        if type(raw_observation) is not str:
            raise ValueError("rolling observation_key must be an ISO date")
        try:
            observed_on = date.fromisoformat(raw_observation)
        except ValueError as exc:
            raise ValueError("rolling observation_key must be an ISO date") from exc
        raw_value = row.get(value_key)
        if isinstance(raw_value, bool) or (value := _as_number(raw_value)) is None:
            raise ValueError("rolling metric value must be numeric")
        if observed_on in observations[role]:
            raise ValueError(
                f"rolling observation is duplicated: {role}:{raw_observation}"
            )
        observations[role][observed_on] = value
    return observations


def _rolling_periods(
    observations: Mapping[date, float],
    *,
    window_role: str,
    rolling_span_days: int,
    rolling_step_days: int,
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    if not observations:
        return (), (
            {
                "period": window_role,
                "reason": "missing_window_role",
                "window_role": window_role,
            },
        )
    first_day = min(observations)
    last_day = max(observations)
    if (last_day - first_day).days + 1 < rolling_span_days:
        return (), (
            {
                "period": window_role,
                "reason": "insufficient_contiguous_days",
                "window_role": window_role,
                "available_days": len(observations),
                "rolling_span_days": rolling_span_days,
            },
        )

    periods = []
    exceptions = []
    start = first_day
    last_start = last_day - timedelta(days=rolling_span_days - 1)
    while start <= last_start:
        days = tuple(
            start + timedelta(days=offset) for offset in range(rolling_span_days)
        )
        missing = tuple(day for day in days if day not in observations)
        if missing:
            exceptions.append(
                {
                    "period": f"{window_role}:{start.isoformat()}",
                    "reason": "incomplete_rolling_window",
                    "window_role": window_role,
                    "missing_observation_keys": tuple(
                        item.isoformat() for item in missing
                    ),
                }
            )
        else:
            periods.append(
                {
                    "start": start.isoformat(),
                    "end": days[-1].isoformat(),
                    "mean": mean(observations[day] for day in days),
                }
            )
        start += timedelta(days=rolling_step_days)
    return tuple(periods), tuple(exceptions)


def _required_positive_int(params: Mapping[str, Any], key: str) -> int:
    value = params.get(key)
    if type(value) is not int or value <= 0:
        raise ValueError(f"{key} must be a positive integer")
    return value


def _required_string(params: Mapping[str, Any], key: str) -> str:
    value = params.get(key)
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{key} is required for rolling patterns")
    return value


def _scan_lag_recovery(rows, materiality_floor, params):
    return _pair_scan(
        rows,
        period_key=params.get("event_key")
        or _first_key(rows, "event_id", "event", "period"),
        group_key=params.get("lag_key") or "lag_bucket",
        target_group=params.get("target_bucket") or "post",
        baseline_group=params.get("baseline_bucket") or "pre",
        materiality_floor=materiality_floor,
    )


def _scan_custom_baseline(rows, materiality_floor, params):
    return _pair_scan(
        rows,
        period_key=params.get("period_key")
        or _first_key(rows, "period", "month", "week"),
        group_key=params.get("group_key") or "group",
        target_group=params.get("target_group") or "target",
        baseline_group=params.get("baseline_group") or "baseline",
        materiality_floor=materiality_floor,
    )


def _pair_scan(
    rows, *, period_key, group_key, target_group, baseline_group, materiality_floor
):
    grouped = _aggregate(rows, period_key, group_key)
    uplifts = []
    exceptions = []
    period_comparisons = []
    for period, groups in sorted(grouped.items()):
        target = groups.get(target_group)
        if target is None:
            exceptions.append(
                {"period": period, "reason": "incomplete", "missing": "target"}
            )
            continue
        if baseline_group is None:
            siblings = [value for key, value in groups.items() if key != target_group]
            baseline = max(siblings) if siblings else None
        else:
            baseline = groups.get(baseline_group)
        _add_comparison(
            period,
            target,
            baseline,
            materiality_floor,
            uplifts,
            exceptions,
            period_comparisons,
        )
    return (
        uplifts,
        exceptions,
        {"period_comparisons": tuple(period_comparisons)},
    )


def _add_comparison(
    period,
    target,
    baseline,
    materiality_floor,
    uplifts,
    exceptions,
    period_comparisons,
    *,
    baseline_period=None,
):
    if baseline is None or baseline <= 0:
        exceptions.append(
            {"period": period, "reason": "incomplete", "missing": "baseline"}
        )
        return
    if _outlier_dominated(target, baseline):
        exceptions.append({"period": period, "reason": "outlier_dominated"})
        return
    uplift = (target - baseline) / abs(baseline)
    uplifts.append(uplift)
    period_comparisons.append(
        {
            "period": period,
            **(
                {"baseline_period": baseline_period}
                if baseline_period is not None
                else {}
            ),
            "target_value": target,
            "baseline_value": baseline,
            "uplift": uplift,
            "direction": (
                "positive" if uplift > 0 else "negative" if uplift < 0 else "unchanged"
            ),
        }
    )
    if uplift < materiality_floor:
        exceptions.append(
            {"period": period, "reason": "failed_direction", "uplift": uplift}
        )


def _period_direction_summary(
    period_comparisons: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    comparisons = tuple(dict(item) for item in period_comparisons)
    positive = tuple(item for item in comparisons if item.get("direction") == "positive")
    negative = tuple(item for item in comparisons if item.get("direction") == "negative")
    unchanged = tuple(
        item for item in comparisons if item.get("direction") == "unchanged"
    )
    if len(positive) > len(negative):
        dominant_direction = "positive"
        minority = negative
    elif len(negative) > len(positive):
        dominant_direction = "negative"
        minority = positive
    else:
        dominant_direction = "mixed"
        minority = ()
    selected = tuple(
        sorted(
            minority,
            key=lambda item: (
                -abs(float(item["uplift"])),
                str(item["period"]),
            ),
        )[:_MATERIAL_EXCEPTION_PERIOD_LIMIT]
    )
    return {
        "comparable_period_count": len(comparisons),
        "positive_period_count": len(positive),
        "negative_period_count": len(negative),
        "unchanged_period_count": len(unchanged),
        "dominant_direction": dominant_direction,
        "minority_direction": (
            "negative"
            if dominant_direction == "positive"
            else "positive"
            if dominant_direction == "negative"
            else "none"
        ),
        "exception_period_count": len(minority),
        "displayed_exception_period_count": len(selected),
        "exception_period_record_limit": _MATERIAL_EXCEPTION_PERIOD_LIMIT,
        "exception_selection_policy": (
            "minority_direction_top_absolute_uplift_then_period"
        ),
        "exception_periods_truncated": len(selected) < len(minority),
        "exception_periods": selected,
    }


def _calendar_group_aggregation(params: Mapping[str, Any]) -> str:
    aggregation = params.get("aggregation")
    if aggregation not in _CALENDAR_GROUP_AGGREGATIONS:
        raise ValueError("calendar pattern aggregation is required")
    return str(aggregation)


def _calendar_baseline_class(params: Mapping[str, Any]) -> str:
    baseline_class = params.get("baseline_class")
    if baseline_class not in {
        "custom_control_window",
        "prior_period",
        "same_month_phase",
    }:
        raise ValueError("calendar pattern baseline_class is required")
    return str(baseline_class)


def _calendar_period_grain(params: Mapping[str, Any]) -> str:
    period_grain = params.get("period_grain")
    if period_grain not in {"month", "week", "year"}:
        raise ValueError("calendar pattern period_grain is required")
    return str(period_grain)


def _calendar_baseline_period(
    period: Any,
    *,
    baseline_class: str,
    period_grain: str,
) -> Any:
    if baseline_class != "prior_period":
        return period
    if period_grain == "month":
        raw = str(period)
        try:
            year_text, month_text = raw.split("-", 1)
            year = int(year_text)
            month = int(month_text)
        except (TypeError, ValueError) as exc:
            raise ValueError("calendar month period key is invalid") from exc
        if len(year_text) != 4 or len(month_text) != 2 or not 1 <= month <= 12:
            raise ValueError("calendar month period key is invalid")
        if month == 1:
            year -= 1
            month = 12
        else:
            month -= 1
        return f"{year:04d}-{month:02d}"
    if period_grain == "year":
        raw = str(period)
        if len(raw) != 4 or not raw.isdigit():
            raise ValueError("calendar year period key is invalid")
        return f"{int(raw) - 1:04d}"
    raw = str(period)
    try:
        year_text, week_text = raw.split("-W", 1)
        monday = date.fromisocalendar(int(year_text), int(week_text), 1)
    except (TypeError, ValueError) as exc:
        raise ValueError("calendar week period key is invalid") from exc
    previous = monday - timedelta(days=7)
    previous_year, previous_week, _ = previous.isocalendar()
    return f"{previous_year:04d}-W{previous_week:02d}"


def _aggregate(
    rows,
    period_key,
    group_key,
    *,
    aggregation: str = "mean_of_complete_days",
):
    if aggregation not in _CALENDAR_GROUP_AGGREGATIONS:
        raise ValueError(f"unsupported calendar pattern aggregation: {aggregation}")
    grouped = {}
    for row in rows:
        period = row.get(period_key)
        group = row.get(group_key)
        amount = _as_number(
            row.get("amount", row.get("value", row.get("metric_value")))
        )
        if period is None or group is None or amount is None:
            continue
        bucket = grouped.setdefault(period, {}).setdefault(
            group, {"amount": 0.0, "days": 0.0, "count": 0}
        )
        days = _as_number(row.get("days"))
        bucket["amount"] += amount
        bucket["days"] += days or 0.0
        bucket["count"] += 1
    return {
        period: {
            group: _bucket_value(bucket, aggregation=aggregation)
            for group, bucket in groups.items()
        }
        for period, groups in grouped.items()
    }


def _bucket_value(bucket, *, aggregation):
    if aggregation == "sum_of_complete_days":
        return bucket["amount"]
    if bucket["days"] > 0:
        return bucket["amount"] / bucket["days"]
    return bucket["amount"] / bucket["count"]


def _row_value(row):
    amount = _as_number(row.get("amount", row.get("value", row.get("metric_value"))))
    days = _as_number(row.get("days"))
    if amount is None:
        return None
    return amount / days if days else amount


def _as_number(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_key(rows, *candidates):
    for key in candidates:
        if any(row.get(key) is not None for row in rows):
            return key
    return candidates[0]


def _outlier_dominated(target, baseline):
    lower = min(abs(target), abs(baseline))
    upper = max(abs(target), abs(baseline))
    return lower > 0 and upper / lower >= 10


def _wording_limit(established, direction_ratio, median_uplift, materiality_floor):
    if established:
        return "supported"
    if 0.60 <= direction_ratio < 0.70 and median_uplift >= 0:
        return "tendency"
    return "insufficient"


def _strength(established, direction_ratio, median_uplift, materiality_floor):
    if not established:
        return "low"
    if direction_ratio >= 0.85 and median_uplift >= materiality_floor * 2:
        return "high"
    return "medium"
