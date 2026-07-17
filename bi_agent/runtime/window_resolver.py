from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import re
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from bi_agent.runtime.analysis_contracts import ContractGap, ResolvedWindow
from bi_agent.runtime.baseline_semantics import CANONICAL_BASELINE_IDS


CURRENT_DATA_BASELINES = CANONICAL_BASELINE_IDS


@dataclass(frozen=True)
class WindowResolution:
    windows: tuple[ResolvedWindow, ...]
    gaps: tuple[ContractGap, ...]


@dataclass(frozen=True)
class CanonicalWindowStrategy:
    window_id: str
    strategy: str
    offset_days: int
    complete_days: int
    aggregation: str


@dataclass(frozen=True)
class ContextWindowSpec:
    capability_id: str
    relation: str
    unit: str
    count: int


CONTEXT_WINDOW_RELATIONS = frozenset({"trailing_complete_periods"})
CONTEXT_WINDOW_UNITS = frozenset({"day", "week", "month", "quarter"})
_CONTEXT_WINDOW_SPEC_FIELDS = frozenset(
    {"capability_id", "relation", "unit", "count"}
)


CANONICAL_WINDOW_STRATEGIES = {
    "previous_day": CanonicalWindowStrategy(
        window_id="previous_day",
        strategy="point_offset",
        offset_days=1,
        complete_days=1,
        aggregation="daily_total",
    ),
    "rolling_7_day_baseline": CanonicalWindowStrategy(
        window_id="rolling_7_day_baseline",
        strategy="trailing_complete_days",
        offset_days=0,
        complete_days=7,
        aggregation="mean_of_complete_days",
    ),
    "same_weekday_last_week": CanonicalWindowStrategy(
        window_id="same_weekday_last_week",
        strategy="point_offset",
        offset_days=7,
        complete_days=1,
        aggregation="daily_total",
    ),
}


def resolve_revenue_windows(
    *,
    target_semantic: str,
    baselines: tuple[str, ...],
    context_window_specs: tuple[Mapping[str, Any], ...],
    as_of: datetime,
    timezone_name: str,
    dataset_watermarks: Mapping[str, date],
    affected_capabilities: tuple[str, ...],
    affected_claim_types: tuple[str, ...],
    fixed_window_bounds: Mapping[str, tuple[str, str]] | None = None,
) -> WindowResolution:
    _validate_window_strategy_ids(baselines, field="baseline")
    context_specs = _validated_context_window_specs(context_window_specs)
    local_day = as_of.astimezone(ZoneInfo(timezone_name)).date()
    target_day = _resolve_target_day(target_semantic, local_day)
    windows = [_day_window("target_day", "target", target_day, timezone_name)]
    for window_id in baselines:
        windows.append(
            _resolve_reference_window(
                CANONICAL_WINDOW_STRATEGIES[window_id],
                target_day=target_day,
                timezone_name=timezone_name,
            )
        )
    windows.extend(
        _resolve_context_window_spec(
            spec,
            target_day=target_day,
            timezone_name=timezone_name,
        )
        for spec in context_specs
    )
    windows = _apply_fixed_window_bounds(
        windows,
        fixed_window_bounds or {},
        timezone_name=timezone_name,
    )
    gaps = []
    required_end = target_day
    has_window_gap = any(watermark < required_end for watermark in dataset_watermarks.values())
    if has_window_gap and not affected_capabilities:
        raise ValueError("window_gap_requires_affected_capabilities")
    if has_window_gap and not affected_claim_types:
        raise ValueError("window_gap_requires_affected_claim_types")
    for dataset_id, watermark in sorted(dataset_watermarks.items()):
        if watermark < required_end:
            gaps.append(
                ContractGap(
                    gap_type="window_data_unavailable",
                    gap_id=(
                        f"{dataset_id}:target_day:{target_day.isoformat()}:"
                        f"watermark:{watermark.isoformat()}"
                    ),
                    dataset_id=dataset_id,
                    affected_capabilities=affected_capabilities,
                    affected_claim_types=affected_claim_types,
                    owner="data_owner",
                    repair_options=("wait_for_refresh",),
                    requires_clarification=False,
                    diagnostic_context={
                        "target_date": target_day.isoformat(),
                        "latest_complete_business_date": watermark.isoformat(),
                        "terminal_for_current_window": True,
                    },
                )
            )
    return WindowResolution(windows=tuple(windows), gaps=tuple(gaps))


def _validate_window_strategy_ids(
    values: tuple[str, ...],
    *,
    field: str,
) -> None:
    seen: set[str] = set()
    for window_id in values:
        if window_id in seen:
            raise ValueError(f"duplicate_{field}:{window_id}")
        seen.add(window_id)
        if window_id not in CANONICAL_WINDOW_STRATEGIES:
            raise ValueError(f"unsupported_{field}:{window_id}")


def _validated_context_window_specs(
    values: tuple[Mapping[str, Any], ...],
) -> tuple[ContextWindowSpec, ...]:
    if not isinstance(values, tuple):
        raise ValueError("context_window_specs_invalid:expected_tuple")
    output: list[ContextWindowSpec] = []
    seen_capabilities: set[str] = set()
    for index, value in enumerate(values):
        if not isinstance(value, Mapping) or set(value) != _CONTEXT_WINDOW_SPEC_FIELDS:
            raise ValueError(f"context_window_spec_invalid:{index}:shape")
        capability_id = str(value.get("capability_id") or "")
        relation = str(value.get("relation") or "")
        unit = str(value.get("unit") or "")
        count = value.get("count")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", capability_id):
            raise ValueError(f"context_window_spec_invalid:{index}:capability_id")
        if relation not in CONTEXT_WINDOW_RELATIONS:
            raise ValueError(f"context_window_spec_invalid:{index}:relation")
        if unit not in CONTEXT_WINDOW_UNITS:
            raise ValueError(f"context_window_spec_invalid:{index}:unit")
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ValueError(f"context_window_spec_invalid:{index}:count")
        if capability_id in seen_capabilities:
            raise ValueError(f"context_window_spec_invalid:{index}:duplicate")
        seen_capabilities.add(capability_id)
        output.append(
            ContextWindowSpec(
                capability_id=capability_id,
                relation=relation,
                unit=unit,
                count=count,
            )
        )
    return tuple(output)


def _resolve_context_window_spec(
    spec: ContextWindowSpec,
    *,
    target_day: date,
    timezone_name: str,
) -> ResolvedWindow:
    if spec.relation != "trailing_complete_periods":
        raise ValueError(f"unsupported_context_window_relation:{spec.relation}")
    end = _complete_period_boundary(target_day, spec.unit)
    if spec.unit == "day":
        start = end - timedelta(days=spec.count)
    elif spec.unit == "week":
        start = end - timedelta(weeks=spec.count)
    elif spec.unit == "month":
        start = _shift_months(end, -spec.count)
    elif spec.unit == "quarter":
        start = _shift_months(end, -(spec.count * 3))
    else:  # pragma: no cover - validated above
        raise ValueError(f"unsupported_context_window_unit:{spec.unit}")
    complete_days = (end - start).days
    if complete_days <= 0:
        raise ValueError("context_window_spec_invalid:empty_window")
    window_id = (
        f"context__{spec.capability_id}__{spec.relation}__"
        f"{spec.count}_{spec.unit}"
    )
    return ResolvedWindow(
        window_id=window_id,
        role="reference",
        label=f"{start.isoformat()}..{(end - timedelta(days=1)).isoformat()}",
        start_inclusive=start.isoformat(),
        end_exclusive=end.isoformat(),
        timezone=timezone_name,
        aggregation="mean_of_complete_days",
        required_complete_days=complete_days,
        source_watermark_requirement=(end - timedelta(days=1)).isoformat(),
        capability_refs=(spec.capability_id,),
    )


def _complete_period_boundary(target_day: date, unit: str) -> date:
    if unit == "day":
        return target_day
    if unit == "week":
        return target_day - timedelta(days=target_day.weekday())
    if unit == "month":
        return target_day.replace(day=1)
    if unit == "quarter":
        quarter_start_month = ((target_day.month - 1) // 3) * 3 + 1
        return target_day.replace(month=quarter_start_month, day=1)
    raise ValueError(f"unsupported_context_window_unit:{unit}")


def _shift_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 + months
    year, zero_based_month = divmod(month_index, 12)
    return value.replace(year=year, month=zero_based_month + 1, day=1)


def _resolve_reference_window(
    strategy: CanonicalWindowStrategy,
    *,
    target_day: date,
    timezone_name: str,
) -> ResolvedWindow:
    if strategy.strategy == "point_offset":
        day = target_day - timedelta(days=strategy.offset_days)
        return _day_window(
            strategy.window_id,
            "baseline",
            day,
            timezone_name,
        )
    if strategy.strategy != "trailing_complete_days":
        raise ValueError(
            f"unsupported_window_strategy:{strategy.window_id}:"
            f"{strategy.strategy}"
        )
    start = target_day - timedelta(days=strategy.complete_days)
    end = target_day - timedelta(days=1)
    return ResolvedWindow(
        window_id=strategy.window_id,
        role="baseline",
        label=f"{start.isoformat()}..{end.isoformat()}",
        start_inclusive=start.isoformat(),
        end_exclusive=target_day.isoformat(),
        timezone=timezone_name,
        aggregation=strategy.aggregation,
        required_complete_days=strategy.complete_days,
        source_watermark_requirement=end.isoformat(),
    )


def _apply_fixed_window_bounds(
    windows: list[ResolvedWindow],
    fixed_window_bounds: Mapping[str, tuple[str, str]],
    *,
    timezone_name: str,
) -> list[ResolvedWindow]:
    if not fixed_window_bounds:
        return windows
    by_id = {window.window_id: window for window in windows}
    allowed = {
        "target_day",
        *CANONICAL_WINDOW_STRATEGIES,
        "pattern_history",
        "anomaly_history",
        # Resume material may repeat the exact bounds of a context window that
        # was already derived from an accepted typed spec. It cannot introduce
        # a new dynamic window id.
        *by_id,
    }
    unknown = set(fixed_window_bounds) - allowed
    if unknown:
        raise ValueError(f"fixed_window_unknown:{sorted(unknown)[0]}")
    for window_id, raw_bounds in fixed_window_bounds.items():
        if (
            not isinstance(raw_bounds, (tuple, list))
            or len(raw_bounds) != 2
            or not all(isinstance(item, str) for item in raw_bounds)
        ):
            raise ValueError(f"fixed_window_invalid:{window_id}")
        try:
            start = date.fromisoformat(raw_bounds[0])
            end = date.fromisoformat(raw_bounds[1])
        except ValueError as exc:
            raise ValueError(f"fixed_window_invalid:{window_id}") from exc
        if start > end:
            raise ValueError(f"fixed_window_invalid:{window_id}")
        existing = by_id.get(window_id)
        if existing is not None:
            if (
                existing.start_inclusive != start.isoformat()
                or existing.end_exclusive != (end + timedelta(days=1)).isoformat()
            ):
                raise ValueError(f"fixed_window_mismatch:{window_id}")
            continue
        is_history = window_id in {"pattern_history", "anomaly_history"}
        strategy = CANONICAL_WINDOW_STRATEGIES.get(window_id)
        is_rolling = bool(
            strategy is not None
            and strategy.strategy == "trailing_complete_days"
        )
        window = ResolvedWindow(
            window_id=window_id,
            role=(
                "target"
                if window_id == "target_day"
                else "reference"
                if is_history
                else "baseline"
            ),
            label=(
                start.isoformat()
                if start == end
                else f"{start.isoformat()}..{end.isoformat()}"
            ),
            start_inclusive=start.isoformat(),
            end_exclusive=(end + timedelta(days=1)).isoformat(),
            timezone=timezone_name,
            aggregation=(
                "daily_series"
                if is_history
                else "mean_of_complete_days"
                if is_rolling
                else "daily_total"
            ),
            required_complete_days=(end - start).days + 1,
            source_watermark_requirement=end.isoformat(),
        )
        windows.append(window)
        by_id[window_id] = window
    return windows


def _resolve_target_day(value: str, local_day: date) -> date:
    if value in {"yesterday", "昨天"}:
        return local_day - timedelta(days=1)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"unsupported_target_semantic:{value}") from exc


def _day_window(window_id: str, role: str, day: date, timezone_name: str) -> ResolvedWindow:
    return ResolvedWindow(
        window_id=window_id,
        role=role,
        label=day.isoformat(),
        start_inclusive=day.isoformat(),
        end_exclusive=(day + timedelta(days=1)).isoformat(),
        timezone=timezone_name,
        aggregation="daily_total",
        required_complete_days=1,
        source_watermark_requirement=day.isoformat(),
    )
