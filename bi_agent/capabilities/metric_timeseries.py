from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from bi_agent.capabilities import make_evidence_envelope


class MetricTimeseriesError(ValueError):
    pass


@dataclass(frozen=True)
class MetricTimeseriesPoint:
    window_id: str
    window_role: str
    observation_key: str
    value: str

    def to_dict(self) -> dict[str, str]:
        return {
            "window_id": self.window_id,
            "window_role": self.window_role,
            "observation_key": self.observation_key,
            "value": self.value,
        }


def metric_timeseries(
    rows: Iterable[Mapping[str, Any]],
    *,
    metric_id: str,
    time_key: str = "observation_key",
    window_id_key: str = "window_id",
    window_role_key: str = "window_role",
    result_refs: tuple[str, ...] = (),
):
    """Preserve one validated aggregate metric point per window observation.

    This primitive does not infer a trend or recurring pattern. Those claims
    belong to downstream capabilities with their own comparison contracts.
    """

    metric_id = _required_name(metric_id, "metric_timeseries_metric_id_invalid")
    time_key = _required_name(time_key, "metric_timeseries_time_key_invalid")
    window_id_key = _required_name(
        window_id_key, "metric_timeseries_window_id_key_invalid"
    )
    window_role_key = _required_name(
        window_role_key, "metric_timeseries_window_role_key_invalid"
    )
    points: list[MetricTimeseriesPoint] = []
    identities: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise MetricTimeseriesError("metric_timeseries_row_invalid")
        window_id = _required_name(
            row.get(window_id_key), "metric_timeseries_window_id_missing"
        )
        window_role = _required_name(
            row.get(window_role_key), "metric_timeseries_window_role_missing"
        )
        observation_key = _observation_key(row.get(time_key))
        if metric_id not in row or row.get(metric_id) is None:
            raise MetricTimeseriesError("metric_timeseries_value_missing")
        value = _decimal(row.get(metric_id))
        identity = (window_id, observation_key)
        if identity in identities:
            raise MetricTimeseriesError("metric_timeseries_observation_duplicate")
        identities.add(identity)
        points.append(
            MetricTimeseriesPoint(
                window_id=window_id,
                window_role=window_role,
                observation_key=observation_key,
                value=str(value),
            )
        )
    if not points:
        raise MetricTimeseriesError("metric_timeseries_rows_empty")
    points.sort(
        key=lambda item: (item.observation_key, item.window_id, item.window_role)
    )
    windows: dict[tuple[str, str], list[MetricTimeseriesPoint]] = {}
    for point in points:
        windows.setdefault((point.window_id, point.window_role), []).append(point)
    series = tuple(
        {
            "window_id": window_id,
            "window_role": window_role,
            "points": tuple(point.to_dict() for point in window_points),
        }
        for (window_id, window_role), window_points in sorted(windows.items())
    )
    return make_evidence_envelope(
        "metric_timeseries",
        evidence_type="observed_comparison",
        strength="descriptive",
        wording_limit="aggregate_timeseries_only",
        numeric_facts={"point_count": len(points), "window_count": len(series)},
        typed_payload={
            "metric_id": metric_id,
            "value_semantics": "decimal_string",
            "point_count": len(points),
            "window_count": len(series),
            "points": tuple(point.to_dict() for point in points),
            "series": series,
            "trend_claim_allowed": False,
            "recurring_pattern_claim_allowed": False,
        },
        limitations=(),
        result_refs=result_refs,
    )


def _required_name(value: Any, error: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MetricTimeseriesError(error)
    return value


def _observation_key(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return _required_name(value, "metric_timeseries_observation_key_missing")


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise MetricTimeseriesError("metric_timeseries_value_invalid")
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MetricTimeseriesError("metric_timeseries_value_invalid") from exc
    if not normalized.is_finite():
        raise MetricTimeseriesError("metric_timeseries_value_invalid")
    return normalized


__all__ = (
    "MetricTimeseriesError",
    "MetricTimeseriesPoint",
    "metric_timeseries",
)
