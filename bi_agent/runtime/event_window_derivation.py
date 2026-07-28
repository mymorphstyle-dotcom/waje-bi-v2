from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value
from bi_agent.runtime.temporal_comparison import EffectiveTemporalComparison


EVENT_WINDOW_DERIVATION_POLICY_SCHEMA = "event-window-derivation-policy.v1"
EVENT_WINDOW_SET_SCHEMA = "derived-event-window-set.v1"
EVENT_WINDOW_SET_REF_PREFIX = "event-set:sha256:"
DERIVED_TEMPORAL_AUTHORITY_REF_PREFIX = "derived-event-window-set:sha256:"

_POLICY_FIELDS = {
    "schema_version",
    "source_dependency",
    "eligible_parent_modes",
    "occurrence_mode",
    "comparison_relation",
    "window_length",
    "aggregation",
    "evaluation_boundary",
    "maximum_occurrences",
}
_EVENT_ROW_DATE_FIELDS = ("event_start_date", "event_end_date")
_RECURRENCE_FIELDS = (
    "recurrence_month_start",
    "recurrence_day_start",
    "recurrence_month_end",
    "recurrence_day_end",
)


class EventWindowDerivationError(ValueError):
    pass


def validate_event_window_derivation_policy(
    value: Any,
    *,
    expected_source_dependency: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _POLICY_FIELDS:
        raise EventWindowDerivationError("event_window_derivation_policy_invalid")
    eligible_modes = value.get("eligible_parent_modes")
    maximum_occurrences = value.get("maximum_occurrences")
    source_dependency = value.get("source_dependency")
    normalized_eligible_modes = (
        tuple(str(item) for item in eligible_modes)
        if not isinstance(eligible_modes, (str, bytes))
        and isinstance(eligible_modes, Sequence)
        else ()
    )
    if (
        value.get("schema_version") != EVENT_WINDOW_DERIVATION_POLICY_SCHEMA
        or not isinstance(source_dependency, str)
        or not source_dependency
        or source_dependency != source_dependency.strip()
        or (
            expected_source_dependency is not None
            and source_dependency != expected_source_dependency
        )
        or normalized_eligible_modes != ("calendar_partition",)
        or value.get("occurrence_mode") != "expand_reviewed_recurrence"
        or value.get("comparison_relation") != "post_event_vs_pre_event"
        or value.get("window_length") != "event_duration"
        or value.get("aggregation")
        != "inherit_parent_complete_day_aggregation"
        or value.get("evaluation_boundary") != "require_complete_windows"
        or isinstance(maximum_occurrences, bool)
        or not isinstance(maximum_occurrences, int)
        or not 1 <= maximum_occurrences <= 5000
    ):
        raise EventWindowDerivationError("event_window_derivation_policy_invalid")
    return {
        **dict(value),
        "eligible_parent_modes": list(normalized_eligible_modes),
    }


def derive_event_window_set(
    events: Iterable[Mapping[str, Any]],
    *,
    temporal_authority: EffectiveTemporalComparison,
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    normalized_policy = validate_event_window_derivation_policy(policy)
    evaluation_start, evaluation_end, aggregation = _evaluation_material(
        temporal_authority,
        policy=normalized_policy,
    )
    rows = _validated_real_event_rows(events)
    occurrences: list[dict[str, Any]] = []
    excluded_counts: dict[str, int] = {}
    for row in rows:
        source_digest = canonical_digest(row)
        for occurrence_start, occurrence_end in _event_occurrences(
            row,
            evaluation_start=evaluation_start,
            evaluation_end=evaluation_end,
        ):
            duration_days = (occurrence_end - occurrence_start).days + 1
            baseline_start = occurrence_start - timedelta(days=duration_days)
            baseline_end = occurrence_start - timedelta(days=1)
            target_start = occurrence_end + timedelta(days=1)
            target_end = occurrence_end + timedelta(days=duration_days)
            if (
                baseline_start < evaluation_start
                or target_end > evaluation_end
            ):
                excluded_counts["incomplete_evaluation_boundary"] = (
                    excluded_counts.get("incomplete_evaluation_boundary", 0) + 1
                )
                continue
            occurrence_material = {
                "source_event_digest": source_digest,
                "source_family": str(row.get("source_family") or ""),
                "event_type": str(row.get("event_type") or ""),
                "affected_scope": str(row.get("affected_scope") or ""),
                "authority": str(row.get("authority") or ""),
                "evidence_level": str(row.get("evidence_level") or ""),
                "wording_limit": str(row.get("wording_limit") or ""),
                "event_start_date": occurrence_start.isoformat(),
                "event_end_date": occurrence_end.isoformat(),
                "baseline_start_date": baseline_start.isoformat(),
                "baseline_end_date": baseline_end.isoformat(),
                "target_start_date": target_start.isoformat(),
                "target_end_date": target_end.isoformat(),
                "required_complete_days": duration_days,
                "aggregation": aggregation,
            }
            occurrences.append(
                {
                    "occurrence_ref": "event-occurrence:sha256:"
                    + canonical_digest(occurrence_material),
                    **occurrence_material,
                }
            )
    occurrences.sort(
        key=lambda item: (
            item["event_start_date"],
            item["event_end_date"],
            item["source_family"],
            item["event_type"],
            item["occurrence_ref"],
        )
    )
    if len(occurrences) > normalized_policy["maximum_occurrences"]:
        raise EventWindowDerivationError(
            "event_window_occurrence_bound_exceeded:"
            + str(normalized_policy["maximum_occurrences"])
        )
    event_identity_material = {
        "schema_version": "derived-event-set-identity.v1",
        "source_event_digests": sorted(
            {str(item["source_event_digest"]) for item in occurrences}
        ),
        "occurrence_refs": [str(item["occurrence_ref"]) for item in occurrences],
    }
    event_ref = EVENT_WINDOW_SET_REF_PREFIX + canonical_digest(
        event_identity_material
    )
    temporal_material = {
        "schema_version": EVENT_WINDOW_SET_SCHEMA,
        "source_temporal_authority_ref": temporal_authority.authority_ref,
        "derivation_policy": normalized_policy,
        "event_ref": event_ref,
        "occurrences": occurrences,
    }
    temporal_authority_ref = DERIVED_TEMPORAL_AUTHORITY_REF_PREFIX + canonical_digest(
        temporal_material
    )
    material = {
        **temporal_material,
        "temporal_authority_ref": temporal_authority_ref,
        "evaluation_window": {
            "start_date": evaluation_start.isoformat(),
            "end_date": evaluation_end.isoformat(),
            "aggregation": aggregation,
        },
        "source_event_count": len(rows),
        "derived_occurrence_count": len(occurrences),
        "excluded_occurrence_counts": dict(sorted(excluded_counts.items())),
    }
    return {
        **material,
        "content_digest": canonical_digest(material),
    }


def validate_event_window_set(
    value: Any,
    *,
    temporal_authority: EffectiveTemporalComparison,
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise EventWindowDerivationError("event_window_set_invalid")
    expected_fields = {
        "schema_version",
        "source_temporal_authority_ref",
        "derivation_policy",
        "event_ref",
        "occurrences",
        "temporal_authority_ref",
        "evaluation_window",
        "source_event_count",
        "derived_occurrence_count",
        "excluded_occurrence_counts",
        "content_digest",
    }
    if set(value) != expected_fields or value.get("schema_version") != (
        EVENT_WINDOW_SET_SCHEMA
    ):
        raise EventWindowDerivationError("event_window_set_invalid")
    normalized_policy = validate_event_window_derivation_policy(policy)
    if canonical_value(value.get("derivation_policy")) != canonical_value(
        normalized_policy
    ):
        raise EventWindowDerivationError("event_window_set_policy_mismatch")
    evaluation_start, evaluation_end, aggregation = _evaluation_material(
        temporal_authority,
        policy=normalized_policy,
    )
    evaluation_window = value.get("evaluation_window")
    occurrences = value.get("occurrences")
    source_event_count = value.get("source_event_count")
    derived_occurrence_count = value.get("derived_occurrence_count")
    excluded = value.get("excluded_occurrence_counts")
    if (
        value.get("source_temporal_authority_ref")
        != temporal_authority.authority_ref
        or not isinstance(evaluation_window, Mapping)
        or evaluation_window
        != {
            "start_date": evaluation_start.isoformat(),
            "end_date": evaluation_end.isoformat(),
            "aggregation": aggregation,
        }
        or isinstance(occurrences, (str, bytes))
        or not isinstance(occurrences, Sequence)
        or any(not isinstance(item, Mapping) for item in occurrences)
        or isinstance(source_event_count, bool)
        or not isinstance(source_event_count, int)
        or source_event_count < 0
        or isinstance(derived_occurrence_count, bool)
        or not isinstance(derived_occurrence_count, int)
        or derived_occurrence_count != len(occurrences)
        or derived_occurrence_count > normalized_policy["maximum_occurrences"]
        or not isinstance(excluded, Mapping)
        or any(
            not isinstance(key, str)
            or not key
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 1
            for key, count in excluded.items()
        )
    ):
        raise EventWindowDerivationError("event_window_set_invalid")
    normalized_occurrences = tuple(
        _validate_derived_occurrence(
            item,
            evaluation_start=evaluation_start,
            evaluation_end=evaluation_end,
            aggregation=aggregation,
        )
        for item in occurrences
    )
    if tuple(
        sorted(
            normalized_occurrences,
            key=lambda item: (
                item["event_start_date"],
                item["event_end_date"],
                item["source_family"],
                item["event_type"],
                item["occurrence_ref"],
            ),
        )
    ) != normalized_occurrences:
        raise EventWindowDerivationError("event_window_set_order_invalid")
    event_identity_material = {
        "schema_version": "derived-event-set-identity.v1",
        "source_event_digests": sorted(
            {str(item["source_event_digest"]) for item in normalized_occurrences}
        ),
        "occurrence_refs": [
            str(item["occurrence_ref"]) for item in normalized_occurrences
        ],
    }
    event_ref = EVENT_WINDOW_SET_REF_PREFIX + canonical_digest(
        event_identity_material
    )
    temporal_material = {
        "schema_version": EVENT_WINDOW_SET_SCHEMA,
        "source_temporal_authority_ref": temporal_authority.authority_ref,
        "derivation_policy": normalized_policy,
        "event_ref": event_ref,
        "occurrences": list(normalized_occurrences),
    }
    temporal_authority_ref = DERIVED_TEMPORAL_AUTHORITY_REF_PREFIX + canonical_digest(
        temporal_material
    )
    if (
        value.get("event_ref") != event_ref
        or value.get("temporal_authority_ref") != temporal_authority_ref
    ):
        raise EventWindowDerivationError("event_window_set_identity_invalid")
    normalized = {
        **dict(value),
        "derivation_policy": normalized_policy,
        "occurrences": list(normalized_occurrences),
        "excluded_occurrence_counts": dict(sorted(excluded.items())),
    }
    content_material = dict(normalized)
    supplied_digest = content_material.pop("content_digest")
    if supplied_digest != canonical_digest(content_material):
        raise EventWindowDerivationError("event_window_set_digest_invalid")
    return normalized


def _evaluation_material(
    temporal_authority: EffectiveTemporalComparison,
    *,
    policy: Mapping[str, Any],
) -> tuple[date, date, str]:
    if (
        not isinstance(temporal_authority, EffectiveTemporalComparison)
        or temporal_authority.mode not in policy["eligible_parent_modes"]
        or temporal_authority.mode != "calendar_partition"
        or temporal_authority.target_window.start is None
        or temporal_authority.target_window.end is None
        or not isinstance(temporal_authority.calendar_partition, Mapping)
    ):
        raise EventWindowDerivationError(
            "event_window_parent_temporal_authority_invalid"
        )
    try:
        evaluation_start = date.fromisoformat(
            temporal_authority.target_window.start
        )
        evaluation_end = date.fromisoformat(temporal_authority.target_window.end)
    except ValueError as exc:
        raise EventWindowDerivationError(
            "event_window_parent_temporal_authority_invalid"
        ) from exc
    aggregation = str(
        temporal_authority.calendar_partition.get("aggregation") or ""
    )
    if aggregation not in {
        "sum_of_complete_days",
        "mean_of_complete_days",
    }:
        raise EventWindowDerivationError(
            "event_window_parent_aggregation_invalid"
        )
    return evaluation_start, evaluation_end, aggregation


def _validated_real_event_rows(
    events: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    output: list[dict[str, Any]] = []
    seen_event_ids: set[str] = set()
    for raw in events:
        if not isinstance(raw, Mapping):
            raise EventWindowDerivationError("event_window_source_row_invalid")
        event_id = str(raw.get("event_id") or "")
        event_count = raw.get("event_count")
        if event_id.startswith("__no_event__:"):
            if event_count != 0:
                raise EventWindowDerivationError(
                    "event_window_source_sentinel_invalid"
                )
            continue
        if (
            not event_id
            or event_id in seen_event_ids
            or isinstance(event_count, bool)
            or event_count != 1
        ):
            raise EventWindowDerivationError("event_window_source_row_invalid")
        normalized = dict(raw)
        for field in _EVENT_ROW_DATE_FIELDS:
            normalized[field] = _date_value(
                raw.get(field),
                "event_window_source_date_invalid",
            ).isoformat()
        seen_event_ids.add(event_id)
        output.append(normalized)
    output.sort(key=lambda item: str(item["event_id"]))
    return tuple(output)


def _event_occurrences(
    row: Mapping[str, Any],
    *,
    evaluation_start: date,
    evaluation_end: date,
) -> tuple[tuple[date, date], ...]:
    active_start = max(
        _date_value(
            row.get("event_start_date"),
            "event_window_source_date_invalid",
        ),
        evaluation_start,
    )
    active_end = min(
        _date_value(
            row.get("event_end_date"),
            "event_window_source_date_invalid",
        ),
        evaluation_end,
    )
    if active_start > active_end:
        return ()
    kind = str(row.get("recurrence_kind") or "")
    if not kind:
        return ((active_start, active_end),)
    recurrence_values = tuple(row.get(field) for field in _RECURRENCE_FIELDS)
    if any(
        isinstance(item, bool) or not isinstance(item, int)
        for item in recurrence_values
    ):
        raise EventWindowDerivationError("event_window_recurrence_invalid")
    month_start, day_start, month_end, day_end = recurrence_values
    if kind == "monthly_day_range":
        if month_start != 0 or month_end != 0 or not (
            1 <= day_start <= day_end <= 31
        ):
            raise EventWindowDerivationError("event_window_recurrence_invalid")
        output = []
        year, month = active_start.year, active_start.month
        while (year, month) <= (active_end.year, active_end.month):
            last_day = monthrange(year, month)[1]
            if day_start <= last_day:
                occurrence_start = date(year, month, day_start)
                occurrence_end = date(year, month, min(day_end, last_day))
                if occurrence_start >= active_start and occurrence_end <= active_end:
                    output.append((occurrence_start, occurrence_end))
            if month == 12:
                year, month = year + 1, 1
            else:
                month += 1
        return tuple(output)
    if kind != "annual_month_day_range":
        raise EventWindowDerivationError("event_window_recurrence_invalid")
    output = []
    for start_year in range(active_start.year - 1, active_end.year + 1):
        occurrence_start = _safe_date(start_year, month_start, day_start)
        end_year = start_year + int(
            (month_end, day_end) < (month_start, day_start)
        )
        occurrence_end = _safe_date(end_year, month_end, day_end)
        if (
            occurrence_start is not None
            and occurrence_end is not None
            and occurrence_start >= active_start
            and occurrence_end <= active_end
        ):
            output.append((occurrence_start, occurrence_end))
    return tuple(output)


def _validate_derived_occurrence(
    value: Mapping[str, Any],
    *,
    evaluation_start: date,
    evaluation_end: date,
    aggregation: str,
) -> dict[str, Any]:
    expected_fields = {
        "occurrence_ref",
        "source_event_digest",
        "source_family",
        "event_type",
        "affected_scope",
        "authority",
        "evidence_level",
        "wording_limit",
        "event_start_date",
        "event_end_date",
        "baseline_start_date",
        "baseline_end_date",
        "target_start_date",
        "target_end_date",
        "required_complete_days",
        "aggregation",
    }
    if set(value) != expected_fields:
        raise EventWindowDerivationError("event_window_occurrence_invalid")
    required_days = value.get("required_complete_days")
    if (
        any(
            not isinstance(value.get(field), str)
            for field in expected_fields
            - {"required_complete_days"}
        )
        or isinstance(required_days, bool)
        or not isinstance(required_days, int)
        or required_days < 1
        or value.get("aggregation") != aggregation
    ):
        raise EventWindowDerivationError("event_window_occurrence_invalid")
    event_start = _date_value(
        value["event_start_date"],
        "event_window_occurrence_invalid",
    )
    event_end = _date_value(
        value["event_end_date"],
        "event_window_occurrence_invalid",
    )
    baseline_start = _date_value(
        value["baseline_start_date"],
        "event_window_occurrence_invalid",
    )
    baseline_end = _date_value(
        value["baseline_end_date"],
        "event_window_occurrence_invalid",
    )
    target_start = _date_value(
        value["target_start_date"],
        "event_window_occurrence_invalid",
    )
    target_end = _date_value(
        value["target_end_date"],
        "event_window_occurrence_invalid",
    )
    if (
        (event_end - event_start).days + 1 != required_days
        or baseline_start != event_start - timedelta(days=required_days)
        or baseline_end != event_start - timedelta(days=1)
        or target_start != event_end + timedelta(days=1)
        or target_end != event_end + timedelta(days=required_days)
        or baseline_start < evaluation_start
        or target_end > evaluation_end
    ):
        raise EventWindowDerivationError("event_window_occurrence_invalid")
    material = dict(value)
    occurrence_ref = material.pop("occurrence_ref")
    if occurrence_ref != "event-occurrence:sha256:" + canonical_digest(material):
        raise EventWindowDerivationError("event_window_occurrence_identity_invalid")
    return dict(value)


def _date_value(value: Any, error: str) -> date:
    try:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value or ""))
    except ValueError as exc:
        raise EventWindowDerivationError(error) from exc


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


__all__ = [
    "DERIVED_TEMPORAL_AUTHORITY_REF_PREFIX",
    "EVENT_WINDOW_DERIVATION_POLICY_SCHEMA",
    "EVENT_WINDOW_SET_REF_PREFIX",
    "EVENT_WINDOW_SET_SCHEMA",
    "EventWindowDerivationError",
    "derive_event_window_set",
    "validate_event_window_derivation_policy",
    "validate_event_window_set",
]
