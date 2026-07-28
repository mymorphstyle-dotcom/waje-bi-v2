from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import re
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from bi_agent.runtime.analysis_contracts import ContractGap, ResolvedWindow
from bi_agent.runtime.baseline_semantics import CANONICAL_BASELINE_IDS
from bi_agent.runtime.temporal_comparison import (
    EffectiveTemporalComparison,
    TemporalComparisonContractError,
    TemporalWindow,
)


class WindowResolutionError(ValueError):
    def __init__(self, kind: str, detail: str | None = None):
        if not isinstance(kind, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", kind):
            raise ValueError("window_resolution_error_kind_invalid")
        if detail is not None and (not isinstance(detail, str) or not detail):
            raise ValueError("window_resolution_error_detail_invalid")
        self.kind = kind
        self.detail = detail
        self.error_ref = kind if detail is None else f"{kind}:{detail}"
        super().__init__(self.error_ref)


@dataclass(frozen=True)
class WindowResolution:
    windows: tuple[ResolvedWindow, ...]
    gaps: tuple[ContractGap, ...]


@dataclass(frozen=True)
class ContextWindowSpec:
    capability_id: str
    relation: str
    unit: str
    count: int


CONTEXT_WINDOW_RELATIONS = frozenset(
    {"trailing_complete_periods", "evaluation_range"}
)
CONTEXT_WINDOW_UNITS = frozenset({"day", "week", "month", "quarter"})
_CONTEXT_WINDOW_SPEC_FIELDS = frozenset({"capability_id", "relation", "unit", "count"})


def resolve_temporal_windows(
    temporal_authority: EffectiveTemporalComparison,
    *,
    context_window_specs: tuple[Mapping[str, Any], ...],
    as_of: datetime,
    timezone_name: str,
    dataset_watermarks: Mapping[str, date],
    affected_capabilities: tuple[str, ...],
    affected_claim_types: tuple[str, ...],
) -> WindowResolution:
    """Resolve physical windows solely from accepted temporal authority."""

    temporal_authority = _validated_temporal_authority(temporal_authority)
    if temporal_authority.mode == "unresolved":
        raise WindowResolutionError("temporal_authority_unresolved")
    _validate_temporal_runtime_inputs(
        as_of=as_of,
        timezone_name=timezone_name,
        dataset_watermarks=dataset_watermarks,
    )
    context_specs = _validated_context_window_specs(context_window_specs)
    if temporal_authority.mode not in {
        "target_only",
        "window_pair",
        "calendar_partition",
        "event_relative",
    }:
        raise WindowResolutionError(
            "unsupported_temporal_mode",
            str(temporal_authority.mode),
        )

    target = _resolved_authority_window(
        temporal_authority.target_window,
        window_id="target_day",
        role="target",
        timezone_name=timezone_name,
    )
    windows = [target]
    if temporal_authority.mode in {"window_pair", "event_relative"}:
        baseline = temporal_authority.baseline_window
        if baseline is None:
            raise WindowResolutionError("temporal_baseline_window_missing")
        baseline_id = _temporal_baseline_window_id(temporal_authority)
        windows.append(
            _resolved_authority_window(
                baseline,
                window_id=baseline_id,
                role="baseline",
                timezone_name=timezone_name,
            )
        )
    elif temporal_authority.baseline_window is not None:
        raise WindowResolutionError("temporal_baseline_window_unexpected")

    target_start = date.fromisoformat(target.start_inclusive)
    for spec in context_specs:
        if spec.relation == "evaluation_range":
            if temporal_authority.mode != "calendar_partition":
                raise WindowResolutionError(
                    "unsupported_context_window_relation",
                    spec.relation,
                )
            windows.append(
                _resolve_evaluation_range_spec(
                    spec,
                    target=target,
                    timezone_name=timezone_name,
                )
            )
            continue
        if temporal_authority.mode == "calendar_partition":
            raise WindowResolutionError(
                "unsupported_context_window_relation",
                spec.relation,
            )
        windows.append(
            _resolve_context_window_spec(
                spec,
                target_day=target_start,
                timezone_name=timezone_name,
            )
        )

    gaps = _temporal_freshness_gaps(
        tuple(windows),
        dataset_watermarks=dataset_watermarks,
        affected_capabilities=affected_capabilities,
        affected_claim_types=affected_claim_types,
    )
    return WindowResolution(windows=tuple(windows), gaps=gaps)


def _validated_temporal_authority(
    temporal_authority: EffectiveTemporalComparison,
) -> EffectiveTemporalComparison:
    if type(temporal_authority) is not EffectiveTemporalComparison:
        raise WindowResolutionError("temporal_authority_invalid")
    try:
        return EffectiveTemporalComparison.from_dict(temporal_authority.to_dict())
    except TemporalComparisonContractError as exc:
        error = str(exc)
        if not re.fullmatch(r"[a-z][a-z0-9_]*", error):
            error = "temporal_authority_invalid"
        raise WindowResolutionError(error) from exc
    except (AttributeError, TypeError, ValueError) as exc:
        raise WindowResolutionError("temporal_authority_invalid") from exc


def _validate_temporal_runtime_inputs(
    *,
    as_of: datetime,
    timezone_name: str,
    dataset_watermarks: Mapping[str, date],
) -> None:
    if (
        not isinstance(as_of, datetime)
        or as_of.tzinfo is None
        or as_of.utcoffset() is None
    ):
        raise WindowResolutionError("temporal_as_of_invalid")
    if not isinstance(timezone_name, str) or not timezone_name:
        raise WindowResolutionError("temporal_timezone_invalid")
    try:
        as_of.astimezone(ZoneInfo(timezone_name))
    except (KeyError, ValueError) as exc:
        raise WindowResolutionError("temporal_timezone_invalid", timezone_name) from exc
    if not isinstance(dataset_watermarks, Mapping) or any(
        not isinstance(dataset_id, str) or not dataset_id or type(watermark) is not date
        for dataset_id, watermark in dataset_watermarks.items()
    ):
        raise WindowResolutionError("temporal_dataset_watermarks_invalid")


def _resolved_authority_window(
    authority_window: TemporalWindow,
    *,
    window_id: str,
    role: str,
    timezone_name: str,
) -> ResolvedWindow:
    if not isinstance(authority_window, TemporalWindow):
        raise WindowResolutionError("temporal_window_invalid", role)
    if authority_window.role != role or authority_window.boundary != "inclusive":
        raise WindowResolutionError("temporal_window_invalid", role)
    try:
        start = date.fromisoformat(str(authority_window.start or ""))
        end = date.fromisoformat(str(authority_window.end or ""))
    except ValueError as exc:
        raise WindowResolutionError("temporal_window_invalid", role) from exc
    if start > end:
        raise WindowResolutionError("temporal_window_invalid", role)
    complete_days = (end - start).days + 1
    aggregation = authority_window.aggregation
    if not isinstance(aggregation, str) or not aggregation:
        raise WindowResolutionError("temporal_window_aggregation_invalid", role)
    try:
        end_exclusive = end + timedelta(days=1)
    except OverflowError as exc:
        raise WindowResolutionError("temporal_window_out_of_range", role) from exc
    return ResolvedWindow(
        window_id=window_id,
        role=role,
        label=(
            start.isoformat()
            if start == end
            else f"{start.isoformat()}..{end.isoformat()}"
        ),
        start_inclusive=start.isoformat(),
        end_exclusive=end_exclusive.isoformat(),
        timezone=timezone_name,
        aggregation=aggregation,
        required_complete_days=complete_days,
        source_watermark_requirement=end.isoformat(),
    )


def _temporal_baseline_window_id(
    temporal_authority: EffectiveTemporalComparison,
) -> str:
    if not temporal_authority.baseline_ids:
        return "baseline_window"
    if len(temporal_authority.baseline_ids) != 1:
        raise WindowResolutionError("temporal_baseline_ids_invalid")
    baseline_id = temporal_authority.baseline_ids[0]
    if baseline_id not in CANONICAL_BASELINE_IDS:
        raise WindowResolutionError("temporal_baseline_id_invalid", baseline_id)
    baseline = temporal_authority.baseline_window
    if baseline is None or baseline.window_ref != f"window:baseline:{baseline_id}":
        raise WindowResolutionError("temporal_baseline_ref_invalid", baseline_id)
    return baseline_id


def _temporal_freshness_gaps(
    windows: tuple[ResolvedWindow, ...],
    *,
    dataset_watermarks: Mapping[str, date],
    affected_capabilities: tuple[str, ...],
    affected_claim_types: tuple[str, ...],
) -> tuple[ContractGap, ...]:
    required_end = max(
        date.fromisoformat(window.source_watermark_requirement) for window in windows
    )
    stale = {
        dataset_id: watermark
        for dataset_id, watermark in dataset_watermarks.items()
        if watermark < required_end
    }
    if stale and not affected_capabilities:
        raise WindowResolutionError("window_gap_requires_affected_capabilities")
    if stale and not affected_claim_types:
        raise WindowResolutionError("window_gap_requires_affected_claim_types")
    required_window_ids = tuple(window.window_id for window in windows)
    return tuple(
        ContractGap(
            gap_type="window_data_unavailable",
            gap_id=(
                f"{dataset_id}:execution_windows:{required_end.isoformat()}:"
                f"watermark:{watermark.isoformat()}"
            ),
            dataset_id=dataset_id,
            affected_capabilities=affected_capabilities,
            affected_claim_types=affected_claim_types,
            owner="data_owner",
            repair_options=("wait_for_refresh",),
            requires_clarification=False,
            diagnostic_context={
                "latest_required_business_date": required_end.isoformat(),
                "latest_complete_business_date": watermark.isoformat(),
                "required_window_ids": required_window_ids,
                "terminal_for_current_window": True,
            },
        )
        for dataset_id, watermark in sorted(stale.items())
    )


def _validated_context_window_specs(
    values: tuple[Mapping[str, Any], ...],
) -> tuple[ContextWindowSpec, ...]:
    if not isinstance(values, tuple):
        raise WindowResolutionError("context_window_specs_invalid", "expected_tuple")
    output: list[ContextWindowSpec] = []
    seen_capabilities: set[str] = set()
    for index, value in enumerate(values):
        if not isinstance(value, Mapping) or set(value) != _CONTEXT_WINDOW_SPEC_FIELDS:
            raise WindowResolutionError("context_window_spec_invalid", f"{index}:shape")
        capability_id = str(value.get("capability_id") or "")
        relation = str(value.get("relation") or "")
        unit = str(value.get("unit") or "")
        count = value.get("count")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", capability_id):
            raise WindowResolutionError(
                "context_window_spec_invalid", f"{index}:capability_id"
            )
        if relation not in CONTEXT_WINDOW_RELATIONS:
            raise WindowResolutionError(
                "context_window_spec_invalid", f"{index}:relation"
            )
        if unit not in CONTEXT_WINDOW_UNITS:
            raise WindowResolutionError("context_window_spec_invalid", f"{index}:unit")
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise WindowResolutionError("context_window_spec_invalid", f"{index}:count")
        if capability_id in seen_capabilities:
            raise WindowResolutionError(
                "context_window_spec_invalid", f"{index}:duplicate"
            )
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
        raise WindowResolutionError(
            "unsupported_context_window_relation", spec.relation
        )
    try:
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
            raise WindowResolutionError("unsupported_context_window_unit", spec.unit)
        last_complete_day = end - timedelta(days=1)
    except (OverflowError, ValueError) as exc:
        raise WindowResolutionError(
            "context_window_out_of_range", spec.capability_id
        ) from exc
    complete_days = (end - start).days
    if complete_days <= 0:
        raise WindowResolutionError("context_window_spec_invalid", "empty_window")
    window_id = (
        f"context__{spec.capability_id}__{spec.relation}__{spec.count}_{spec.unit}"
    )
    return ResolvedWindow(
        window_id=window_id,
        role="reference",
        label=f"{start.isoformat()}..{last_complete_day.isoformat()}",
        start_inclusive=start.isoformat(),
        end_exclusive=end.isoformat(),
        timezone=timezone_name,
        aggregation="mean_of_complete_days",
        required_complete_days=complete_days,
        source_watermark_requirement=last_complete_day.isoformat(),
        capability_refs=(spec.capability_id,),
    )


def _resolve_evaluation_range_spec(
    spec: ContextWindowSpec,
    *,
    target: ResolvedWindow,
    timezone_name: str,
) -> ResolvedWindow:
    try:
        start = date.fromisoformat(target.start_inclusive)
        end_exclusive = date.fromisoformat(target.end_exclusive)
    except ValueError as exc:
        raise WindowResolutionError(
            "context_window_out_of_range",
            spec.capability_id,
        ) from exc
    complete_days = (end_exclusive - start).days
    if (
        spec.unit != "day"
        or complete_days <= 0
        or complete_days != spec.count
    ):
        raise WindowResolutionError(
            "context_window_spec_invalid",
            f"{spec.capability_id}:evaluation_range",
        )
    last_complete_day = end_exclusive - timedelta(days=1)
    return ResolvedWindow(
        window_id=(
            f"context__{spec.capability_id}__{spec.relation}__"
            f"{spec.count}_{spec.unit}"
        ),
        role="reference",
        label=f"{start.isoformat()}..{last_complete_day.isoformat()}",
        start_inclusive=start.isoformat(),
        end_exclusive=end_exclusive.isoformat(),
        timezone=timezone_name,
        aggregation="daily_observations",
        required_complete_days=complete_days,
        source_watermark_requirement=last_complete_day.isoformat(),
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
    raise WindowResolutionError("unsupported_context_window_unit", unit)


def _shift_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 + months
    year, zero_based_month = divmod(month_index, 12)
    return value.replace(year=year, month=zero_based_month + 1, day=1)
