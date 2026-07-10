from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Mapping
from zoneinfo import ZoneInfo

from bi_agent.runtime.analysis_contracts import ContractGap, ResolvedWindow


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
