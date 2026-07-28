from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import math
from types import MappingProxyType
from typing import Any, Mapping

from bi_agent.runtime.baseline_semantics import (
    BaselineSemanticError,
    canonical_baseline_aggregation,
    canonical_baseline_bounds,
    canonical_baseline_ids,
)
from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value


TIME_SPEC_KINDS = frozenset({"date", "date_range", "relative", "period", "custom"})
COMPARISON_KINDS = frozenset(
    {
        "none",
        "decision_slot",
        "fixed_window",
        "calendar_partition",
        "event_relative_window",
    }
)
FIXED_WINDOW_BASELINE_CLASSES = frozenset(
    {"prior_period", "same_period_last_year", "custom_control_window"}
)
WINDOW_AGGREGATIONS = frozenset({"sum_of_complete_days", "mean_of_complete_days"})
TEMPORAL_WINDOW_AGGREGATIONS = frozenset({"daily_total", *WINDOW_AGGREGATIONS})
CALENDAR_PARTITION_CONTRACTS: Mapping[str, tuple[str, frozenset[Any]]] = (
    MappingProxyType(
        {
            "quarter_of_year": ("year", frozenset({"Q1", "Q2", "Q3", "Q4"})),
            "month_of_year": ("year", frozenset(range(1, 13))),
            "month_phase": ("month", frozenset({"start", "mid", "end"})),
            "iso_weekday": ("week", frozenset(range(1, 8))),
        }
    )
)
_CALENDAR_MEMBER_ORDER: Mapping[str, tuple[Any, ...]] = MappingProxyType(
    {
        "quarter_of_year": ("Q1", "Q2", "Q3", "Q4"),
        "month_of_year": tuple(range(1, 13)),
        "month_phase": ("start", "mid", "end"),
        "iso_weekday": tuple(range(1, 8)),
    }
)
_CALENDAR_BASELINE_CLASSES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "quarter_of_year": frozenset({"prior_period", "custom_control_window"}),
        "month_of_year": frozenset({"prior_period", "custom_control_window"}),
        "month_phase": frozenset({"same_month_phase"}),
        "iso_weekday": frozenset({"custom_control_window"}),
    }
)
COMPARISON_DECISION_SLOT_IDS = frozenset(
    {
        "comparison_baseline",
        "comparison_window",
        "comparison_interpretation",
        "event_relative_window",
    }
)
COMPARISON_OVERRIDE_SLOT_IDS = frozenset(
    {
        "month_phase_definition",
        "phase_aggregation",
    }
)
MONTH_PHASE_DEFINITION_VALUE_REFS = (
    "definition_1",
    "definition_2",
    "definition_3",
)
PHASE_AGGREGATION_VALUE_REFS = (
    "sum_of_complete_days",
    "mean_of_complete_days",
)
COMPARISON_WINDOW_VALUE_REFS = (
    "prior_period",
    "same_period_last_year",
    "same_month_phase",
    "custom_control_window",
)
TEMPORAL_MODES = frozenset(
    {
        "target_only",
        "window_pair",
        "calendar_partition",
        "event_relative",
        "unresolved",
    }
)
ROLLING_WINDOW_PARAMETER_FIELDS = frozenset(
    {
        "materiality_floor",
        "rolling_span_policy",
        "minimum_span_days",
        "rolling_step_policy",
        "min_periods",
    }
)


class TemporalComparisonContractError(ValueError):
    pass


@dataclass(frozen=True)
class RollingWindowStrategy:
    materiality_floor: float
    rolling_span_days: int
    rolling_step_days: int
    min_periods: int
    context_days: int
    context_limited: bool

    def capability_parameters(self) -> dict[str, Any]:
        return {
            "materiality_floor": self.materiality_floor,
            "rolling_span_days": self.rolling_span_days,
            "rolling_step_days": self.rolling_step_days,
            "min_periods": self.min_periods,
        }


def _required_string(value: Any, error: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise TemporalComparisonContractError(error)
    return value


def _canonical_date(value: Any, error: str) -> str:
    raw = _required_string(value, error)
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise TemporalComparisonContractError(error) from exc
    if parsed.isoformat() != raw:
        raise TemporalComparisonContractError(error)
    return raw


def _date_bounds(
    *,
    start: Any,
    end: Any,
    error: str,
) -> tuple[str, str]:
    normalized_start = _canonical_date(start, error)
    normalized_end = _canonical_date(end, error)
    if normalized_start > normalized_end:
        raise TemporalComparisonContractError(error)
    return normalized_start, normalized_end


def _deep_freeze(value: Any) -> Any:
    normalized = canonical_value(value)
    if isinstance(normalized, Mapping):
        return MappingProxyType(
            {str(key): _deep_freeze(item) for key, item in normalized.items()}
        )
    if isinstance(normalized, list):
        return tuple(_deep_freeze(item) for item in normalized)
    return normalized


def _immutable_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    frozen = _deep_freeze(value)
    if not isinstance(frozen, Mapping):  # pragma: no cover - typed caller boundary
        raise TemporalComparisonContractError("temporal_mapping_invalid")
    return frozen


def _calendar_member(
    value: date,
    partition_field: str,
    *,
    month_phase_member_definitions: tuple[Mapping[str, Any], ...] = (),
) -> Any:
    if partition_field == "quarter_of_year":
        return f"Q{((value.month - 1) // 3) + 1}"
    if partition_field == "month_of_year":
        return value.month
    if partition_field == "month_phase":
        for definition in month_phase_member_definitions:
            if int(definition["day_start"]) <= value.day <= int(
                definition["day_end"]
            ):
                return str(definition["member"])
        raise TemporalComparisonContractError("temporal_comparison_spec_invalid")
    if partition_field == "iso_weekday":
        return value.isoweekday()
    raise TemporalComparisonContractError("temporal_comparison_spec_invalid")


def _validate_calendar_range_coverage(
    *,
    time_spec: Mapping[str, Any],
    partition_field: str,
    target_members: tuple[Any, ...],
    baseline_members: tuple[Any, ...],
    month_phase_member_definitions: tuple[Mapping[str, Any], ...] = (),
    error: str,
) -> None:
    bounds = target_bounds(time_spec)
    if bounds is None:
        raise TemporalComparisonContractError(error)
    start = date.fromisoformat(bounds[0])
    end = date.fromisoformat(bounds[1])
    days_to_check = min((end - start).days + 1, 366)
    observed_members = {
        _calendar_member(
            start + timedelta(days=offset),
            partition_field,
            month_phase_member_definitions=month_phase_member_definitions,
        )
        for offset in range(days_to_check)
    }
    if not observed_members.intersection(target_members) or not (
        observed_members.intersection(baseline_members)
    ):
        raise TemporalComparisonContractError(error)


def validate_time_spec(value: Any) -> Mapping[str, Any]:
    """Validate and normalize the exact IntentRevision time_spec union."""

    error = "temporal_time_spec_invalid"
    if not isinstance(value, Mapping):
        raise TemporalComparisonContractError(error)
    kind = value.get("kind")
    shapes = {
        "date": {"kind", "target"},
        "date_range": {"kind", "start", "end"},
        "relative": {"kind", "reference"},
        "period": {"kind", "period_ref"},
        "custom": {"kind", "expression"},
    }
    if (
        not isinstance(kind, str)
        or kind not in TIME_SPEC_KINDS
        or set(value) != shapes[kind]
    ):
        raise TemporalComparisonContractError(error)
    if kind == "date":
        return _immutable_mapping(
            {"kind": "date", "target": _canonical_date(value["target"], error)}
        )
    if kind == "date_range":
        start, end = _date_bounds(start=value["start"], end=value["end"], error=error)
        return _immutable_mapping({"kind": "date_range", "start": start, "end": end})
    field = {
        "relative": "reference",
        "period": "period_ref",
        "custom": "expression",
    }[kind]
    return _immutable_mapping(
        {"kind": kind, field: _required_string(value[field], error)}
    )


def target_bounds(time_spec: Mapping[str, Any]) -> tuple[str, str] | None:
    normalized = validate_time_spec(time_spec)
    if normalized["kind"] == "date":
        target = str(normalized["target"])
        return target, target
    if normalized["kind"] == "date_range":
        return str(normalized["start"]), str(normalized["end"])
    return None


def target_window_ref(time_spec: Mapping[str, Any]) -> str:
    normalized = validate_time_spec(time_spec)
    if normalized["kind"] == "date":
        return f"window:target:{normalized['target']}"
    if normalized["kind"] == "date_range":
        return f"window:target:{normalized['start']}:{normalized['end']}"
    return "window:target:sha256:" + canonical_digest(normalized)


def _validate_aggregation(value: Any, error: str) -> str:
    if not isinstance(value, str) or value not in WINDOW_AGGREGATIONS:
        raise TemporalComparisonContractError(error)
    return str(value)


def _validate_calendar_members(
    value: Any,
    *,
    allowed: frozenset[Any],
    member_order: tuple[Any, ...],
    error: str,
) -> tuple[Any, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise TemporalComparisonContractError(error)
    members = tuple(value)
    if any(
        isinstance(member, bool)
        or not isinstance(member, (str, int))
        or member not in allowed
        for member in members
    ):
        raise TemporalComparisonContractError(error)
    if len(members) != len(set(members)):
        raise TemporalComparisonContractError(error)
    return tuple(member for member in member_order if member in set(members))


def validate_month_phase_member_definitions(
    value: Any,
) -> tuple[Mapping[str, Any], ...]:
    """Validate explicit, SQL-affecting month-phase membership."""

    error = "temporal_month_phase_member_definitions_invalid"
    if (
        isinstance(value, (str, bytes))
        or not isinstance(value, (list, tuple))
        or len(value) != 3
        or any(not isinstance(item, Mapping) for item in value)
    ):
        raise TemporalComparisonContractError(error)
    normalized: list[Mapping[str, Any]] = []
    expected_member_order = ("start", "mid", "end")
    expected_start = 1
    for index, item in enumerate(value):
        if set(item) != {"member", "day_start", "day_end"}:
            raise TemporalComparisonContractError(error)
        member = item.get("member")
        day_start = item.get("day_start")
        day_end = item.get("day_end")
        if (
            member != expected_member_order[index]
            or isinstance(day_start, bool)
            or isinstance(day_end, bool)
            or not isinstance(day_start, int)
            or not isinstance(day_end, int)
            or day_start != expected_start
            or day_end < day_start
            or day_end > 31
        ):
            raise TemporalComparisonContractError(error)
        normalized.append(
            _immutable_mapping(
                {
                    "member": str(member),
                    "day_start": day_start,
                    "day_end": day_end,
                }
            )
        )
        expected_start = day_end + 1
    if expected_start != 32:
        raise TemporalComparisonContractError(error)
    return tuple(normalized)


def calendar_partition_llm_contracts() -> dict[str, dict[str, Any]]:
    """Return the exact field combinations the LLM may bind."""

    return {
        partition_field: {
            "period_grain": contract[0],
            "baseline_classes": sorted(
                _CALENDAR_BASELINE_CLASSES[partition_field]
            ),
            "members": list(_CALENDAR_MEMBER_ORDER[partition_field]),
        }
        for partition_field, contract in CALENDAR_PARTITION_CONTRACTS.items()
    }


def validate_calendar_partition_role_frame(
    value: Any,
) -> Mapping[str, Any]:
    error = "temporal_calendar_partition_role_frame_invalid"
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "partition_field",
        "target_members",
        "baseline_members",
        "aggregation",
        "member_definitions",
    }:
        raise TemporalComparisonContractError(error)
    if value.get("schema_version") != "calendar-partition-role-frame.v1":
        raise TemporalComparisonContractError(error)
    partition_field = value.get("partition_field")
    contract = CALENDAR_PARTITION_CONTRACTS.get(str(partition_field))
    if contract is None:
        raise TemporalComparisonContractError(error)
    target_members = _validate_calendar_members(
        value.get("target_members"),
        allowed=contract[1],
        member_order=_CALENDAR_MEMBER_ORDER[str(partition_field)],
        error=error,
    )
    baseline_members = _validate_calendar_members(
        value.get("baseline_members"),
        allowed=contract[1],
        member_order=_CALENDAR_MEMBER_ORDER[str(partition_field)],
        error=error,
    )
    if set(target_members).intersection(baseline_members):
        raise TemporalComparisonContractError(error)
    definitions = (
        validate_month_phase_member_definitions(value.get("member_definitions"))
        if partition_field == "month_phase"
        else ()
    )
    if (
        partition_field != "month_phase"
        and value.get("member_definitions") not in ((), [])
    ):
        raise TemporalComparisonContractError(error)
    return _immutable_mapping(
        {
            "schema_version": "calendar-partition-role-frame.v1",
            "partition_field": partition_field,
            "target_members": target_members,
            "baseline_members": baseline_members,
            "aggregation": _validate_aggregation(value.get("aggregation"), error),
            "member_definitions": definitions,
        }
    )


def calendar_partition_role_for_date(
    observed_on: date,
    frame: Mapping[str, Any],
) -> str | None:
    if not isinstance(observed_on, date):
        raise TemporalComparisonContractError(
            "temporal_calendar_partition_observation_invalid"
        )
    normalized = validate_calendar_partition_role_frame(frame)
    partition_field = str(normalized["partition_field"])
    if partition_field == "quarter_of_year":
        member: Any = f"Q{((observed_on.month - 1) // 3) + 1}"
    elif partition_field == "month_of_year":
        member = observed_on.month
    elif partition_field == "iso_weekday":
        member = observed_on.isoweekday()
    else:
        definitions = tuple(normalized["member_definitions"])
        member = next(
            (
                item["member"]
                for item in definitions
                if int(item["day_start"])
                <= observed_on.day
                <= int(item["day_end"])
            ),
            None,
        )
    if member in set(normalized["target_members"]):
        return "target"
    if member in set(normalized["baseline_members"]):
        return "baseline"
    return None


def validate_comparison_spec(
    value: Any,
    *,
    time_spec: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Validate the exact comparison_spec union against its target time authority."""

    error = "temporal_comparison_spec_invalid"
    normalized_time = validate_time_spec(time_spec)
    if not isinstance(value, Mapping):
        raise TemporalComparisonContractError(error)
    kind = value.get("kind")
    if not isinstance(kind, str) or kind not in COMPARISON_KINDS:
        raise TemporalComparisonContractError(error)
    if kind == "none":
        if set(value) != {"kind"}:
            raise TemporalComparisonContractError(error)
        return _immutable_mapping({"kind": "none"})
    if kind == "decision_slot":
        if set(value) != {"kind", "slot_id"}:
            raise TemporalComparisonContractError(error)
        slot_id = _required_string(value.get("slot_id"), error)
        if slot_id not in COMPARISON_DECISION_SLOT_IDS:
            raise TemporalComparisonContractError(error)
        if (slot_id == "comparison_baseline" and normalized_time["kind"] != "date") or (
            slot_id == "comparison_window" and normalized_time["kind"] != "date_range"
        ) or (
            slot_id == "comparison_interpretation"
            and normalized_time["kind"] != "date_range"
        ):
            raise TemporalComparisonContractError(error)
        return _immutable_mapping(
            {
                "kind": "decision_slot",
                "slot_id": slot_id,
            }
        )
    if kind == "fixed_window":
        if set(value) != {
            "kind",
            "baseline_class",
            "baseline_start",
            "baseline_end",
            "aggregation",
        }:
            raise TemporalComparisonContractError(error)
        bounds = target_bounds(normalized_time)
        if bounds is None:
            raise TemporalComparisonContractError(error)
        baseline_class = value.get("baseline_class")
        if (
            not isinstance(baseline_class, str)
            or baseline_class not in FIXED_WINDOW_BASELINE_CLASSES
        ):
            raise TemporalComparisonContractError(error)
        baseline_start, baseline_end = _date_bounds(
            start=value.get("baseline_start"),
            end=value.get("baseline_end"),
            error=error,
        )
        target_start, target_end = bounds
        if not (baseline_end < target_start or baseline_start > target_end):
            raise TemporalComparisonContractError(error)
        if (
            baseline_class in {"prior_period", "same_period_last_year"}
            and baseline_end >= target_start
        ):
            raise TemporalComparisonContractError(error)
        if baseline_class == "same_period_last_year" and (
            date.fromisoformat(target_start).year
            - date.fromisoformat(baseline_start).year
            != 1
            or date.fromisoformat(target_end).year
            - date.fromisoformat(baseline_end).year
            != 1
            or target_start[5:] != baseline_start[5:]
            or target_end[5:] != baseline_end[5:]
        ):
            raise TemporalComparisonContractError(error)
        return _immutable_mapping(
            {
                "kind": "fixed_window",
                "baseline_class": baseline_class,
                "baseline_start": baseline_start,
                "baseline_end": baseline_end,
                "aggregation": _validate_aggregation(value.get("aggregation"), error),
            }
        )
    if kind == "calendar_partition":
        partition_field = value.get("partition_field")
        expected_fields = {
            "kind",
            "baseline_class",
            "period_grain",
            "partition_field",
            "target_members",
            "baseline_members",
            "aggregation",
        }
        if partition_field == "month_phase":
            expected_fields.add("member_definitions")
        if set(value) != expected_fields:
            raise TemporalComparisonContractError(error)
        if normalized_time["kind"] != "date_range":
            raise TemporalComparisonContractError(error)
        contract = CALENDAR_PARTITION_CONTRACTS.get(str(partition_field))
        if contract is None or value.get("period_grain") != contract[0]:
            raise TemporalComparisonContractError(error)
        baseline_class = value.get("baseline_class")
        if (
            not isinstance(baseline_class, str)
            or baseline_class
            not in _CALENDAR_BASELINE_CLASSES[str(partition_field)]
        ):
            raise TemporalComparisonContractError(error)
        target_members = _validate_calendar_members(
            value.get("target_members"),
            allowed=contract[1],
            member_order=_CALENDAR_MEMBER_ORDER[str(partition_field)],
            error=error,
        )
        baseline_members = _validate_calendar_members(
            value.get("baseline_members"),
            allowed=contract[1],
            member_order=_CALENDAR_MEMBER_ORDER[str(partition_field)],
            error=error,
        )
        if set(target_members).intersection(baseline_members):
            raise TemporalComparisonContractError(error)
        month_phase_member_definitions = (
            validate_month_phase_member_definitions(value.get("member_definitions"))
            if partition_field == "month_phase"
            else ()
        )
        _validate_calendar_range_coverage(
            time_spec=normalized_time,
            partition_field=str(partition_field),
            target_members=target_members,
            baseline_members=baseline_members,
            month_phase_member_definitions=month_phase_member_definitions,
            error=error,
        )
        normalized_partition = {
                "kind": "calendar_partition",
                "baseline_class": baseline_class,
                "period_grain": contract[0],
                "partition_field": partition_field,
                "target_members": target_members,
                "baseline_members": baseline_members,
                "aggregation": _validate_aggregation(value.get("aggregation"), error),
        }
        if month_phase_member_definitions:
            normalized_partition["member_definitions"] = (
                month_phase_member_definitions
            )
        return _immutable_mapping(normalized_partition)
    if set(value) != {
        "kind",
        "event_ref",
        "target_start",
        "target_end",
        "baseline_start",
        "baseline_end",
        "aggregation",
    }:
        raise TemporalComparisonContractError(error)
    bounds = target_bounds(normalized_time)
    explicit_target = _date_bounds(
        start=value.get("target_start"), end=value.get("target_end"), error=error
    )
    if bounds is not None and explicit_target != bounds:
        raise TemporalComparisonContractError(error)
    baseline_start, baseline_end = _date_bounds(
        start=value.get("baseline_start"), end=value.get("baseline_end"), error=error
    )
    if not (baseline_end < explicit_target[0] or baseline_start > explicit_target[1]):
        raise TemporalComparisonContractError(error)
    return _immutable_mapping(
        {
            "kind": "event_relative_window",
            "event_ref": _required_string(value.get("event_ref"), error),
            "target_start": explicit_target[0],
            "target_end": explicit_target[1],
            "baseline_start": baseline_start,
            "baseline_end": baseline_end,
            "aggregation": _validate_aggregation(value.get("aggregation"), error),
        }
    )


def normalize_temporal_decision_value(
    *,
    slot_id: str,
    value: Any,
    time_spec: Mapping[str, Any],
) -> tuple[Mapping[str, Any], str]:
    """Normalize one typed temporal decision and return its stable value ref."""

    normalized_time = validate_time_spec(time_spec)
    if slot_id == "comparison_baseline":
        if normalized_time["kind"] != "date" or not isinstance(value, Mapping):
            raise TemporalComparisonContractError(
                "temporal_comparison_decision_invalid"
            )
        try:
            baseline_ids = canonical_baseline_ids(value)
        except BaselineSemanticError as exc:
            raise TemporalComparisonContractError(
                "temporal_comparison_decision_invalid"
            ) from exc
        if len(baseline_ids) != 1:
            raise TemporalComparisonContractError(
                "temporal_comparison_decision_invalid"
            )
        baseline_id = baseline_ids[0]
        return _immutable_mapping({"baseline_id": baseline_id}), baseline_id

    if slot_id == "month_phase_definition":
        if not isinstance(value, Mapping) or set(value) != {
            "value_ref",
            "member_definitions",
        }:
            raise TemporalComparisonContractError(
                "temporal_comparison_decision_invalid"
            )
        value_ref = value.get("value_ref")
        if value_ref not in MONTH_PHASE_DEFINITION_VALUE_REFS:
            raise TemporalComparisonContractError(
                "temporal_comparison_decision_invalid"
            )
        definitions = validate_month_phase_member_definitions(
            value.get("member_definitions")
        )
        return (
            _immutable_mapping(
                {
                    "value_ref": str(value_ref),
                    "member_definitions": definitions,
                }
            ),
            str(value_ref),
        )

    if slot_id == "phase_aggregation":
        if not isinstance(value, Mapping) or set(value) != {"aggregation"}:
            raise TemporalComparisonContractError(
                "temporal_comparison_decision_invalid"
            )
        aggregation = _validate_aggregation(
            value.get("aggregation"),
            "temporal_comparison_decision_invalid",
        )
        return _immutable_mapping({"aggregation": aggregation}), aggregation

    if slot_id not in {
        "comparison_window",
        "comparison_interpretation",
        "event_relative_window",
    }:
        raise TemporalComparisonContractError("temporal_comparison_decision_invalid")
    normalized = validate_comparison_spec(value, time_spec=normalized_time)
    if slot_id in {"comparison_window", "comparison_interpretation"}:
        if normalized["kind"] not in {"fixed_window", "calendar_partition"}:
            raise TemporalComparisonContractError(
                "temporal_comparison_decision_invalid"
            )
        if slot_id == "comparison_interpretation":
            return (
                normalized,
                "interpretation_" + canonical_digest(normalized)[:16],
            )
        value_ref = str(normalized["baseline_class"])
        if value_ref not in COMPARISON_WINDOW_VALUE_REFS:
            raise TemporalComparisonContractError(
                "temporal_comparison_decision_invalid"
            )
        return normalized, value_ref
    if normalized["kind"] != "event_relative_window":
        raise TemporalComparisonContractError("temporal_comparison_decision_invalid")
    return normalized, str(normalized["event_ref"])


def temporal_decision_option_id(
    *,
    slot_id: str,
    value: Any,
    time_spec: Mapping[str, Any],
) -> str:
    """Return the content-stable option identity for one typed temporal choice."""

    normalized, value_ref = normalize_temporal_decision_value(
        slot_id=slot_id,
        value=value,
        time_spec=time_spec,
    )
    if slot_id == "comparison_baseline":
        return f"{slot_id}.{value_ref}"
    return f"{slot_id}.{value_ref}.{canonical_digest(normalized)[:16]}"


def validate_comparison_slot_binding(
    comparison_spec: Mapping[str, Any],
    *,
    ambiguity_slots: tuple[Mapping[str, Any], ...],
) -> None:
    """Ensure one comparison authority: immutable explicit spec or one ledger slot."""

    slot_ids = tuple(str(slot["slot_id"]) for slot in ambiguity_slots)
    comparison_slot_ids = set(slot_ids).intersection(COMPARISON_DECISION_SLOT_IDS)
    override_slot_ids = set(slot_ids).intersection(COMPARISON_OVERRIDE_SLOT_IDS)
    if comparison_spec["kind"] == "decision_slot":
        slot_id = str(comparison_spec["slot_id"])
        if (
            slot_ids.count(slot_id) != 1
            or comparison_slot_ids != {slot_id}
            or override_slot_ids
        ):
            raise TemporalComparisonContractError(
                "temporal_comparison_decision_slot_missing"
            )
        slot = next(slot for slot in ambiguity_slots if slot["slot_id"] == slot_id)
        if slot.get("status") != "unresolved" or slot.get("materiality") != "material":
            raise TemporalComparisonContractError(
                "temporal_comparison_decision_slot_invalid"
            )
        return
    if comparison_slot_ids:
        raise TemporalComparisonContractError("temporal_comparison_authority_conflict")
    if not override_slot_ids:
        return
    if (
        comparison_spec.get("kind") != "calendar_partition"
        or comparison_spec.get("partition_field") != "month_phase"
    ):
        raise TemporalComparisonContractError(
            "temporal_comparison_override_slot_invalid"
        )
    for slot in ambiguity_slots:
        if str(slot["slot_id"]) not in override_slot_ids:
            continue
        if slot.get("status") != "unresolved" or slot.get("materiality") != "material":
            raise TemporalComparisonContractError(
                "temporal_comparison_override_slot_invalid"
            )


@dataclass(frozen=True)
class TemporalWindow:
    window_ref: str
    role: str
    start: str | None
    end: str | None
    boundary: str
    aggregation: str | None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "TemporalWindow":
        expected = {
            "window_ref",
            "role",
            "start",
            "end",
            "boundary",
            "aggregation",
        }
        if not isinstance(payload, Mapping) or set(payload) != expected:
            raise TemporalComparisonContractError("temporal_window_shape_invalid")
        window_ref = payload.get("window_ref")
        role = payload.get("role")
        boundary = payload.get("boundary")
        aggregation = payload.get("aggregation")
        if (
            not isinstance(window_ref, str)
            or not window_ref
            or role not in {"target", "baseline"}
            or boundary not in {"inclusive", "unresolved"}
            or aggregation not in {None, *TEMPORAL_WINDOW_AGGREGATIONS}
        ):
            raise TemporalComparisonContractError("temporal_window_shape_invalid")
        start = payload.get("start")
        end = payload.get("end")
        if boundary == "unresolved":
            if role != "target" or start is not None or end is not None or aggregation:
                raise TemporalComparisonContractError("temporal_window_shape_invalid")
        else:
            start, end = _date_bounds(
                start=start,
                end=end,
                error="temporal_window_shape_invalid",
            )
        return cls(
            window_ref=window_ref,
            role=str(role),
            start=start,
            end=end,
            boundary=str(boundary),
            aggregation=aggregation,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_ref": self.window_ref,
            "role": self.role,
            "start": self.start,
            "end": self.end,
            "boundary": self.boundary,
            "aggregation": self.aggregation,
        }


@dataclass(frozen=True)
class EffectiveTemporalComparison:
    mode: str
    source: str
    time_spec: Mapping[str, Any]
    intent_comparison_spec: Mapping[str, Any]
    effective_comparison_spec: Mapping[str, Any]
    decision_refs: tuple[str, ...]
    target_window: TemporalWindow
    baseline_window: TemporalWindow | None
    calendar_partition: Mapping[str, Any] | None
    event_ref: str | None
    baseline_ids: tuple[str, ...]
    content_digest: str
    authority_ref: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EffectiveTemporalComparison":
        expected = {
            "schema_version",
            "mode",
            "source",
            "time_spec",
            "intent_comparison_spec",
            "effective_comparison_spec",
            "decision_refs",
            "target_window",
            "baseline_window",
            "calendar_partition",
            "event_ref",
            "baseline_ids",
            "resolved_window_refs",
            "content_digest",
            "authority_ref",
        }
        if (
            not isinstance(payload, Mapping)
            or set(payload) != expected
            or payload.get("schema_version") != "temporal-comparison-authority.v3"
        ):
            raise TemporalComparisonContractError("temporal_authority_shape_invalid")
        try:
            target_window = TemporalWindow.from_dict(payload["target_window"])
            baseline_window = (
                TemporalWindow.from_dict(payload["baseline_window"])
                if payload["baseline_window"] is not None
                else None
            )
        except (TypeError, ValueError) as exc:
            raise TemporalComparisonContractError(
                "temporal_authority_shape_invalid"
            ) from exc
        mode = payload.get("mode")
        source = payload.get("source")
        decision_refs = payload.get("decision_refs")
        event_ref = payload.get("event_ref")
        baseline_ids = payload.get("baseline_ids")
        resolved_window_refs = payload.get("resolved_window_refs")
        if (
            mode not in TEMPORAL_MODES
            or source not in {"intent", "decision", "unresolved_decision_slot"}
            or not isinstance(decision_refs, list)
            or any(
                not isinstance(item, str) or not item
                for item in decision_refs
            )
            or len(decision_refs) != len(set(decision_refs))
            or (
                event_ref is not None
                and (not isinstance(event_ref, str) or not event_ref)
            )
            or not isinstance(baseline_ids, list)
            or any(not isinstance(item, str) or not item for item in baseline_ids)
            or len(baseline_ids) != len(set(baseline_ids))
            or not isinstance(resolved_window_refs, list)
            or any(
                not isinstance(item, str) or not item for item in resolved_window_refs
            )
        ):
            raise TemporalComparisonContractError("temporal_authority_shape_invalid")
        intent_spec = payload.get("intent_comparison_spec")
        effective_spec = payload.get("effective_comparison_spec")
        time_spec = payload.get("time_spec")
        if (
            not isinstance(time_spec, Mapping)
            or not isinstance(intent_spec, Mapping)
            or not isinstance(effective_spec, Mapping)
        ):
            raise TemporalComparisonContractError("temporal_authority_shape_invalid")
        _validate_temporal_authority_material(
            mode=str(mode),
            source=str(source),
            time_spec=time_spec,
            decision_refs=tuple(decision_refs),
            target_window=target_window,
            baseline_window=baseline_window,
            calendar_partition=payload.get("calendar_partition"),
            event_ref=event_ref,
            baseline_ids=tuple(baseline_ids),
            intent_spec=intent_spec,
            effective_spec=effective_spec,
        )
        rebuilt = _effective_result(
            mode=str(mode),
            source=str(source),
            time_spec=time_spec,
            intent_spec=intent_spec,
            effective_spec=effective_spec,
            decision_refs=tuple(decision_refs),
            target_window=target_window,
            baseline_window=baseline_window,
            calendar_partition=(
                payload["calendar_partition"]
                if isinstance(payload.get("calendar_partition"), Mapping)
                else None
            ),
            event_ref=event_ref,
            baseline_ids=tuple(baseline_ids),
        )
        if (
            payload.get("content_digest") != rebuilt.content_digest
            or payload.get("authority_ref") != rebuilt.authority_ref
            or resolved_window_refs != list(rebuilt.resolved_window_refs)
            or canonical_value(payload) != rebuilt.to_dict()
        ):
            raise TemporalComparisonContractError(
                "temporal_authority_integrity_invalid"
            )
        return rebuilt

    @property
    def comparison_ref(self) -> str:
        return self.authority_ref

    @property
    def target_window_ref(self) -> str:
        return self.target_window.window_ref

    @property
    def baseline_window_refs(self) -> tuple[str, ...]:
        return (
            (self.baseline_window.window_ref,)
            if self.baseline_window is not None
            else ()
        )

    @property
    def resolved_window_refs(self) -> tuple[str, ...]:
        return (self.target_window_ref, *self.baseline_window_refs)

    @property
    def has_physical_target(self) -> bool:
        return (
            self.target_window.start is not None and self.target_window.end is not None
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **self._material_payload(),
            "content_digest": self.content_digest,
            "authority_ref": self.authority_ref,
        }

    def _material_payload(self) -> dict[str, Any]:
        return {
            "schema_version": "temporal-comparison-authority.v3",
            "mode": self.mode,
            "source": self.source,
            "time_spec": canonical_value(self.time_spec),
            "intent_comparison_spec": canonical_value(self.intent_comparison_spec),
            "effective_comparison_spec": canonical_value(
                self.effective_comparison_spec
            ),
            "decision_refs": list(self.decision_refs),
            "target_window": self.target_window.to_dict(),
            "baseline_window": (
                self.baseline_window.to_dict()
                if self.baseline_window is not None
                else None
            ),
            "calendar_partition": canonical_value(self.calendar_partition),
            "event_ref": self.event_ref,
            "baseline_ids": list(self.baseline_ids),
            "resolved_window_refs": list(self.resolved_window_refs),
        }


def _validate_temporal_authority_material(
    *,
    mode: str,
    source: str,
    time_spec: Mapping[str, Any],
    decision_refs: tuple[str, ...],
    target_window: TemporalWindow,
    baseline_window: TemporalWindow | None,
    calendar_partition: Any,
    event_ref: str | None,
    baseline_ids: tuple[str, ...],
    intent_spec: Mapping[str, Any],
    effective_spec: Mapping[str, Any],
) -> None:
    error = "temporal_authority_shape_invalid"
    if target_window.role != "target":
        raise TemporalComparisonContractError(error)
    if baseline_window is not None and baseline_window.role != "baseline":
        raise TemporalComparisonContractError(error)
    if source == "intent":
        if decision_refs or intent_spec != effective_spec:
            raise TemporalComparisonContractError(error)
    elif source == "decision":
        decision_slot_source = intent_spec.get("kind") == "decision_slot"
        decision_binding_source = _is_calendar_partition_decision_binding(
            intent_spec=intent_spec,
            effective_spec=effective_spec,
        )
        if not decision_refs or not (
            decision_slot_source or decision_binding_source
        ):
            raise TemporalComparisonContractError(error)
    elif mode != "unresolved" or decision_refs:
        raise TemporalComparisonContractError(error)

    normalized_time = validate_time_spec(time_spec)
    normalized_intent = validate_comparison_spec(
        intent_spec,
        time_spec=normalized_time,
    )

    if mode == "unresolved":
        expected_target = _pending_decision_target_window(normalized_time)
        if (
            source != "unresolved_decision_slot"
            or normalized_intent["kind"] != "decision_slot"
            or canonical_value(effective_spec) != canonical_value(normalized_intent)
            or target_window != expected_target
            or baseline_window is not None
            or calendar_partition is not None
            or event_ref is not None
            or baseline_ids
        ):
            raise TemporalComparisonContractError(error)
        return

    if target_window.boundary != "inclusive":
        raise TemporalComparisonContractError(error)
    normalized_bounds = target_bounds(normalized_time)
    if mode != "event_relative" and (
        normalized_bounds is None
        or target_window.start != normalized_bounds[0]
        or target_window.end != normalized_bounds[1]
        or target_window.window_ref != target_window_ref(normalized_time)
    ):
        raise TemporalComparisonContractError(error)
    if effective_spec.get("kind") == "canonical_daily":
        if (
            mode != "window_pair"
            or source != "decision"
            or set(effective_spec) != {"kind", "baseline_id", "aggregation"}
            or baseline_window is None
            or calendar_partition is not None
            or event_ref is not None
        ):
            raise TemporalComparisonContractError(error)
        normalized_value, baseline_id = normalize_temporal_decision_value(
            slot_id="comparison_baseline",
            value={"baseline_id": effective_spec.get("baseline_id")},
            time_spec=time_spec,
        )
        del normalized_value
        expected_aggregation = canonical_baseline_aggregation(baseline_id)
        baseline_start, baseline_end = canonical_baseline_bounds(
            str(target_window.start),
            baseline_id,
        )
        if (
            effective_spec.get("aggregation") != expected_aggregation
            or target_window.aggregation != "sum_of_complete_days"
            or baseline_window.start != baseline_start
            or baseline_window.end != baseline_end
            or baseline_window.aggregation != expected_aggregation
            or baseline_window.window_ref != f"window:baseline:{baseline_id}"
            or baseline_ids != (baseline_id,)
        ):
            raise TemporalComparisonContractError(error)
        return

    normalized_effective = validate_comparison_spec(
        effective_spec,
        time_spec=normalized_time,
    )
    kind = str(normalized_effective["kind"])
    expected_mode = {
        "none": "target_only",
        "fixed_window": "window_pair",
        "calendar_partition": "calendar_partition",
        "event_relative_window": "event_relative",
    }.get(kind)
    if expected_mode != mode or baseline_ids:
        raise TemporalComparisonContractError(error)
    if kind == "none":
        expected_target_aggregation = _physical_target_aggregation(normalized_time)
        if (
            baseline_window is not None
            or calendar_partition is not None
            or event_ref is not None
            or target_window.aggregation != expected_target_aggregation
        ):
            raise TemporalComparisonContractError(error)
        return
    aggregation = str(normalized_effective["aggregation"])
    if target_window.aggregation != aggregation:
        raise TemporalComparisonContractError(error)
    if kind == "calendar_partition":
        expected_partition = {
            key: normalized_effective[key]
            for key in (
                "baseline_class",
                "period_grain",
                "partition_field",
                "target_members",
                "baseline_members",
                "aggregation",
            )
        }
        if "member_definitions" in normalized_effective:
            expected_partition["member_definitions"] = normalized_effective[
                "member_definitions"
            ]
        if (
            baseline_window is not None
            or event_ref is not None
            or not isinstance(calendar_partition, Mapping)
            or canonical_value(calendar_partition)
            != canonical_value(expected_partition)
        ):
            raise TemporalComparisonContractError(error)
        return
    if baseline_window is None or calendar_partition is not None:
        raise TemporalComparisonContractError(error)
    baseline_authority = {
        key: normalized_effective[key]
        for key in normalized_effective
        if key
        in {
            "kind",
            "baseline_class",
            "event_ref",
            "baseline_start",
            "baseline_end",
            "aggregation",
        }
    }
    if (
        baseline_window.start != normalized_effective["baseline_start"]
        or baseline_window.end != normalized_effective["baseline_end"]
        or baseline_window.aggregation != aggregation
        or baseline_window.window_ref
        != "window:baseline:sha256:" + canonical_digest(baseline_authority)
    ):
        raise TemporalComparisonContractError(error)
    if kind == "event_relative_window":
        if event_ref != normalized_effective[
            "event_ref"
        ] or target_window != _event_target_window(
            normalized_time, normalized_effective
        ):
            raise TemporalComparisonContractError(error)
    elif event_ref is not None:
        raise TemporalComparisonContractError(error)


def _is_calendar_partition_decision_binding(
    *,
    intent_spec: Mapping[str, Any],
    effective_spec: Mapping[str, Any],
) -> bool:
    if (
        intent_spec.get("kind") != "calendar_partition"
        or effective_spec.get("kind") != "calendar_partition"
        or intent_spec.get("partition_field") != "month_phase"
        or effective_spec.get("partition_field") != "month_phase"
    ):
        return False
    stable_fields = {
        "kind",
        "baseline_class",
        "period_grain",
        "partition_field",
        "target_members",
        "baseline_members",
    }
    return all(
        canonical_value(intent_spec.get(field))
        == canonical_value(effective_spec.get(field))
        for field in stable_fields
    )


def _active_comparison_decisions(
    decision_ledger: Any,
    *,
    additional_slot_ids: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    active_records = getattr(decision_ledger, "active_records", None)
    if not callable(active_records):
        raise TemporalComparisonContractError("temporal_decision_ledger_invalid")
    comparison_slot_ids = (
        COMPARISON_DECISION_SLOT_IDS
        .union(COMPARISON_OVERRIDE_SLOT_IDS)
        .union(additional_slot_ids)
    )
    return {
        str(record.slot_id): record
        for record in active_records()
        if str(record.slot_id) in comparison_slot_ids
    }


def _effective_result(
    *,
    mode: str,
    source: str,
    time_spec: Mapping[str, Any],
    intent_spec: Mapping[str, Any],
    effective_spec: Mapping[str, Any],
    decision_refs: tuple[str, ...],
    target_window: TemporalWindow,
    baseline_window: TemporalWindow | None = None,
    calendar_partition: Mapping[str, Any] | None = None,
    event_ref: str | None = None,
    baseline_ids: tuple[str, ...] = (),
) -> EffectiveTemporalComparison:
    if mode not in TEMPORAL_MODES:
        raise TemporalComparisonContractError("temporal_mode_invalid")
    provisional = EffectiveTemporalComparison(
        mode=mode,
        source=source,
        time_spec=_immutable_mapping(validate_time_spec(time_spec)),
        intent_comparison_spec=_immutable_mapping(intent_spec),
        effective_comparison_spec=_immutable_mapping(effective_spec),
        decision_refs=decision_refs,
        target_window=target_window,
        baseline_window=baseline_window,
        calendar_partition=(
            _immutable_mapping(calendar_partition)
            if calendar_partition is not None
            else None
        ),
        event_ref=event_ref,
        baseline_ids=baseline_ids,
        content_digest="pending",
        authority_ref="pending",
    )
    digest = canonical_digest(provisional._material_payload())
    return EffectiveTemporalComparison(
        **{
            **provisional.__dict__,
            "content_digest": digest,
            "authority_ref": "temporal-comparison:sha256:" + digest,
        }
    )


def _target_window(
    time_spec: Mapping[str, Any],
    *,
    aggregation: str,
) -> TemporalWindow:
    bounds = target_bounds(time_spec)
    if bounds is None:
        raise TemporalComparisonContractError("temporal_target_window_unresolved")
    return TemporalWindow(
        window_ref=target_window_ref(time_spec),
        role="target",
        start=bounds[0],
        end=bounds[1],
        boundary="inclusive",
        aggregation=aggregation,
    )


def _physical_target_aggregation(time_spec: Mapping[str, Any]) -> str:
    bounds = target_bounds(time_spec)
    if bounds is None:
        raise TemporalComparisonContractError("temporal_target_window_unresolved")
    return "daily_total" if bounds[0] == bounds[1] else "sum_of_complete_days"


def _pending_decision_target_window(
    time_spec: Mapping[str, Any],
) -> TemporalWindow:
    if target_bounds(time_spec) is not None:
        return _target_window(
            time_spec,
            aggregation=_physical_target_aggregation(time_spec),
        )
    return TemporalWindow(
        window_ref=target_window_ref(time_spec),
        role="target",
        start=None,
        end=None,
        boundary="unresolved",
        aggregation=None,
    )


def _baseline_window(
    *,
    baseline_ref: str,
    start: str,
    end: str,
    aggregation: str,
) -> TemporalWindow:
    return TemporalWindow(
        window_ref=baseline_ref,
        role="baseline",
        start=start,
        end=end,
        boundary="inclusive",
        aggregation=aggregation,
    )


def _event_target_window(
    time_spec: Mapping[str, Any],
    event_spec: Mapping[str, Any],
) -> TemporalWindow:
    bounds = target_bounds(time_spec)
    event_bounds = (
        str(event_spec["target_start"]),
        str(event_spec["target_end"]),
    )
    if bounds is not None and bounds != event_bounds:
        raise TemporalComparisonContractError("temporal_comparison_spec_invalid")
    window_ref = (
        target_window_ref(time_spec)
        if bounds is not None
        else "window:target:sha256:"
        + canonical_digest(
            {
                "event_ref": event_spec["event_ref"],
                "target_start": event_bounds[0],
                "target_end": event_bounds[1],
                "boundary": "inclusive",
            }
        )
    )
    return TemporalWindow(
        window_ref=window_ref,
        role="target",
        start=event_bounds[0],
        end=event_bounds[1],
        boundary="inclusive",
        aggregation=str(event_spec["aggregation"]),
    )


def resolve_effective_comparison(
    *,
    time_spec: Mapping[str, Any],
    comparison_spec: Mapping[str, Any],
    decision_ledger: Any,
    require_physical_baseline: bool,
) -> EffectiveTemporalComparison:
    """Resolve intent plus ledger once into the plan's effective temporal authority."""

    normalized_time = validate_time_spec(time_spec)
    intent_spec = validate_comparison_spec(comparison_spec, time_spec=normalized_time)
    additional_slot_ids = (
        frozenset({str(intent_spec["slot_id"])})
        if intent_spec["kind"] == "decision_slot"
        else frozenset()
    )
    active = _active_comparison_decisions(
        decision_ledger,
        additional_slot_ids=additional_slot_ids,
    )
    if intent_spec["kind"] != "decision_slot":
        authority_decisions = {
            slot_id: decision
            for slot_id, decision in active.items()
            if slot_id in COMPARISON_DECISION_SLOT_IDS
        }
        override_decisions = {
            slot_id: decision
            for slot_id, decision in active.items()
            if slot_id in COMPARISON_OVERRIDE_SLOT_IDS
        }
        if authority_decisions:
            raise TemporalComparisonContractError(
                "temporal_comparison_authority_conflict"
            )
        if override_decisions:
            if (
                intent_spec.get("kind") != "calendar_partition"
                or intent_spec.get("partition_field") != "month_phase"
            ):
                raise TemporalComparisonContractError(
                    "temporal_comparison_authority_conflict"
                )
            effective = canonical_value(intent_spec)
            decision_ids: list[str] = []
            for slot_id in sorted(override_decisions):
                decision = override_decisions[slot_id]
                if decision.status not in {"inferred", "user_confirmed"}:
                    raise TemporalComparisonContractError(
                        "temporal_comparison_decision_invalid"
                    )
                normalized_value, _ = normalize_temporal_decision_value(
                    slot_id=slot_id,
                    value=decision.value,
                    time_spec=normalized_time,
                )
                if slot_id == "month_phase_definition":
                    effective["member_definitions"] = canonical_value(
                        normalized_value["member_definitions"]
                    )
                elif slot_id == "phase_aggregation":
                    effective["aggregation"] = str(
                        normalized_value["aggregation"]
                    )
                decision_ids.append(str(decision.decision_id))
            normalized_effective = validate_comparison_spec(
                effective,
                time_spec=normalized_time,
            )
            return _resolve_explicit_comparison(
                time_spec=normalized_time,
                intent_spec=intent_spec,
                effective_spec=normalized_effective,
                source="decision",
                decision_refs=tuple(decision_ids),
                require_physical_baseline=require_physical_baseline,
            )
        return _resolve_explicit_comparison(
            time_spec=normalized_time,
            intent_spec=intent_spec,
            effective_spec=intent_spec,
            source="intent",
            decision_refs=(),
            require_physical_baseline=require_physical_baseline,
        )

    slot_id = str(intent_spec["slot_id"])
    unexpected = set(active).difference({slot_id})
    if unexpected:
        raise TemporalComparisonContractError("temporal_comparison_authority_conflict")
    decision = active.get(slot_id)
    if decision is None:
        if require_physical_baseline:
            raise TemporalComparisonContractError(
                "temporal_comparison_decision_unresolved"
            )
        return _effective_result(
            mode="unresolved",
            source="unresolved_decision_slot",
            time_spec=normalized_time,
            intent_spec=intent_spec,
            effective_spec=intent_spec,
            decision_refs=(),
            target_window=_pending_decision_target_window(normalized_time),
        )
    if decision.status not in {"inferred", "user_confirmed"}:
        raise TemporalComparisonContractError("temporal_comparison_decision_invalid")
    normalized_decision_value, _ = normalize_temporal_decision_value(
        slot_id=slot_id,
        value=decision.value,
        time_spec=normalized_time,
    )
    if slot_id == "comparison_baseline":
        baseline_id = str(normalized_decision_value["baseline_id"])
        effective = {
            "kind": "canonical_daily",
            "baseline_id": baseline_id,
            "aggregation": canonical_baseline_aggregation(baseline_id),
        }
        target = str(normalized_time["target"])
        baseline_start, baseline_end = canonical_baseline_bounds(
            target,
            baseline_id,
        )
        return _effective_result(
            mode="window_pair",
            source="decision",
            time_spec=normalized_time,
            intent_spec=intent_spec,
            effective_spec=effective,
            decision_refs=(str(decision.decision_id),),
            target_window=_target_window(
                normalized_time,
                aggregation="sum_of_complete_days",
            ),
            baseline_window=_baseline_window(
                baseline_ref=f"window:baseline:{baseline_id}",
                start=baseline_start,
                end=baseline_end,
                aggregation=str(effective["aggregation"]),
            ),
            baseline_ids=(baseline_id,),
        )
    return _resolve_explicit_comparison(
        time_spec=normalized_time,
        intent_spec=intent_spec,
        effective_spec=normalized_decision_value,
        source="decision",
        decision_refs=(str(decision.decision_id),),
        require_physical_baseline=require_physical_baseline,
    )


def _resolve_explicit_comparison(
    *,
    time_spec: Mapping[str, Any],
    intent_spec: Mapping[str, Any],
    effective_spec: Mapping[str, Any],
    source: str,
    decision_refs: tuple[str, ...],
    require_physical_baseline: bool,
) -> EffectiveTemporalComparison:
    kind = str(effective_spec["kind"])
    if kind == "none":
        if require_physical_baseline:
            raise TemporalComparisonContractError("temporal_physical_baseline_required")
        return _effective_result(
            mode="target_only",
            source=source,
            time_spec=time_spec,
            intent_spec=intent_spec,
            effective_spec=effective_spec,
            decision_refs=decision_refs,
            target_window=_target_window(
                time_spec,
                aggregation=_physical_target_aggregation(time_spec),
            ),
        )
    if kind == "calendar_partition":
        if require_physical_baseline:
            raise TemporalComparisonContractError("temporal_physical_baseline_required")
        partition = {
            "baseline_class": effective_spec["baseline_class"],
            "period_grain": effective_spec["period_grain"],
            "partition_field": effective_spec["partition_field"],
            "target_members": effective_spec["target_members"],
            "baseline_members": effective_spec["baseline_members"],
            "aggregation": effective_spec["aggregation"],
        }
        if "member_definitions" in effective_spec:
            partition["member_definitions"] = effective_spec["member_definitions"]
        return _effective_result(
            mode="calendar_partition",
            source=source,
            time_spec=time_spec,
            intent_spec=intent_spec,
            effective_spec=effective_spec,
            decision_refs=decision_refs,
            target_window=_target_window(
                time_spec,
                aggregation=str(effective_spec["aggregation"]),
            ),
            calendar_partition=partition,
        )
    if kind not in {"fixed_window", "event_relative_window"}:
        raise TemporalComparisonContractError("temporal_comparison_decision_invalid")
    baseline_authority = {
        key: effective_spec[key]
        for key in effective_spec
        if key
        in {
            "kind",
            "baseline_class",
            "event_ref",
            "baseline_start",
            "baseline_end",
            "aggregation",
        }
    }
    baseline_ref = "window:baseline:sha256:" + canonical_digest(baseline_authority)
    target_aggregation = str(effective_spec["aggregation"])
    return _effective_result(
        mode=("event_relative" if kind == "event_relative_window" else "window_pair"),
        source=source,
        time_spec=time_spec,
        intent_spec=intent_spec,
        effective_spec=effective_spec,
        decision_refs=decision_refs,
        target_window=(
            _event_target_window(time_spec, effective_spec)
            if kind == "event_relative_window"
            else _target_window(time_spec, aggregation=target_aggregation)
        ),
        baseline_window=_baseline_window(
            baseline_ref=baseline_ref,
            start=str(effective_spec["baseline_start"]),
            end=str(effective_spec["baseline_end"]),
            aggregation=target_aggregation,
        ),
        event_ref=(
            str(effective_spec["event_ref"])
            if kind == "event_relative_window"
            else None
        ),
    )


def capability_supports_temporal_authority(
    capability_contract: Mapping[str, Any],
    temporal_authority: EffectiveTemporalComparison,
) -> bool:
    """Return whether Query IR can construct a typed input for this authority."""

    from bi_agent.runtime.query_ir import compile_capability_query_route

    if not isinstance(temporal_authority, EffectiveTemporalComparison):
        raise TemporalComparisonContractError("temporal_authority_invalid")
    try:
        route = compile_capability_query_route(
            capability_id="temporal-capability-probe",
            capability_contract=capability_contract,
            temporal_authority=temporal_authority,
        )
    except ValueError:
        return False
    return route.get("status") != "unavailable"


def temporal_execution_mode(
    temporal_authority: EffectiveTemporalComparison,
) -> str:
    """Map accepted temporal authority to the physical capability contract mode."""

    if not isinstance(temporal_authority, EffectiveTemporalComparison):
        raise TemporalComparisonContractError("temporal_authority_invalid")
    if temporal_authority.mode in {
        "target_only",
        "calendar_partition",
        "event_relative",
    }:
        return temporal_authority.mode
    if temporal_authority.mode != "window_pair":
        raise TemporalComparisonContractError("temporal_authority_unresolved")
    baseline = temporal_authority.baseline_window
    if baseline is None:
        raise TemporalComparisonContractError("temporal_baseline_window_missing")
    return (
        "single_day_window_pair"
        if temporal_authority.target_window.start
        == temporal_authority.target_window.end
        and baseline.start == baseline.end
        else "aggregate_window_pair"
    )


def resolve_rolling_window_strategy(
    temporal_authority: EffectiveTemporalComparison,
    *,
    parameters: Mapping[str, Any],
    maximum_context_days: int | None = None,
) -> RollingWindowStrategy:
    """Bind rolling analysis to the accepted physical target window."""

    if not isinstance(temporal_authority, EffectiveTemporalComparison):
        raise TemporalComparisonContractError("rolling_temporal_authority_invalid")
    if set(parameters) != ROLLING_WINDOW_PARAMETER_FIELDS:
        raise TemporalComparisonContractError("rolling_window_parameters_invalid")
    materiality_floor = parameters.get("materiality_floor")
    minimum_span_days = parameters.get("minimum_span_days")
    min_periods = parameters.get("min_periods")
    if (
        isinstance(materiality_floor, bool)
        or not isinstance(materiality_floor, (int, float))
        or not math.isfinite(float(materiality_floor))
        or isinstance(minimum_span_days, bool)
        or not isinstance(minimum_span_days, int)
        or minimum_span_days <= 0
        or isinstance(min_periods, bool)
        or not isinstance(min_periods, int)
        or min_periods < 2
        or parameters.get("rolling_span_policy")
        != "target_window_duration_with_minimum"
        or parameters.get("rolling_step_policy") != "target_window_duration"
    ):
        raise TemporalComparisonContractError("rolling_window_parameters_invalid")
    start = temporal_authority.target_window.start
    end = temporal_authority.target_window.end
    if start is None or end is None:
        raise TemporalComparisonContractError("rolling_target_window_unresolved")
    try:
        target_days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    except ValueError as exc:
        raise TemporalComparisonContractError(
            "rolling_target_window_invalid"
        ) from exc
    if target_days <= 0:
        raise TemporalComparisonContractError("rolling_target_window_invalid")
    if maximum_context_days is not None and (
        isinstance(maximum_context_days, bool)
        or not isinstance(maximum_context_days, int)
        or maximum_context_days <= 0
    ):
        raise TemporalComparisonContractError("rolling_context_limit_invalid")

    rolling_span_days = max(target_days, minimum_span_days)
    rolling_step_days = target_days
    required_context_days = rolling_span_days + (
        (min_periods - 1) * rolling_step_days
    )
    context_days = (
        min(required_context_days, maximum_context_days)
        if maximum_context_days is not None
        else required_context_days
    )
    return RollingWindowStrategy(
        materiality_floor=float(materiality_floor),
        rolling_span_days=rolling_span_days,
        rolling_step_days=rolling_step_days,
        min_periods=min_periods,
        context_days=context_days,
        context_limited=context_days < required_context_days,
    )
