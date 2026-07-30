"""Generic within-period comparison with explicit observed exposure."""

from __future__ import annotations

import csv
import hashlib
import io
import math
import re
import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import Any, Mapping, Protocol

from waje_vnext.controller.effects import (
    EffectExecutionResult,
    EffectPermanentError,
    EvidenceDraft,
)
from waje_vnext.domain.authority import (
    ComparisonGroupRole,
    EvidenceStrength,
    EvidenceType,
)
from waje_vnext.domain.canonical import content_sha256, require_nonempty
from waje_vnext.domain.runtime_state import OutboxMessage


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_QUALIFIED_IDENTIFIER = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$"
)
_GROUP_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class PeriodUnit(StrEnum):
    CALENDAR_MONTH = "calendar_month"


@dataclass(frozen=True, slots=True)
class SourceBinding:
    source_ref: str
    metric_ref: str
    table: str
    date_column: str
    value_column: str
    snapshot_release_ref: str
    business_timezone: str
    available_from: date
    available_through: date

    def __post_init__(self) -> None:
        for name in (
            "source_ref",
            "metric_ref",
            "table",
            "date_column",
            "value_column",
            "snapshot_release_ref",
            "business_timezone",
        ):
            require_nonempty(getattr(self, name), name)
        if not _QUALIFIED_IDENTIFIER.fullmatch(self.table):
            raise ValueError("source table must be a qualified identifier")
        for name in ("date_column", "value_column"):
            if not _IDENTIFIER.fullmatch(getattr(self, name)):
                raise ValueError("{} must be a safe identifier".format(name))
        if self.available_through < self.available_from:
            raise ValueError("source availability range is inverted")


@dataclass(frozen=True, slots=True)
class OrdinalGroupSpec:
    group_id: str
    role: ComparisonGroupRole
    lower_inclusive: int
    upper_inclusive: int

    def __post_init__(self) -> None:
        if not _GROUP_ID.fullmatch(self.group_id):
            raise ValueError("group_id must be a safe stable identifier")
        if not isinstance(self.role, ComparisonGroupRole):
            raise TypeError("role must be ComparisonGroupRole")
        if self.lower_inclusive < 1:
            raise ValueError("group lower bound must be positive")
        if self.upper_inclusive < self.lower_inclusive:
            raise ValueError("group ordinal range is inverted")


@dataclass(frozen=True, slots=True)
class PeriodComparisonQuerySpec:
    query_spec_id: str
    metric_ref: str
    source_ref: str
    period_unit: PeriodUnit
    range_start: date
    range_end: date
    groups: tuple[OrdinalGroupSpec, ...]

    def __post_init__(self) -> None:
        for name in ("query_spec_id", "metric_ref", "source_ref"):
            require_nonempty(getattr(self, name), name)
        if not isinstance(self.period_unit, PeriodUnit):
            raise TypeError("period_unit must be PeriodUnit")
        if self.range_end < self.range_start:
            raise ValueError("query range is inverted")
        if self.range_start.day != 1:
            raise ValueError("period comparison must start at a complete month")
        if (self.range_end + timedelta(days=1)).day != 1:
            raise ValueError("period comparison must end at a complete month")
        if not isinstance(self.groups, tuple) or len(self.groups) < 2:
            raise ValueError("period comparison requires at least two groups")
        group_ids = tuple(group.group_id for group in self.groups)
        if len(group_ids) != len(set(group_ids)):
            raise ValueError("period comparison group IDs must be unique")
        roles = {group.role for group in self.groups}
        if roles != {
            ComparisonGroupRole.FOCAL,
            ComparisonGroupRole.REFERENCE,
        }:
            raise ValueError(
                "period comparison requires focal and reference groups"
            )
        ordered = sorted(
            self.groups,
            key=lambda item: (item.lower_inclusive, item.upper_inclusive),
        )
        for prior, current in zip(ordered, ordered[1:], strict=False):
            if current.lower_inclusive <= prior.upper_inclusive:
                raise ValueError("period comparison groups cannot overlap")

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)


@dataclass(frozen=True, slots=True)
class PeriodComparisonRow:
    period_start: date
    group_id: str
    total_value: float
    observed_exposure_units: int
    value_per_exposure_unit: float

    def __post_init__(self) -> None:
        if not _GROUP_ID.fullmatch(self.group_id):
            raise ValueError("row group_id is invalid")
        if self.observed_exposure_units < 1:
            raise ValueError("row exposure units must be positive")
        for name in ("total_value", "value_per_exposure_unit"):
            if not math.isfinite(getattr(self, name)):
                raise ValueError("{} must be finite".format(name))


class QueryRunner(Protocol):
    def run(self, sql: str) -> str: ...


class PeriodComparisonEffectExecutor:
    def __init__(
        self,
        *,
        source_bindings: Mapping[str, SourceBinding],
        query_runner: QueryRunner,
    ) -> None:
        if not source_bindings:
            raise ValueError("period comparison requires source bindings")
        self._source_bindings = dict(source_bindings)
        self._query_runner = query_runner

    def execute(self, message: OutboxMessage) -> EffectExecutionResult:
        try:
            if message.destination != "analysis_probe":
                raise ValueError(
                    "period comparison executor only accepts analysis probes"
                )
            request = _mapping(message.payload["request"], "request")
            if request["probe_kind"] != "period_comparison":
                raise ValueError("unsupported probe kind")
            task_id = _string(request, "task_id")
            parameters = _mapping(request["parameters"], "parameters")
            spec = decode_period_comparison_spec(
                _mapping(parameters["query_spec"], "query_spec")
            )
            binding = self._source_bindings.get(spec.source_ref)
            if binding is None:
                raise ValueError("query source has no governed binding")
            sql = compile_period_comparison_sql(spec, binding)
            rows = parse_period_comparison_tsv(self._query_runner.run(sql))
            summary = summarize_period_comparison(spec, rows)
            evidence = build_period_comparison_evidence(
                spec=spec,
                binding=binding,
                task_id=task_id,
                rows=rows,
            )
            return EffectExecutionResult(
                payload=summary,
                business_summary=(
                    "Period comparison measured raw values, observed "
                    "exposure units, and exposure-normalized values"
                ),
                evidence=evidence,
            )
        except (
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            raise EffectPermanentError(
                "period comparison request violates its governed contract"
            ) from error


def decode_period_comparison_spec(
    value: Mapping[str, Any],
) -> PeriodComparisonQuerySpec:
    expected = {
        "query_spec_id",
        "metric_ref",
        "source_ref",
        "period_unit",
        "range_start",
        "range_end",
        "groups",
    }
    if set(value) != expected:
        raise ValueError("period comparison query spec fields differ")
    groups_value = value["groups"]
    if not isinstance(groups_value, list | tuple):
        raise TypeError("query spec groups must be an array")
    groups = []
    for raw_group in groups_value:
        group = _mapping(raw_group, "group")
        if set(group) != {
            "group_id",
            "role",
            "lower_inclusive",
            "upper_inclusive",
        }:
            raise ValueError("period comparison group fields differ")
        groups.append(
            OrdinalGroupSpec(
                group_id=_string(group, "group_id"),
                role=ComparisonGroupRole(_string(group, "role")),
                lower_inclusive=_integer(group, "lower_inclusive"),
                upper_inclusive=_integer(group, "upper_inclusive"),
            )
        )
    return PeriodComparisonQuerySpec(
        query_spec_id=_string(value, "query_spec_id"),
        metric_ref=_string(value, "metric_ref"),
        source_ref=_string(value, "source_ref"),
        period_unit=PeriodUnit(_string(value, "period_unit")),
        range_start=date.fromisoformat(_string(value, "range_start")),
        range_end=date.fromisoformat(_string(value, "range_end")),
        groups=tuple(groups),
    )


def compile_period_comparison_sql(
    spec: PeriodComparisonQuerySpec,
    binding: SourceBinding,
) -> str:
    _validate_binding(spec, binding)
    date_column = binding.date_column
    clauses = ", ".join(
        "toDayOfMonth({date}) BETWEEN {lower} AND {upper}, '{group}'".format(
            date=date_column,
            lower=group.lower_inclusive,
            upper=group.upper_inclusive,
            group=group.group_id,
        )
        for group in spec.groups
    )
    start = spec.range_start.isoformat()
    end = spec.range_end.isoformat()
    return (
        "SELECT period_start, group_id, "
        "sum(metric_value) AS total_value, "
        "uniqExact(business_date) AS observed_exposure_units, "
        "total_value / observed_exposure_units AS value_per_exposure_unit "
        "FROM ("
        "SELECT toStartOfMonth({date}) AS period_start, "
        "{date} AS business_date, {value} AS metric_value, "
        "multiIf({clauses}, '') AS group_id "
        "FROM {table} "
        "WHERE {date} BETWEEN toDate('{start}') AND toDate('{end}')"
        ") WHERE group_id != '' "
        "GROUP BY period_start, group_id "
        "ORDER BY period_start, group_id "
        "FORMAT TSVWithNames"
    ).format(
        date=date_column,
        value=binding.value_column,
        clauses=clauses,
        table=binding.table,
        start=start,
        end=end,
    )


def parse_period_comparison_tsv(payload: str) -> tuple[PeriodComparisonRow, ...]:
    reader = csv.DictReader(io.StringIO(payload), delimiter="\t")
    expected = {
        "period_start",
        "group_id",
        "total_value",
        "observed_exposure_units",
        "value_per_exposure_unit",
    }
    if reader.fieldnames is None or set(reader.fieldnames) != expected:
        raise ValueError("period comparison result columns do not match")
    rows = tuple(
        PeriodComparisonRow(
            period_start=date.fromisoformat(row["period_start"]),
            group_id=row["group_id"],
            total_value=float(row["total_value"]),
            observed_exposure_units=int(row["observed_exposure_units"]),
            value_per_exposure_unit=float(
                row["value_per_exposure_unit"]
            ),
        )
        for row in reader
    )
    if not rows:
        raise ValueError("period comparison result is empty")
    return rows


def summarize_period_comparison(
    spec: PeriodComparisonQuerySpec,
    rows: tuple[PeriodComparisonRow, ...],
) -> Mapping[str, object]:
    expected_groups = {group.group_id for group in spec.groups}
    by_period: dict[date, dict[str, PeriodComparisonRow]] = {}
    for row in rows:
        if row.group_id not in expected_groups:
            raise ValueError("result contains an undeclared comparison group")
        period_rows = by_period.setdefault(row.period_start, {})
        if row.group_id in period_rows:
            raise ValueError("result repeats a period and group")
        period_rows[row.group_id] = row
    complete = {
        period: group_rows
        for period, group_rows in by_period.items()
        if set(group_rows) == expected_groups
    }
    if not complete:
        raise ValueError("no complete comparable periods")
    focal_ids = tuple(
        group.group_id
        for group in spec.groups
        if group.role is ComparisonGroupRole.FOCAL
    )
    reference_ids = tuple(
        group.group_id
        for group in spec.groups
        if group.role is ComparisonGroupRole.REFERENCE
    )
    contrasts = []
    for focal_id in focal_ids:
        for reference_id in reference_ids:
            raw_ratios = []
            normalized_ratios = []
            raw_hits = 0
            normalized_hits = 0
            for group_rows in complete.values():
                focal = group_rows[focal_id]
                reference = group_rows[reference_id]
                raw_hits += focal.total_value > reference.total_value
                normalized_hits += (
                    focal.value_per_exposure_unit
                    > reference.value_per_exposure_unit
                )
                raw_ratios.append(
                    _safe_ratio(focal.total_value, reference.total_value)
                )
                normalized_ratios.append(
                    _safe_ratio(
                        focal.value_per_exposure_unit,
                        reference.value_per_exposure_unit,
                    )
                )
            contrasts.append(
                {
                    "focal_group_id": focal_id,
                    "reference_group_id": reference_id,
                    "raw_direction_hits": raw_hits,
                    "normalized_direction_hits": normalized_hits,
                    "comparable_periods": len(complete),
                    "median_raw_ratio": statistics.median(raw_ratios),
                    "median_normalized_ratio": statistics.median(
                        normalized_ratios
                    ),
                }
            )
    exposure_by_group = {
        group_id: {
            "minimum": min(
                rows_by_group[group_id].observed_exposure_units
                for rows_by_group in complete.values()
            ),
            "maximum": max(
                rows_by_group[group_id].observed_exposure_units
                for rows_by_group in complete.values()
            ),
            "median": statistics.median(
                rows_by_group[group_id].observed_exposure_units
                for rows_by_group in complete.values()
            ),
        }
        for group_id in sorted(expected_groups)
    }
    return {
        "query_spec_id": spec.query_spec_id,
        "comparable_periods": len(complete),
        "exposure_by_group": exposure_by_group,
        "contrasts": contrasts,
    }


def build_period_comparison_evidence(
    *,
    spec: PeriodComparisonQuerySpec,
    binding: SourceBinding,
    task_id: str,
    rows: tuple[PeriodComparisonRow, ...],
) -> tuple[EvidenceDraft, EvidenceDraft]:
    query = compile_period_comparison_sql(spec, binding)
    summary = summarize_period_comparison(spec, rows)
    query_sha256 = hashlib.sha256(query.encode("utf-8")).hexdigest()
    common_provenance = {
        "query_spec_id": spec.query_spec_id,
        "query_spec_sha256": spec.content_sha256,
        "compiled_query_sha256": query_sha256,
        "source_ref": binding.source_ref,
        "snapshot_release_ref": binding.snapshot_release_ref,
        "business_timezone": binding.business_timezone,
    }
    contract_refs = (binding.metric_ref, binding.source_ref)
    exposure_payload = {
        "comparable_periods": summary["comparable_periods"],
        "exposure_by_group": summary["exposure_by_group"],
    }
    pattern_payload = {
        "comparable_periods": summary["comparable_periods"],
        "contrasts": summary["contrasts"],
    }
    return (
        EvidenceDraft(
            task_id=task_id,
            capability_name="period_comparison",
            query_spec_ref=spec.query_spec_id,
            semantic_contract_refs=contract_refs,
            snapshot_release_ref=binding.snapshot_release_ref,
            grain="comparison_group_by_{}".format(spec.period_unit.value),
            evidence_type=EvidenceType.DATA_QUALITY,
            strength=EvidenceStrength.QUANTIFIED,
            business_summary=(
                "Observed exposure units were measured for every comparison "
                "group and complete period"
            ),
            limitations=(),
            provenance=common_provenance,
            inline_payload=exposure_payload,
        ),
        EvidenceDraft(
            task_id=task_id,
            capability_name="period_comparison",
            query_spec_ref=spec.query_spec_id,
            semantic_contract_refs=contract_refs,
            snapshot_release_ref=binding.snapshot_release_ref,
            grain="comparison_group_by_{}".format(spec.period_unit.value),
            evidence_type=EvidenceType.ASSOCIATION,
            strength=EvidenceStrength.QUANTIFIED,
            business_summary=(
                "Raw and exposure-normalized contrasts were measured from "
                "the same complete periods"
            ),
            limitations=(
                "Descriptive recurrence does not establish a mechanism",
            ),
            provenance=common_provenance,
            inline_payload=pattern_payload,
        ),
    )


def _validate_binding(
    spec: PeriodComparisonQuerySpec,
    binding: SourceBinding,
) -> None:
    if spec.metric_ref != binding.metric_ref:
        raise ValueError("query metric does not match source binding")
    if spec.source_ref != binding.source_ref:
        raise ValueError("query source does not match source binding")
    if (
        spec.range_start < binding.available_from
        or spec.range_end > binding.available_through
    ):
        raise ValueError("query range exceeds accepted source availability")


def _safe_ratio(numerator: float, denominator: float) -> float:
    if denominator == 0:
        raise ValueError("comparison denominator is zero")
    value = numerator / denominator
    if not math.isfinite(value):
        raise ValueError("comparison ratio is not finite")
    return value


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("{} must be an object".format(label))
    return value


def _string(value: Mapping[str, Any], field_name: str) -> str:
    member = value[field_name]
    if not isinstance(member, str):
        raise TypeError("{} must be a string".format(field_name))
    require_nonempty(member, field_name)
    return member


def _integer(value: Mapping[str, Any], field_name: str) -> int:
    member = value[field_name]
    if not isinstance(member, int) or isinstance(member, bool):
        raise TypeError("{} must be an integer".format(field_name))
    return member
