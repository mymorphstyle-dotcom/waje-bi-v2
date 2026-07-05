from dataclasses import dataclass
from statistics import mean, median
from typing import Any, Iterable, Optional


@dataclass(frozen=True)
class PatternScanResult:
    evidence_ref: str
    capability: str
    evidence_type: str
    strength: str
    wording_limit: str
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
    min_periods = min_periods if min_periods is not None else (24 if pattern_family == "intra_period" else 2)

    scanners = {
        "intra_period": _scan_intra_period,
        "weekly": _scan_weekly,
        "event_relative": _scan_event_relative,
        "rolling": _scan_rolling,
        "lag_recovery": _scan_lag_recovery,
        "custom_baseline": _scan_custom_baseline,
    }
    if pattern_family not in scanners:
        raise ValueError(f"unsupported pattern_family: {pattern_family}")

    uplifts, exceptions = scanners[pattern_family](rows, materiality_floor, params)
    comparable_periods = len(uplifts)
    direction_ratio = sum(1 for uplift in uplifts if uplift >= materiality_floor) / comparable_periods if comparable_periods else 0.0
    median_uplift = median(uplifts) if uplifts else 0.0
    established = (
        comparable_periods >= min_periods
        and direction_ratio >= 0.70
        and median_uplift >= materiality_floor
    )
    wording_limit = _wording_limit(established, direction_ratio, median_uplift, materiality_floor)
    strength = _strength(established, direction_ratio, median_uplift, materiality_floor)
    limitations = tuple(
        reason
        for reason, present in (
            ("insufficient_comparable_periods", comparable_periods < min_periods),
            ("weak_direction", direction_ratio < 0.70),
            ("below_materiality_floor", median_uplift < materiality_floor),
        )
        if present
    )

    typed_payload = {
        "pattern_family": pattern_family,
        "materiality_floor": materiality_floor,
        "direction_ratio": direction_ratio,
        "median_uplift": median_uplift,
        "comparable_periods": comparable_periods,
        "min_periods": min_periods,
        "exceptions": exceptions,
    }
    return PatternScanResult(
        evidence_ref=evidence_ref or f"pattern_scan:{pattern_family}",
        capability="pattern_scan",
        evidence_type="statistical_association",
        strength=strength,
        wording_limit=wording_limit,
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
    target = params.get("target_phase") or params.get("target_group")
    if target is None:
        raise ValueError("target_phase is required for intra_period patterns")
    return _pair_scan(
        rows,
        period_key=period_key,
        group_key=group_key,
        target_group=target,
        baseline_group=None,
        materiality_floor=materiality_floor,
    )


def _scan_weekly(rows, materiality_floor, params):
    period_key = params.get("week_key") or _first_key(rows, "week", "period")
    group_key = params.get("weekday_key") or "weekday"
    selected = params.get("target_weekdays") or params.get("target_weekday")
    if isinstance(selected, (str, int)):
        selected = (selected,)
    if not selected:
        raise ValueError("target_weekday or target_weekdays is required for weekly patterns")

    grouped = _aggregate(rows, period_key, group_key)
    uplifts = []
    exceptions = []
    for period, groups in sorted(grouped.items()):
        selected_values = [groups[item] for item in selected if item in groups]
        if not selected_values or not groups:
            exceptions.append({"period": period, "reason": "incomplete"})
            continue
        target = mean(selected_values)
        baseline = mean(groups.values())
        _add_comparison(period, target, baseline, materiality_floor, uplifts, exceptions)
    return uplifts, exceptions


def _scan_event_relative(rows, materiality_floor, params):
    return _pair_scan(
        rows,
        period_key=params.get("event_key") or _first_key(rows, "event_id", "event", "period"),
        group_key=params.get("window_key") or "window",
        target_group=params.get("target_window") or "during",
        baseline_group=params.get("baseline_window") or "before",
        materiality_floor=materiality_floor,
    )


def _scan_rolling(rows, materiality_floor, params):
    if any("baseline_high" in row for row in rows):
        period_key = params.get("period_key") or _first_key(rows, "window", "period", "month")
        uplifts = []
        exceptions = []
        for row in rows:
            target = _row_value(row)
            baseline = _as_number(row.get("baseline_high"))
            period = row.get(period_key)
            if target is None or baseline is None:
                exceptions.append({"period": period, "reason": "incomplete"})
                continue
            _add_comparison(period, target, baseline, materiality_floor, uplifts, exceptions)
        return uplifts, exceptions

    return _pair_scan(
        rows,
        period_key=params.get("period_key") or _first_key(rows, "window", "period", "month"),
        group_key=params.get("group_key") or "group",
        target_group=params.get("target_group") or "target",
        baseline_group=params.get("baseline_group") or "baseline",
        materiality_floor=materiality_floor,
    )


def _scan_lag_recovery(rows, materiality_floor, params):
    return _pair_scan(
        rows,
        period_key=params.get("event_key") or _first_key(rows, "event_id", "event", "period"),
        group_key=params.get("lag_key") or "lag_bucket",
        target_group=params.get("target_bucket") or "post",
        baseline_group=params.get("baseline_bucket") or "pre",
        materiality_floor=materiality_floor,
    )


def _scan_custom_baseline(rows, materiality_floor, params):
    return _pair_scan(
        rows,
        period_key=params.get("period_key") or _first_key(rows, "period", "month", "week"),
        group_key=params.get("group_key") or "group",
        target_group=params.get("target_group") or "target",
        baseline_group=params.get("baseline_group") or "baseline",
        materiality_floor=materiality_floor,
    )


def _pair_scan(rows, *, period_key, group_key, target_group, baseline_group, materiality_floor):
    grouped = _aggregate(rows, period_key, group_key)
    uplifts = []
    exceptions = []
    for period, groups in sorted(grouped.items()):
        target = groups.get(target_group)
        if target is None:
            exceptions.append({"period": period, "reason": "incomplete", "missing": "target"})
            continue
        if baseline_group is None:
            siblings = [value for key, value in groups.items() if key != target_group]
            baseline = max(siblings) if siblings else None
        else:
            baseline = groups.get(baseline_group)
        _add_comparison(period, target, baseline, materiality_floor, uplifts, exceptions)
    return uplifts, exceptions


def _add_comparison(period, target, baseline, materiality_floor, uplifts, exceptions):
    if baseline is None or baseline <= 0:
        exceptions.append({"period": period, "reason": "incomplete", "missing": "baseline"})
        return
    if _outlier_dominated(target, baseline):
        exceptions.append({"period": period, "reason": "outlier_dominated"})
        return
    uplift = (target - baseline) / abs(baseline)
    uplifts.append(uplift)
    if uplift < materiality_floor:
        exceptions.append({"period": period, "reason": "failed_direction", "uplift": uplift})


def _aggregate(rows, period_key, group_key):
    grouped = {}
    for row in rows:
        period = row.get(period_key)
        group = row.get(group_key)
        amount = _as_number(row.get("amount", row.get("value", row.get("metric_value"))))
        if period is None or group is None or amount is None:
            continue
        bucket = grouped.setdefault(period, {}).setdefault(group, {"amount": 0.0, "days": 0.0, "count": 0})
        days = _as_number(row.get("days"))
        bucket["amount"] += amount
        bucket["days"] += days or 0.0
        bucket["count"] += 1
    return {
        period: {group: _bucket_value(bucket) for group, bucket in groups.items()}
        for period, groups in grouped.items()
    }


def _bucket_value(bucket):
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
