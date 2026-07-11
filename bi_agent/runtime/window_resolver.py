from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Mapping
from zoneinfo import ZoneInfo

from bi_agent.runtime.analysis_contracts import ContractGap, ResolvedWindow


CURRENT_DATA_BASELINES = (
    "previous_day",
    "rolling_7_day_baseline",
    "same_weekday_last_week",
)


@dataclass(frozen=True)
class WindowResolution:
    windows: tuple[ResolvedWindow, ...]
    gaps: tuple[ContractGap, ...]


def resolve_revenue_windows(
    *,
    target_semantic: str,
    baselines: tuple[str, ...],
    as_of: datetime,
    timezone_name: str,
    dataset_watermarks: Mapping[str, date],
    affected_capabilities: tuple[str, ...],
    affected_claim_types: tuple[str, ...],
    fixed_window_bounds: Mapping[str, tuple[str, str]] | None = None,
) -> WindowResolution:
    seen_baselines = set()
    for baseline in baselines:
        if baseline in seen_baselines:
            raise ValueError(f"duplicate_baseline:{baseline}")
        seen_baselines.add(baseline)
    local_day = as_of.astimezone(ZoneInfo(timezone_name)).date()
    target_day = _resolve_target_day(target_semantic, local_day)
    windows = [_day_window("target_day", "target", target_day, timezone_name)]
    for baseline in baselines:
        if baseline == "previous_day":
            windows.append(_day_window("previous_day", "baseline", target_day - timedelta(days=1), timezone_name))
        elif baseline == "same_weekday_last_week":
            windows.append(_day_window("same_weekday_last_week", "baseline", target_day - timedelta(days=7), timezone_name))
        elif baseline == "rolling_7_day_baseline":
            start = target_day - timedelta(days=7)
            end = target_day - timedelta(days=1)
            windows.append(
                ResolvedWindow(
                    window_id="rolling_7_day_baseline",
                    role="baseline",
                    label=f"{start.isoformat()}..{end.isoformat()}",
                    start_inclusive=start.isoformat(),
                    end_exclusive=target_day.isoformat(),
                    timezone=timezone_name,
                    aggregation="mean_of_complete_days",
                    required_complete_days=7,
                    source_watermark_requirement=end.isoformat(),
                )
            )
        else:
            raise ValueError(f"unsupported_baseline:{baseline}")
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
                    repair_options=("wait_for_refresh", "use_latest_complete_business_day"),
                    requires_clarification=True,
                )
            )
    return WindowResolution(windows=tuple(windows), gaps=tuple(gaps))


def _apply_fixed_window_bounds(
    windows: list[ResolvedWindow],
    fixed_window_bounds: Mapping[str, tuple[str, str]],
    *,
    timezone_name: str,
) -> list[ResolvedWindow]:
    if not fixed_window_bounds:
        return windows
    allowed = {
        "target_day",
        "previous_day",
        "rolling_7_day_baseline",
        "same_weekday_last_week",
        "pattern_history",
        "anomaly_history",
    }
    unknown = set(fixed_window_bounds) - allowed
    if unknown:
        raise ValueError(f"fixed_window_unknown:{sorted(unknown)[0]}")
    by_id = {window.window_id: window for window in windows}
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
        is_rolling = window_id == "rolling_7_day_baseline"
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
